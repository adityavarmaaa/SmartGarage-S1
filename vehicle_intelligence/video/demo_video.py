"""Annotated multi-camera demo video (all cameras tiled side by side, one MP4).

Reads pipeline state only; it never changes detection, tracking or events. All
rendering is wrapped so a drawing error can never stop the pipeline.
"""
import logging
import math
import os
import shutil
import subprocess
import time
import tempfile
from collections import deque

import cv2
import numpy as np

from vehicle_intelligence.common.errors import Diagnostic

from vehicle_intelligence.camera_motion import transform_points

log = logging.getLogger("demo_video")
FONT = cv2.FONT_HERSHEY_SIMPLEX
GREEN, RED, CYAN, WHITE, YELLOW, ORANGE = (0, 220, 0), (0, 0, 255), (255, 200, 0), (255, 255, 255), (0, 255, 255), (0, 140, 255)
EVENT_COLOURS = {"ENTRY": GREEN, "EXIT": ORANGE, "PARK_START": YELLOW, "PARK_END": ORANGE,
                 "TRANSFER_IN": CYAN, "TRANSFER_OUT": CYAN}


def _hms(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def _label(img, lines, x, y, colour=GREEN, scale=0.5):
    """Dark-background text block whose bottom-left is near (x, y)."""
    h = int(22 * scale / 0.5)
    widths = [cv2.getTextSize(t, FONT, scale, 1)[0][0] for t in lines]
    w = (max(widths) if widths else 0) + 8
    top = y - h * len(lines)
    top = max(38, min(top, img.shape[0] - h * len(lines) - 2))  # keep clear of the header bar
    x = max(2, min(x, img.shape[1] - w - 2))
    cv2.rectangle(img, (x, top), (x + w, top + h * len(lines) + 4), (20, 20, 20), -1)
    for i, t in enumerate(lines):
        cv2.putText(img, t, (x + 4, top + h * (i + 1) - 4), FONT, scale, colour, 1, cv2.LINE_AA)


class DemoRecorder:
    def __init__(self, path, camera_ids, fps=10.0, tile_height=720, panel_height=170, frame_sink=None):
        self.path = path
        self.frame_sink = frame_sink
        self.fps = float(fps)
        self.interval = 1.0 / self.fps
        self.camera_ids = list(camera_ids)
        n = len(self.camera_ids)
        self.cols = max(1, math.ceil(math.sqrt(n))) if n > 2 else n
        self.rows = math.ceil(n / self.cols)
        self.tile_h = int(tile_height if n <= 2 else min(tile_height, 1080 // self.rows))
        self.panel_h = int(panel_height)
        self.events = deque(maxlen=7)
        self.writer = None
        self.size = None
        self.frames_written = 0
        self.last_write = 0.0
        self.t0 = None
        self.errors = 0
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self.raw_path = None
        self._closed_path = None

    # ---- events shown in the bottom panel ---------------------------------
    def add_event(self, cam, etype, detail, ts):
        self.events.append((ts, cam, etype, detail))

    # ---- per-camera tile --------------------------------------------------
    def _tile(self, rt, cam, ctx):
        raw = getattr(rt, "latest_raw", None)
        if raw is None:
            return np.zeros((self.tile_h, int(self.tile_h * 9 / 16), 3), np.uint8)
        h0, w0 = raw.shape[:2]
        s = self.tile_h / float(h0)
        img = cv2.resize(raw, (max(1, int(w0 * s)), self.tile_h), interpolation=cv2.INTER_AREA)
        p = rt.pipeline
        inv = cv2.invertAffineTransform(p.camera_transform)

        def sc(pts):
            return np.round(transform_points(pts, inv) * s).astype(np.int32)

        cv2.line(img, *map(tuple, sc(p.geo.tripwire)[:2].tolist()), YELLOW, 2)
        occ_by_track = {}
        for sid, o in p.slots.occupants.items():
            occ_by_track[o.track_id] = (sid, o)
        for slot in p.geo.slots:
            sid = slot["slot_id"]
            o = p.slots.occupants.get(sid)
            poly = sc(slot["polygon"])
            cv2.polylines(img, [poly], True, RED if o else GREEN, 2)
            txt = sid
            if o is not None:
                v = getattr(o, "vehicle", None)
                plate = getattr(v, "emitted_plate", None) if v else None
                txt = f"{sid} {plate or ''}".strip()
            cv2.putText(img, txt, tuple(poly[0].tolist()), FONT, 0.5, RED if o else GREEN, 1, cv2.LINE_AA)

        reid, handoff = ctx.get("reid"), ctx.get("handoff")
        now_ts = getattr(p, "display_ts", 0.0)
        for t in p.last_tracks:
            x1, y1, x2, y2 = [int(c * s) for c in t.bbox]
            v = p.registry.by_track.get(t.id)
            cv2.rectangle(img, (x1, y1), (x2, y2), CYAN, 2)
            gid = reid.gid(cam, t.id) if reid is not None and hasattr(reid, "gid") else None
            plate, prov = None, None
            if v is not None:
                try:
                    c = v.voter.consensus()
                    if c is not None and c.valid:
                        if p._plate_ready(v):
                            plate = c.text
                        else:
                            prov = c.text
                except Exception:
                    pass
            colour = (v.color() if v is not None else None) or "?"
            sid, o = occ_by_track.get(t.id, (None, None))
            if o is not None:
                if getattr(o, "baseline", False):
                    parked = "parked before video start"
                elif o.parked_at is None:
                    parked = "occupied (entry unverified)"
                else:
                    st = getattr(p.slots, "stream_start_ts", None) or o.parked_at
                    parked = f"parked @+{_hms(o.parked_at - st)} ({_hms(now_ts - o.parked_at)})"
            else:
                parked = "moving"
            src = None
            if handoff is not None and gid is not None:
                for tr in reversed(handoff.transfers):
                    if tr["global_id"] == gid and tr["camera_id"] == cam and tr["raw_event"] == "ENTRY":
                        src = tr["reason"].replace("TRANSFER_IN from ", "from ")
                        break
            lines = [f"{gid or 'G-?'}  (local #{t.id})",
                     f"Plate: {plate or ('~' + prov if prov else 'PLATE?')}",
                     f"Colour: {colour}   Slot: {sid or '-'}",
                     parked]
            if src:
                lines.insert(1, f"CAME {src}")
            _label(img, lines, x1, y1 - 4)

        # header
        occ, tot = len(p.slots.occupants), len(p.geo.slots)
        gate = getattr(rt, "gate", None)
        state = {"motion": "ACTIVE", "idle_skip": "IDLE", "heartbeat_tracks": "IDLE(hb)",
                 "heartbeat_empty": "IDLE(hb)"}.get(getattr(gate, "last_reason", ""), "-")
        cv2.rectangle(img, (0, 0), (img.shape[1], 34), (20, 20, 20), -1)
        cv2.putText(img, f"{cam}  slots {occ}/{tot}  free {tot - occ}  motion:{state}  "
                         f"yolo:{rt.processed_frames} skip:{getattr(rt, 'skipped_idle', 0)}",
                    (8, 23), FONT, 0.55, WHITE, 1, cv2.LINE_AA)
        return img

    # ---- composite --------------------------------------------------------
    def tick(self, runtimes, ctx, now_mono, force=False):
        if not force and now_mono - self.last_write < self.interval:
            return
        self.last_write = now_mono
        try:
            tiles = [self._tile(runtimes[c], c, ctx) for c in self.camera_ids if c in runtimes]
            if not tiles:
                return
            tw = max(t.shape[1] for t in tiles)
            grid_rows = []
            for r in range(self.rows):
                row = tiles[r * self.cols:(r + 1) * self.cols]
                row = [cv2.copyMakeBorder(t, 0, 0, 0, tw - t.shape[1], cv2.BORDER_CONSTANT) for t in row]
                while len(row) < self.cols:
                    row.append(np.zeros((self.tile_h, tw, 3), np.uint8))
                grid_rows.append(np.hstack(row))
            comp = np.vstack(grid_rows)
            panel = np.full((self.panel_h, comp.shape[1], 3), 25, np.uint8)
            cv2.putText(panel, "EVENTS (video time)  |  ENTRY / EXIT / PARK_START / PARK_END / TRANSFER (camera hand-off)",
                        (8, 20), FONT, 0.55, WHITE, 1, cv2.LINE_AA)
            if self.t0 is None and self.events:
                self.t0 = self.events[0][0]
            for i, (ts, cam, etype, detail) in enumerate(reversed(self.events)):
                rel = _hms(ts - (self.t0 if self.t0 is not None else ts))
                cv2.putText(panel, f"{rel}  {cam}  {etype}  {detail}", (8, 46 + 20 * i), FONT, 0.55,
                            EVENT_COLOURS.get(etype, WHITE), 1, cv2.LINE_AA)
            frame = np.vstack([comp, panel])
            if self.writer is None:
                h, w = frame.shape[:2]
                w, h = w - w % 2, h - h % 2
                self.size = (w, h)
                if self.raw_path is None:
                    self.raw_path = self._temp_video_path()
                self.writer = cv2.VideoWriter(self.raw_path, cv2.VideoWriter_fourcc(*"mp4v"), self.fps, self.size)
                if not self.writer.isOpened():
                    self.writer.release()
                    self.writer = None
                    raise RuntimeError("cv2.VideoWriter could not open " + self.raw_path)
            frame = frame[:self.size[1], :self.size[0]]
            self.writer.write(frame)
            self.frames_written += 1
            if self.frame_sink is not None:
                self._publish_preview(frame)
        except Exception:
            self.errors += 1
            if self.errors <= 3:
                Diagnostic(
                    "VIDEO_OUTPUT_FRAME_FAILED", "VIDEO_PROCESSING", "demo recorder", "render",
                    "output", "Could not render or write a video frame.", True, "partial",
                    "Continued processing without this output frame.",
                    context={"frames_written": self.frames_written},
                ).log(log, exc_info=True)

    def _publish_preview(self, frame):
        try:
            ok, jpeg = cv2.imencode(".jpg", frame)
            if ok:
                self.frame_sink(jpeg.tobytes())
            else:
                log.warning("VIDEO_PREVIEW_ENCODE_FAILED")
        except Exception:
            log.exception("VIDEO_PREVIEW_FAILED recording continues")
            self.frame_sink = None

    def _temp_video_path(self):
        descriptor, path = tempfile.mkstemp(suffix=".mp4", dir=os.path.dirname(os.path.abspath(self.path)))
        os.close(descriptor)
        return path

    @staticmethod
    def _playable(path):
        capture = cv2.VideoCapture(path)
        try:
            ok, frame = capture.read()
            return ok and frame is not None
        finally:
            capture.release()

    def _transcode(self, ffmpeg, raw, converted, cancel_event):
        command = [ffmpeg, "-y", "-loglevel", "error", "-i", raw,
                   "-vcodec", "libx264", "-pix_fmt", "yuv420p",
                   "-movflags", "+faststart", converted]
        with subprocess.Popen(command) as process:
            deadline = time.monotonic() + 600
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    if process.poll() is None:
                        process.kill()
                    return False
                try:
                    status = process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        process.kill()
                        raise subprocess.TimeoutExpired(command, 600)
                else:
                    if status:
                        raise subprocess.CalledProcessError(status, command)
                    return True

    def close(self, cancel_event=None):
        if self.raw_path is None:
            return self._closed_path
        raw, self.raw_path = self.raw_path, None
        converted = None
        try:
            if self.writer is not None:
                self.writer.release()
                self.writer = None
            if not self.frames_written or not self._playable(raw):
                return None
            candidate = raw
            ffmpeg = shutil.which("ffmpeg")
            if ffmpeg and (cancel_event is None or not cancel_event.is_set()):
                try:
                    converted = self._temp_video_path()
                    finished = self._transcode(ffmpeg, raw, converted, cancel_event)
                    if finished and self._playable(converted):
                        candidate = converted
                    elif finished:
                        log.warning("VIDEO_TRANSCODE_INVALID keeping playable MP4 fallback")
                except (OSError, subprocess.SubprocessError):
                    log.exception("VIDEO_TRANSCODE_FAILED keeping playable MP4 fallback")
            os.replace(candidate, self.path)
            self._closed_path = self.path
            return self._closed_path
        finally:
            for path in (raw, converted):
                if path is not None:
                    try:
                        os.unlink(path)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        log.exception("VIDEO_TEMP_CLEANUP_FAILED")
