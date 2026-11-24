"""Low-latency video sources with bounded RTSP open/read timeouts."""
import logging
import math
import os
import threading
import time
import subprocess
from datetime import datetime, timezone
from collections import deque

import cv2

from vehicle_intelligence.common import Frame
from vehicle_intelligence.common.errors import Diagnostic, ProcessingError

log = logging.getLogger("grabber")

OPEN_TIMEOUT_MS = int(os.getenv("S1_RTSP_OPEN_TIMEOUT_MS", "10000"))
READ_TIMEOUT_MS = int(os.getenv("S1_RTSP_READ_TIMEOUT_MS", "10000"))


def _is_rtsp(source):
    return isinstance(source, str) and source.lower().startswith(("rtsp://", "rtsps://"))


def _redact(source):
    """Hide credentials from URLs before logging them."""
    if isinstance(source, str) and "://" in source and "@" in source:
        scheme, rest = source.split("://", 1)
        return scheme + "://***@" + rest.split("@", 1)[1]
    return source


def _open(source):
    if isinstance(source, str) and source.isdigit():
        source = int(source)
    if _is_rtsp(source):
        params = [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, OPEN_TIMEOUT_MS,
            cv2.CAP_PROP_READ_TIMEOUT_MSEC, READ_TIMEOUT_MS,
        ]
        try:
            cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG, params)
        except (TypeError, cv2.error) as exc:
            # Do not silently retry without timeouts: that can hang forever.
            raise RuntimeError(
                "This OpenCV build does not accept RTSP timeout parameters. "
                "Check cv2.getBuildInformation() and the installed OpenCV version."
            ) from exc
        return cap
    return cv2.VideoCapture(source)


class LatestFrameGrabber:
    """Read in a background thread into a bounded frame buffer.

    The default capacity of one preserves legacy latest-frame behavior.
    Multi-camera mode can opt into a small bounded history so brief transitions
    are less likely to be lost. On overload, the oldest queued frame is dropped.
    Frame timestamps are host decode times, not camera sensor timestamps.
    """

    def __init__(
        self, source, paced=False, start_time=None, buffer_capacity=1,
        evidence_history_seconds=4.0,
        evidence_sample_interval_s=0.1,
        evidence_max_width=640,
        evidence_jpeg_quality=65,
        evidence_event_max_count=4,
        evidence_event_max_bytes=5 * 1024 * 1024,
        evidence_event_total_max_bytes=12 * 1024 * 1024,
    ): # EVIDENCE_HISTORY_V1
        self.source = source
        self.is_file = isinstance(source, str) and os.path.isfile(source)
        self.paced = paced and self.is_file
        self.start_time = start_time
        self._cond = threading.Condition()
        self.buffer_capacity = max(1, int(buffer_capacity))
        self._frames = deque()
        self.dropped_frames = 0

        # Independent, compressed visual evidence history.
        self.evidence_history_seconds = max(0.0, float(evidence_history_seconds))
        self.evidence_sample_interval_s = max(0.01, float(evidence_sample_interval_s))
        self.evidence_max_width = max(64, int(evidence_max_width))
        self.evidence_jpeg_quality = min(95, max(30, int(evidence_jpeg_quality)))

        self._evidence_capacity = max(
            1,
            int(math.ceil(
                self.evidence_history_seconds /
                self.evidence_sample_interval_s
            )) + 2,
        )
        self._evidence_frames = deque()
        self._evidence_lock = threading.Lock()
        self._last_evidence_ts = None

        self.evidence_frames_stored = 0
        self.evidence_evicted_frames = 0
        self.evidence_encode_failures = 0

        # EVIDENCE_EVENT_PINNING_V1
        self.evidence_event_max_count = max(1, int(evidence_event_max_count))
        self.evidence_event_max_bytes = max(1, int(evidence_event_max_bytes))
        self.evidence_event_total_max_bytes = max(
            self.evidence_event_max_bytes,
            int(evidence_event_total_max_bytes),
        )
        self._evidence_events = {}
        self._evidence_event_bytes = 0
        self.evidence_events_rejected = 0
        self.evidence_event_overflows = 0

        self._count = 0
        self._stop = threading.Event()
        # Local-file playback can be paused without the decoder running ahead.
        # RTSP callers normally leave this gate open; UI pause/resume may still
        # use it when testing a recorded source.
        self._play_gate = threading.Event()
        self._play_gate.set()
        self._pause_lock = threading.Lock()
        self._paused_at = None
        self._paused_total = 0.0
        self._thread = threading.Thread(target=self._run, daemon=True, name="grabber")
        self.eof = False
        self.last_frame_wall = 0.0
        self.reconnects = 0
        self.last_error = None

    def start(self):
        if not self._thread.is_alive() and self._thread.ident is None:
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._play_gate.set()  # wake a paused capture thread so it can exit
        with self._cond:
            self._cond.notify_all()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            timeout = max(OPEN_TIMEOUT_MS, READ_TIMEOUT_MS) / 1000.0 + 1.0
            self._thread.join(timeout)
            if self._thread.is_alive():
                log.warning("VIDEO_CAPTURE_STOP_TIMEOUT after %.1fs", timeout)
        self.finalize_evidence_events("capture_stopped")

    def pause(self):
        """Pause decoding for a file source; no-op for live RTSP streams."""
        if self.is_file:
            with self._pause_lock:
                if self._play_gate.is_set():
                    self._paused_at = time.monotonic()
                    self._play_gate.clear()

    def resume(self):
        """Resume local playback without fast-forwarding to the old wall clock."""
        with self._pause_lock:
            if self._paused_at is not None:
                self._paused_total += max(0.0, time.monotonic() - self._paused_at)
                self._paused_at = None
            self._play_gate.set()

    def _sleep_interruptibly(self, seconds):
        self._stop.wait(seconds)

    def _run(self):
        backoff = 1.0
        while not self._stop.is_set():
            log.info(
                "opening %s via %s (open timeout=%ds, read timeout=%ds)",
                _redact(self.source),
                "FFmpeg/RTSP" if _is_rtsp(self.source) else "OpenCV",
                OPEN_TIMEOUT_MS // 1000,
                READ_TIMEOUT_MS // 1000,
            )
            t_open = time.monotonic()
            try:
                cap = _open(self.source)
            except Exception as exc:
                self.last_error = str(exc)
                log.exception("capture initialization failed for %s", _redact(self.source))
                self.reconnects += 1
                self._sleep_interruptibly(backoff)
                backoff = min(backoff * 2, 15.0)
                continue

            if not cap.isOpened():
                elapsed = time.monotonic() - t_open
                self.last_error = "capture did not open"
                log.warning(
                    "cannot open %s after %.1fs (check RTSP path, credentials, codec and OpenCV FFmpeg support); retry in %.0fs",
                    _redact(self.source), elapsed, backoff,
                )
                cap.release()
                self.reconnects += 1
                self._sleep_interruptibly(backoff)
                backoff = min(backoff * 2, 15.0)
                continue

            backoff = 1.0
            backend = "unknown"
            try:
                backend = cap.getBackendName()
            except (AttributeError, cv2.error):
                pass
            log.info(
                "capture opened for %s in %.1fs (backend=%s); waiting for decoded frames",
                _redact(self.source), time.monotonic() - t_open, backend,
            )
            fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
            wall0 = time.time()
            base = self.start_time if self.start_time is not None else wall0
            n = 0
            try:
                while not self._stop.is_set():
                    self._play_gate.wait()
                    if self._stop.is_set():
                        break
                    ok, img = cap.read()
                    if not ok or img is None:
                        self.last_error = f"read failed after {n} frames"
                        log.warning(
                            "read failed for %s after %d frames (timeout, EOF, authentication, or decode error)",
                            _redact(self.source), n,
                        )
                        break
                    # If Space paused while cap.read() was in flight, do not
                    # publish/decode ahead until playback is resumed.
                    self._play_gate.wait()
                    if self._stop.is_set():
                        break
                    # Timestamp immediately after decode; not a camera hardware timestamp.
                    decoded_at = time.time()
                    if n == 0:
                        log.info(
                            "first frame decoded: %dx%d after %.1fs",
                            img.shape[1], img.shape[0], time.monotonic() - t_open,
                        )
                    if self.is_file:
                        # Preserve source-video timeline even if processing lags.
                        ts = base + n / fps
                        if self.paced:
                            # Exclude user pause duration from the file's wall-clock
                            # schedule so pressing Space resumes instead of catching
                            # up by decoding a burst of frames.
                            delay = wall0 + n / fps + self._paused_total - time.time()
                            if delay > 0:
                                self._sleep_interruptibly(delay)
                    else:
                        ts = decoded_at
                    n += 1
                    self.last_frame_wall = time.time()
                    frame = Frame(ts, img, self._count)
                    self._append_evidence(frame)

                    with self._cond:
                        if len(self._frames) >= self.buffer_capacity:
                            self._frames.popleft()
                            self.dropped_frames += 1
                        self._frames.append(frame)
                        self._count += 1
                        self._cond.notify_all()
            except Exception:
                self.last_error = "exception while reading capture"
                log.exception("unexpected exception reading %s", _redact(self.source))
            finally:
                cap.release()

            if self.is_file:
                with self._cond:
                    self.eof = True
                    self._cond.notify_all()
                return
            if not self._stop.is_set():
                self.reconnects += 1
                log.warning("stream ended/dropped for %s; reconnecting in 1s", _redact(self.source))
                self._sleep_interruptibly(1.0)

    def read(self, last_idx=-1, timeout=1.0):
        """Wait for a frame newer than last_idx; return None on timeout/EOF/stop."""
        with self._cond:
            self._cond.wait_for(
                lambda: any(frame.idx > last_idx for frame in self._frames)
                or self.eof or self._stop.is_set(),
                timeout=max(0.0, timeout),
            )
            while self._frames and self._frames[0].idx <= last_idx:
                self._frames.popleft()
            if self._frames and self._frames[0].idx > last_idx:
                return self._frames.popleft()
            return None


    def _append_evidence(self, frame):
        """Store a bounded, compressed sample independently of scheduler reads."""
        if self.evidence_history_seconds <= 0:
            return

        if self._last_evidence_ts is not None:
            if frame.ts - self._last_evidence_ts < self.evidence_sample_interval_s:
                return

        image = frame.image
        height, width = image.shape[:2]

        if width > self.evidence_max_width:
            new_height = max(
                1, int(round(height * self.evidence_max_width / width))
            )
            image = cv2.resize(
                image,
                (self.evidence_max_width, new_height),
                interpolation=cv2.INTER_AREA,
            )

        ok, encoded = cv2.imencode(
            ".jpg",
            image,
            [cv2.IMWRITE_JPEG_QUALITY, self.evidence_jpeg_quality],
        )
        if not ok:
            self.evidence_encode_failures += 1
            return

        item = {
            "ts": float(frame.ts),
            "idx": int(frame.idx),
            "width": int(width),
            "height": int(height),
            "jpeg": encoded.tobytes(),
        }

        cutoff = frame.ts - self.evidence_history_seconds

        with self._evidence_lock:
            while (
                self._evidence_frames
                and self._evidence_frames[0]["ts"] < cutoff
            ):
                self._evidence_frames.popleft()
                self.evidence_evicted_frames += 1

            if len(self._evidence_frames) >= self._evidence_capacity:
                self._evidence_frames.popleft()
                self.evidence_evicted_frames += 1

            self._evidence_frames.append(item)
            self.evidence_frames_stored += 1
            self._last_evidence_ts = frame.ts

            # Append only frames not already included by event backfill.
            item_size = len(item["jpeg"])

            for event in self._evidence_events.values():
                if event["status"] != "CAPTURING":
                    continue

                if frame.ts < event["start_ts"]:
                    continue

                last_ts = event.get("last_frame_ts")
                if last_ts is not None and frame.ts <= last_ts:
                    continue

                if not event["overflow"]:
                    if event["bytes"] + item_size > self.evidence_event_max_bytes:
                        event["overflow"] = True
                        event["overflow_reason"] = "per_event_byte_limit"
                        self.evidence_event_overflows += 1

                    elif (
                        self._evidence_event_bytes + item_size
                        > self.evidence_event_total_max_bytes
                    ):
                        event["overflow"] = True
                        event["overflow_reason"] = "per_camera_total_byte_limit"
                        self.evidence_event_overflows += 1

                    else:
                        event["frames"].append(dict(item))
                        event["bytes"] += item_size
                        self._evidence_event_bytes += item_size

                event["last_frame_ts"] = frame.ts

                if frame.ts >= event["end_ts"]:
                    event["capture_complete"] = True
                    event["complete"] = (
                        not event["overflow"]
                        and event["pre_event_complete"]
                    )
                    if event["overflow"]:
                        event["status"] = "COMPLETE_WITH_OVERFLOW"
                    elif not event["pre_event_complete"]:
                        event["status"] = "INCOMPLETE_PRE_EVENT"
                    else:
                        event["status"] = "COMPLETE"

    def evidence_snapshot(self, start_ts=None, end_ts=None):
        """Return JPEG evidence records in the requested timestamp window."""
        with self._evidence_lock:
            records = list(self._evidence_frames)

        return [
            dict(item)
            for item in records
            if (start_ts is None or item["ts"] >= start_ts)
            and (end_ts is None or item["ts"] <= end_ts)
        ]


    def start_evidence_event(
        self,
        event_id,
        trigger_ts=None,
        pre_event_seconds=2.0,
        post_event_seconds=2.0,
    ):
        """Pin event history, backfill already-captured frames and collect future frames."""
        event_id = str(event_id).strip()
        if not event_id:
            raise ValueError("event_id must not be empty")

        pre_seconds = max(0.0, float(pre_event_seconds))
        post_seconds = max(0.0, float(post_event_seconds))

        with self._evidence_lock:
            if event_id in self._evidence_events:
                return {
                    "accepted": False,
                    "reason": "duplicate_event_id",
                    "event_id": event_id,
                }

            if len(self._evidence_events) >= self.evidence_event_max_count:
                self.evidence_events_rejected += 1
                return {
                    "accepted": False,
                    "reason": "event_count_limit",
                    "event_id": event_id,
                }

            if not self._evidence_frames and pre_seconds > 0.0:
                self.evidence_events_rejected += 1
                return {
                    "accepted": False,
                    "reason": "no_evidence_available",
                    "event_id": event_id,
                }

            if trigger_ts is None:
                trigger_ts = self._evidence_frames[-1]["ts"]

            trigger_ts = float(trigger_ts)
            start_ts = trigger_ts - pre_seconds
            end_ts = trigger_ts + post_seconds

            # Backfill both pre-trigger and already-recorded post-trigger frames.
            event_frames = [
                dict(item)
                for item in self._evidence_frames
                if start_ts <= item["ts"] <= end_ts
            ]
            event_frames.sort(key=lambda item: (item["ts"], item["idx"]))

            # Zero pre-event duration means no pre-event frames.
            # Keep the trigger frame separately as event evidence.
            pre_frames = (
                [
                    item for item in event_frames
                    if item["ts"] <= trigger_ts
                ]
                if pre_seconds > 0.0 else []
            )

            trigger_frames = [
                item for item in event_frames
                if item["ts"] == trigger_ts
            ]

            post_frames = [
                item for item in event_frames
                if item["ts"] > trigger_ts
            ]

            pre_coverage = (
                trigger_ts - pre_frames[0]["ts"]
                if pre_frames else 0.0
            )

            # Allow sampling tolerance at both ends of the requested window.
            tolerance = max(0.05, self.evidence_sample_interval_s * 1.5)

            pre_event_complete = (
                pre_seconds == 0.0
                or (
                    bool(pre_frames)
                    and pre_frames[0]["ts"] <= start_ts + tolerance
                    and trigger_ts - pre_frames[-1]["ts"] <= tolerance
                )
            )

            pre_bytes = sum(
                len(item["jpeg"]) for item in pre_frames
            )

            # Pre-event evidence must fit or the event cannot be accepted.
            if pre_bytes > self.evidence_event_max_bytes:
                self.evidence_events_rejected += 1
                return {
                    "accepted": False,
                    "reason": "pre_event_exceeds_event_byte_limit",
                    "event_id": event_id,
                    "pre_event_bytes": pre_bytes,
                }

            if (
                self._evidence_event_bytes + pre_bytes
                > self.evidence_event_total_max_bytes
            ):
                self.evidence_events_rejected += 1
                return {
                    "accepted": False,
                    "reason": "pre_event_exceeds_camera_byte_limit",
                    "event_id": event_id,
                    "pre_event_bytes": pre_bytes,
                }

            # Backfilled post-event frames use the same bounded overflow
            # policy as frames arriving after event creation.
            accepted_frames = list(pre_frames)
            event_bytes = pre_bytes
            overflow = False
            overflow_reason = None

            # Process the trigger frame and already-captured post-event frames
            # through the same bounded overflow policy.
            backfill_frames = (
                trigger_frames if pre_seconds == 0.0 else []
            ) + post_frames

            for item in backfill_frames:
                item_size = len(item["jpeg"])

                if event_bytes + item_size > self.evidence_event_max_bytes:
                    overflow = True
                    overflow_reason = "per_event_byte_limit"
                    break

                if (
                    self._evidence_event_bytes + event_bytes + item_size
                    > self.evidence_event_total_max_bytes
                ):
                    overflow = True
                    overflow_reason = "per_camera_total_byte_limit"
                    break

                accepted_frames.append(item)
                event_bytes += item_size

            # Completion must reflect frames actually accepted into
            # this event, not the camera's independent rolling buffer.
            last_frame_ts = (
                accepted_frames[-1]["ts"] if accepted_frames else None
            )

            capture_complete = (
                post_seconds == 0.0
                or (
                    last_frame_ts is not None
                    and last_frame_ts >= end_ts
                )
            )

            if capture_complete:
                if not pre_event_complete:
                    status = "INCOMPLETE_PRE_EVENT"
                else:
                    status = "COMPLETE"
            else:
                status = "CAPTURING"

            event = {
                "event_id": event_id,
                "trigger_ts": trigger_ts,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "pre_event_seconds": pre_seconds,
                "post_event_seconds": post_seconds,
                "pre_event_complete": pre_event_complete,
                "pre_coverage_seconds": max(0.0, pre_coverage),
                "frames": accepted_frames,
                "bytes": event_bytes,
                "last_frame_ts": last_frame_ts,
                "overflow": overflow,
                "overflow_reason": overflow_reason,
                "capture_complete": capture_complete,
                "complete": (
                    capture_complete
                    and pre_event_complete
                    and not overflow
                ),
                "status": (
                    "COMPLETE_WITH_OVERFLOW"
                    if capture_complete and overflow
                    else "INCOMPLETE_PRE_EVENT"
                    if capture_complete and not pre_event_complete
                    else "COMPLETE"
                    if capture_complete
                    else status
                ),
            }

            self._evidence_events[event_id] = event
            self._evidence_event_bytes += event_bytes

            return {
                "accepted": True,
                "event_id": event_id,
                "status": status,
                "pre_event_frames": len(pre_frames),
                "post_event_frames_backfilled": len(post_frames),
                "pre_event_bytes": pre_bytes,
                "event_bytes": event_bytes,
                "pre_event_complete": pre_event_complete,
                "pre_coverage_seconds": max(0.0, pre_coverage),
                "trigger_ts": trigger_ts,
                "end_ts": end_ts,
            }

    def get_evidence_event(self, event_id):
        """Return a copy of an event package, including its JPEG evidence."""
        with self._evidence_lock:
            event = self._evidence_events.get(str(event_id))
            if event is None:
                return None

            result = dict(event)
            result["frames"] = [
                dict(item) for item in event["frames"]
            ]
            return result

    def release_evidence_event(self, event_id):
        """Explicitly release a consumed event package and its memory budget."""
        with self._evidence_lock:
            event = self._evidence_events.pop(str(event_id), None)
            if event is None:
                return False

            self._evidence_event_bytes -= event["bytes"]
            self._evidence_event_bytes = max(0, self._evidence_event_bytes)
            return True

    def finalize_evidence_events(self, reason="capture_stopped"):
        """Mark unfinished event packages as interrupted, never complete."""
        with self._evidence_lock:
            for event in self._evidence_events.values():
                if event["status"] == "CAPTURING":
                    event["status"] = "INTERRUPTED"
                    event["capture_complete"] = False
                    event["complete"] = False
                    event["interrupted_reason"] = str(reason)


def _video_creation_epoch(path):
    """Return embedded video creation time as Unix epoch, or None."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format_tags=creation_time",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=8, check=True,
        )
        raw = result.stdout.strip().splitlines()
        if not raw:
            return None
        value = datetime.fromisoformat(raw[0].replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    except Exception as exc:
        log.warning("Video creation time unavailable for %s: %s", path, exc)
        return None


class SequentialFileSource:
    """Read every file frame in order for deterministic tuning runs."""

    def __init__(self, path, start_time=None):
        self.path = path
        self.cap = cv2.VideoCapture(path)
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 25.0
        self.base = start_time if start_time is not None else time.time()
        self.n = 0
        frame_count = self.cap.get(cv2.CAP_PROP_FRAME_COUNT)
        self.expected_frames = int(frame_count) if math.isfinite(frame_count) and frame_count > 0 else 0
        self.eof = False

    def start(self):
        if not self.cap.isOpened():
            self.cap.release()
            raise ProcessingError(Diagnostic(
                "VIDEO_SOURCE_OPEN_FAILED", "VIDEO_PROCESSING", "file capture", "open",
                "availability", "Could not open the input video.", False,
                "failed", "Released capture.", context={"file": self.path},
            ))
        return self

    def stop(self):
        self.cap.release()

    def read(self, last_idx=-1, timeout=1.0):
        try:
            ok, img = self.cap.read()
        except cv2.error as exc:
            self.eof = True
            raise self._decode_error() from exc
        if not ok or img is None:
            self.eof = True
            if self.n == 0 or self.n < self.expected_frames:
                raise self._decode_error()
            return None
        f = Frame(self.base + self.n / self.fps, img, self.n)
        self.n += 1
        return f

    def _decode_error(self):
        return ProcessingError(Diagnostic(
            "VIDEO_FRAME_READ_FAILED", "VIDEO_PROCESSING", "file capture", "decode",
            "decode", "The video stopped decoding before its expected end.",
            False, "partial" if self.n else "failed", "Stopped reading this source.",
            context={"file": self.path, "frame_id": self.n},
        ))


def open_source(cfg):
    src = cfg["source"]
    start = cfg.get("replay_start_time")
    is_file = isinstance(src, str) and os.path.isfile(src)
    if (isinstance(src, str) and not is_file and not src.isdecimal()
            and "://" not in src and not os.path.exists(src)):
        raise ProcessingError(Diagnostic(
            "VIDEO_SOURCE_NOT_FOUND", "VIDEO_PROCESSING", "capture", "locate source",
            "availability", "Configured local video or device does not exist.",
            False, "failed", "Skipped opening the missing source.",
            context={"camera_id": cfg.get("camera_id"), "file": src},
        ))
    if is_file and start is None:
        start = _video_creation_epoch(src)
    if is_file and cfg.get("replay") == "sync":
        return SequentialFileSource(src, start).start()
    return LatestFrameGrabber(
        src,
        paced=bool(cfg.get("replay") == "realtime"),
        start_time=start,
        buffer_capacity=cfg.get("frame_buffer_capacity", 1),
        evidence_history_seconds=cfg.get("evidence_history_seconds", 4.0),
        evidence_sample_interval_s=cfg.get("evidence_sample_interval_s", 0.1),
        evidence_max_width=cfg.get("evidence_max_width", 640),
        evidence_jpeg_quality=cfg.get("evidence_jpeg_quality", 65),
        evidence_event_max_count=cfg.get("evidence_event_max_count", 4),
        evidence_event_max_bytes=cfg.get(
            "evidence_event_max_bytes", 5 * 1024 * 1024
        ),
        evidence_event_total_max_bytes=cfg.get(
            "evidence_event_total_max_bytes", 12 * 1024 * 1024
        ),
    ).start()
