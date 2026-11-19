"""Pre-YOLO motion gating, async GPU ReID, and cross-camera hand-off.

Three small, independently testable pieces that sit around the existing
scheduler/pipeline instead of replacing it:

* MotionGate         - decides per camera whether a frame is worth sending to
                       YOLO (grayscale diff + Lucas-Kanade score already computed
                       by multicam._activity_score).
* AsyncGlobalReID    - OSNet + GlobalIdentityRegistry on a background thread
                       (aux GPU if present, otherwise shares the YOLO GPU only
                       while YOLO is idle). The main loop never blocks on it.
* HandoffCoordinator - decides whether a tripwire ENTRY/EXIT is a real gate
                       event or the same car moving between cameras.
"""
import logging
import queue
import threading
import time
from collections import deque

import numpy as np

log = logging.getLogger("flow")


# --------------------------------------------------------------------------
# 1. Pre-YOLO motion gate
# --------------------------------------------------------------------------
class MotionGate:
    """Hysteresis gate with safety heartbeats.

    ACTIVE  : motion seen within hold_s -> camera runs at its normal max_fps.
    IDLE    : no motion. Camera is only sampled at a heartbeat:
                * idle_track_heartbeat_s when it still has live tracks (must be
                  shorter than tracker max_age_s or parked cars lose their track
                  and a false PARK_END can follow),
                * idle_empty_heartbeat_s when the scene is empty (safety net
                  against missed motion).
    """

    def __init__(self, on_threshold=0.025, off_ratio=0.5, hold_s=3.0,
                 wake_frames=1, idle_track_heartbeat_s=0.5,
                 idle_empty_heartbeat_s=3.0, enabled=True):
        self.on = float(on_threshold)
        self.off = float(on_threshold) * float(off_ratio)
        self.hold_s = float(hold_s)
        self.wake_frames = max(1, int(wake_frames))
        self.track_hb = float(idle_track_heartbeat_s)
        self.empty_hb = float(idle_empty_heartbeat_s)
        self.enabled = bool(enabled)
        self.active_until = -1e18
        self._streak = 0
        self.skipped = 0
        self.admitted = 0
        self.last_reason = "init"

    @classmethod
    def from_cfg(cls, inference_cfg, tracker_max_age_s=2.0):
        g = (inference_cfg or {}).get("motion_gate", {}) or {}
        hb = float(g.get("idle_track_heartbeat_s", 0.5))
        hb = min(hb, max(0.1, 0.5 * float(tracker_max_age_s)))
        return cls(
            on_threshold=float(g.get("on_threshold",
                               (inference_cfg or {}).get("activity_threshold", 0.025))),
            off_ratio=float(g.get("off_ratio", 0.5)),
            hold_s=float(g.get("hold_s", 3.0)),
            wake_frames=int(g.get("wake_frames", 1)),
            idle_track_heartbeat_s=hb,
            idle_empty_heartbeat_s=float(g.get("idle_empty_heartbeat_s", 3.0)),
            enabled=bool(g.get("enabled", True)),
        )

    def observe(self, activity, now):
        """Feed the raw activity score of a newly captured frame."""
        if activity >= self.on:
            self._streak += 1
            if self._streak >= self.wake_frames:
                self.active_until = now + self.hold_s
        elif activity < self.off:
            self._streak = 0
        # between off and on: keep streak, do not extend hold (hysteresis)

    def allow(self, now, last_infer, has_tracks):
        """Return (run_yolo, reason). `last_infer` is monotonic time of last run."""
        if not self.enabled:
            self.admitted += 1
            self.last_reason = "gate_disabled"
            return True, "gate_disabled"
        if now <= self.active_until:
            self.admitted += 1
            self.last_reason = "motion"
            return True, "motion"
        hb = self.track_hb if has_tracks else self.empty_hb
        if now - last_infer >= hb:
            self.admitted += 1
            self.last_reason = "heartbeat_tracks" if has_tracks else "heartbeat_empty"
            return True, self.last_reason
        self.skipped += 1
        self.last_reason = "idle_skip"
        return False, "idle_skip"

    @property
    def active(self):
        return self.last_reason in ("motion",)


# --------------------------------------------------------------------------
# 2. GPU sharing + async ReID
# --------------------------------------------------------------------------
class GpuArbiter:
    """Lets background GPU work run only while YOLO is not running."""

    def __init__(self):
        self.yolo_busy = threading.Event()

    def yolo_enter(self):
        self.yolo_busy.set()

    def yolo_exit(self):
        self.yolo_busy.clear()

    def wait_free(self, timeout=0.25):
        t_end = time.monotonic() + timeout
        while self.yolo_busy.is_set() and time.monotonic() < t_end:
            time.sleep(0.002)
        return not self.yolo_busy.is_set()


def resolve_aux_device(yolo_device, requested="auto"):
    """Pick the device for ReID/other background models.

    Returns (device, shares_with_yolo). Kaggle's 2xT4: YOLO on cuda:0 ->
    background work on cuda:1, no contention. One GPU: share it, gated by
    GpuArbiter. No CUDA: cpu.
    """
    yolo_device = str(yolo_device or "cpu")
    if requested and requested != "auto":
        return str(requested), str(requested) == yolo_device
    if not yolo_device.startswith("cuda"):
        return "cpu", False
    try:
        import torch
        n = torch.cuda.device_count()
    except Exception:
        n = 1
    if n >= 2:
        yolo_idx = int(yolo_device.split(":")[1]) if ":" in yolo_device else 0
        return f"cuda:{1 - yolo_idx if yolo_idx in (0, 1) else 0}", False
    return yolo_device, True


class AsyncGlobalReID:
    """Non-blocking replacement for GlobalReIDManager.

    update() has the same signature, but only enqueues crops and returns the
    global IDs already resolved. Embedding + registry assignment run on a worker
    thread, which then notifies the HandoffCoordinator.
    """

    def __init__(self, embedder, registry, handoff=None, arbiter=None,
                 shares_gpu_with_yolo=False, embedding_interval_s=0.5,
                 queue_size=64, deterministic=False):
        self.embedder = embedder
        self.registry = registry
        self.handoff = handoff
        self.arbiter = arbiter
        self.shares = bool(shares_gpu_with_yolo)
        self.interval = float(embedding_interval_s)
        self.q = queue.Queue(maxsize=queue_size)
        self.local_to_global = {}
        self.last_submit = {}
        self.dropped = 0
        self.embedded = 0
        self.deterministic = bool(deterministic)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="reid-worker", daemon=True)
        self._thread.start()

    @staticmethod
    def _crop(frame, bbox):
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = map(int, bbox)
        x1, x2 = max(0, x1), min(w, x2)
        y1, y2 = max(0, y1), min(h, y2)
        if x2 - x1 < 16 or y2 - y1 < 16:
            return None
        return frame[y1:y2, x1:x2].copy()

    def update(self, camera_id, frame, tracks, timestamp=None):
        now = float(timestamp if timestamp is not None else time.time())
        result = {}
        for track in tracks:
            if not getattr(track, "confirmed", False):
                continue
            local_id = int(track.id)
            key = (str(camera_id), local_id)
            with self._lock:
                gid = self.local_to_global.get(key)
            if gid is not None:
                result[local_id] = gid
            # Keep refreshing the prototype / last_seen even after assignment.
            if now - self.last_submit.get(key, -1e18) < self.interval:
                continue
            crop = self._crop(frame, track.bbox)
            if crop is None:
                continue
            try:
                if self.deterministic:
                    self.q.put((str(camera_id), local_id, crop, now))
                else:
                    self.q.put_nowait((str(camera_id), local_id, crop, now))
                self.last_submit[key] = now
            except queue.Full:
                self.dropped += 1  # never block the scheduler
        if self.deterministic:
            self.wait_idle()  # assignments land before the next frame, every run
        return result

    def wait_idle(self, timeout=60.0):
        end = time.monotonic() + float(timeout)
        while self.q.unfinished_tasks:
            if not self._thread.is_alive():
                log.error("REID_WAIT_IDLE worker thread is not alive; %d crop(s) unfinished",
                          self.q.unfinished_tasks)
                return False
            if time.monotonic() > end:
                log.error("REID_WAIT_IDLE timed out after %.0fs", timeout)
                return False
            time.sleep(0.0005)
        return True

    def gid(self, camera_id, local_id):
        with self._lock:
            return self.local_to_global.get((str(camera_id), int(local_id)))

    def _run(self):
        # Shutdown is graceful: finish every crop already accepted into the
        # bounded queue before exiting. Otherwise the final tracks in a replay
        # can lose their global IDs simply because the video reached EOF.
        while not self._stop.is_set() or not self.q.empty():
            try:
                cam, local_id, crop, ts = self.q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if self.shares and self.arbiter is not None:
                    self.arbiter.wait_free(0.25)  # YOLO has priority
                emb = self.embedder.encode(crop)
                if emb is None:
                    continue
                emb = np.asarray(emb, dtype=np.float32).reshape(-1)
                if emb.size != 512 or not np.all(np.isfinite(emb)):
                    continue
                with self._lock:
                    a = self.registry.assign(cam, local_id, emb, ts)
                    gid = a.get("global_id")
                    if gid is not None:
                        self.local_to_global[(cam, local_id)] = gid
                self.embedded += 1
                if gid is not None and self.handoff is not None:
                    self.handoff.note_sighting(cam, local_id, gid, ts)
            except Exception:
                log.exception("ReID worker failed on a crop; continuing")
            finally:
                self.q.task_done()

    def summary(self):
        with self._lock:
            return self.registry.summary()

    def stop(self, timeout=15.0):
        self._stop.set()
        self._thread.join(timeout=max(0.1, float(timeout)))
        if self._thread.is_alive():
            log.error("ReID worker shutdown timed out with %d queued crops", self.q.qsize())
            return False
        return True


# --------------------------------------------------------------------------
# 3. Cross-camera hand-off
# --------------------------------------------------------------------------
EMIT, SUPPRESS, PENDING = "EMIT", "SUPPRESS", "PENDING"


class HandoffCoordinator:
    """Turn raw per-camera tripwire crossings into real gate events.

    * ENTRY is suppressed when the same global ID was already seen on another
      camera shortly before (TRANSFER_IN) or is already counted as inside.
    * EXIT is held for exit_wait_s; if the same global ID shows up on another
      camera after the crossing it is a TRANSFER_OUT, not an exit.
    * If the global ID is unknown when the wait expires, the event is emitted
      (fail open: never lose a real entry/exit because ReID was slow).
    All times are on the frame clock (frame.ts) that all cameras share.
    """

    def __init__(self, entry_wait_s=4.0, exit_wait_s=8.0, transfer_window_s=30.0):
        self.entry_wait_s = float(entry_wait_s)
        self.exit_wait_s = float(exit_wait_s)
        self.window_s = float(transfer_window_s)
        self._lock = threading.Lock()
        self.sightings = {}      # gid -> {cam: [first_ts, last_ts]}
        self.local_gid = {}      # (cam, local) -> gid
        self.inside = {}         # gid -> bool
        self.held = {}           # cam -> list of dict
        self.transfers = []      # audit trail for the report
        self.clock = {}          # cam -> latest frame ts seen

    @classmethod
    def from_cfg(cls, cfg):
        h = (cfg or {}).get("handoff", {}) or {}
        return cls(h.get("entry_wait_s", 4.0), h.get("exit_wait_s", 8.0),
                   h.get("transfer_window_s", 30.0))

    # ---- fed by the ReID worker -------------------------------------------
    def note_sighting(self, cam, local_id, gid, ts):
        with self._lock:
            self.local_gid[(cam, int(local_id))] = gid
            s = self.sightings.setdefault(gid, {}).setdefault(cam, [ts, ts])
            s[0], s[1] = min(s[0], ts), max(s[1], ts)

    # ---- fed by the pipeline ----------------------------------------------
    def hold(self, cam, local_id, etype, event_ts, now_ts, payload):
        with self._lock:
            self.held.setdefault(cam, []).append({
                "cam": cam, "local": int(local_id), "etype": etype,
                "event_ts": float(event_ts), "requested": float(now_ts),
                "payload": payload,
            })

    def poll(self, cam, now_ts, final=False):
        """Return [(item, verdict, reason)] for items of `cam` that resolved."""
        out, keep = [], []
        with self._lock:
            self.clock[cam] = max(self.clock.get(cam, -1e18), now_ts)
            for it in self.held.get(cam, []):
                verdict, reason = self._decide(it, now_ts, final)
                if verdict == PENDING:
                    keep.append(it)
                else:
                    out.append((it, verdict, reason))
                    self._apply(it, verdict, reason)
            self.held[cam] = keep
        return out

    def _gid(self, it):
        return self.local_gid.get((it["cam"], it["local"]))

    def _other_cams(self, gid, cam):
        return {c: v for c, v in self.sightings.get(gid, {}).items() if c != cam}

    def _decide(self, it, now_ts, final):
        gid = self._gid(it)
        waited = now_ts - it["requested"]
        others = self._other_cams(gid, it["cam"]) if gid else {}

        if it["etype"] == "ENTRY":
            if gid is None:
                if final or waited >= self.entry_wait_s:
                    return EMIT, "GID_UNKNOWN_FAIL_OPEN"
                return PENDING, ""
            came_from = [c for c, (f, l) in others.items()
                         if l >= it["event_ts"] - self.window_s and f <= it["event_ts"]]
            if came_from:
                return SUPPRESS, f"TRANSFER_IN from {sorted(came_from)}"
            if self.inside.get(gid):
                return SUPPRESS, "ALREADY_INSIDE"
            return EMIT, "NEW_ENTRY"

        # EXIT
        if gid is not None:
            went_to = [c for c, (f, l) in others.items()
                       if l > it["event_ts"] and f >= it["event_ts"] - 1.0]
            if went_to:
                return SUPPRESS, f"TRANSFER_OUT to {sorted(went_to)}"
        if final or waited >= self.exit_wait_s:
            return EMIT, "REAL_EXIT" if gid else "GID_UNKNOWN_FAIL_OPEN"
        return PENDING, ""

    def _apply(self, it, verdict, reason):
        gid = self._gid(it)
        if verdict == EMIT and gid is not None:
            self.inside[gid] = (it["etype"] == "ENTRY")
        if verdict == SUPPRESS:
            self.transfers.append({
                "camera_id": it["cam"], "local_track_id": it["local"],
                "global_id": gid, "raw_event": it["etype"],
                "event_ts": it["event_ts"], "reason": reason,
            })
            log.info("HANDOFF %s suppressed cam=%s track=%s gid=%s reason=%s",
                     it["etype"], it["cam"], it["local"], gid, reason)
