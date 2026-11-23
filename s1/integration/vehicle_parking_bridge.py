import base64
import logging
from s1.common.errors import Diagnostic
from s1.common.data_models import usable_frame
from numbers import Integral
import os
import time
import uuid
from collections import Counter, deque

import cv2
import numpy as np

from s1.attributes import dominant_color_details
from s1.events import build_event
from s1.events.event_validation import validate_event
from s1.logic import SlotManager, Tripwire, still_run_start
from s1.camera_motion import CameraMotionEstimator, transform_points
from s1.plates import PlateVoter, crop_quality
from s1.plate_crop import cut_plate
from s1.plates import fix_indian
from s1.tracker import ByteTracker
from s1.vehicles import Vehicle, VehicleRegistry

log = logging.getLogger("pipeline")

def _plate_crop_rank(img):
    """Rank a vehicle crop by measurable image quality.

    This does not claim that a crop contains a readable plate.
    It only helps retain the better of two observed vehicle crops.
    """
    if img is None or not getattr(img, "size", 0):
        return (-1.0, -1.0, -1.0, -1)

    try:
        gray = (
            cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            if len(img.shape) == 3 else img
        )
        brightness = float(np.mean(gray))
        contrast = float(np.std(gray))
        sharpness = float(cv2.Laplacian(
            gray, cv2.CV_64F
        ).var())
        area = int(img.shape[0] * img.shape[1])

        # Exposure penalty is lower when brightness is near 128.
        exposure_score = -abs(brightness - 128.0)

        return (
            sharpness,
            contrast,
            exposure_score,
            area,
        )
    except Exception:
        return (-1.0, -1.0, -1.0, -1)


LIFECYCLE_EVENT_TYPES = frozenset({
    "ENTRY",
    "PARK_START",
    "PARK_END",
    "EXIT",
})


class Stats:
    def __init__(self, n=200):
        self.d = {k: deque(maxlen=n) for k in ("detect", "track", "total", "e2e")}
        self.frames = 0
        self.t0 = time.time()

    def add(self, k, v):
        self.d[k].append(v)

    def summary(self):
        def p(k, q):
            a = self.d[k]
            return float(np.percentile(a, q)) if a else 0.0

        fps = self.frames / max(time.time() - self.t0, 1e-6)
        return (
            f"fps={fps:4.1f} detect={p('detect', 50):5.0f}ms track={p('track', 50):4.1f}ms "
            f"total p50={p('total', 50):5.0f} p95={p('total', 95):5.0f}ms "
            f"capture->event-ready p50={p('e2e', 50):5.0f} p95={p('e2e', 95):5.0f}ms"
        )


class Pipeline:
    def __init__(
        self,
        cfg,
        geometry,
        frame_size,
        detector,
        publisher,
        plate_worker=None,
        now_fn=time.time,
        evidence_event_callback=None,
        handoff=None,
    ):
        self.handoff = handoff
        self.cfg, self.cam = cfg, cfg["camera_id"]
        self.now = now_fn
        # Deterministic file replay: every deadline is measured on the VIDEO
        # clock (the timestamp of the frame being processed), never on the
        # wall clock, so a faster or slower machine cannot change the output.
        self.deterministic = bool(cfg.get("deterministic", False))
        if self.deterministic:
            self.now = lambda: float(getattr(self, "display_ts", 0.0))
        self.detector, self.publisher, self.plates = detector, publisher, plate_worker
        self.evidence_event_callback = evidence_event_callback

        # MOG2 motion gate: independent background model per camera.
        gate_cfg = cfg.get("motion_gate", {})
        self.motion_gate_enabled = bool(gate_cfg.get("enabled", False))
        self.motion_gate_min_pixels = int(gate_cfg.get("min_motion_pixels", 500))
        self.motion_gate_force_every = max(
            1, int(gate_cfg.get("force_yolo_every_n_frames", 10))
        )
        self.motion_gate_frame_count = 0
        self.motion_gate = (
            cv2.createBackgroundSubtractorMOG2(
                history=int(gate_cfg.get("history", 300)),
                varThreshold=float(gate_cfg.get("var_threshold", 25)),
                detectShadows=True,
            )
            if self.motion_gate_enabled else None
        )

        t = cfg["tracker"]
        self.tracker = ByteTracker(
            t["high_thr"],
            t["low_thr"],
            t["match_iou"],
            t["low_match_iou"],
            t["max_age_s"],
            t["min_hits"],
        )

        g = geometry.scaled(*frame_size)
        self.geo = g
        motion_cfg = cfg.get("camera_motion", {})
        self.camera_motion_enabled = bool(motion_cfg.get("enabled", False))

        self.camera_motion = CameraMotionEstimator(
            diagnostics_path=motion_cfg.get(
                "diagnostics_path", "data/camera_motion_diagnostics.csv"
            ),
            max_dimension=motion_cfg.get("max_dimension", 800),
            min_inliers=motion_cfg.get("min_inliers", 8),
            max_translation_fraction=motion_cfg.get("max_translation_fraction", 0.35),
            max_rotation_deg=motion_cfg.get("max_rotation_deg", 20.0),
            min_scale=motion_cfg.get("min_scale", 0.65),
            max_scale=motion_cfg.get("max_scale", 1.50),
            smoothing=motion_cfg.get("smoothing", 0.60),
        )
        self.camera_transform = np.array(
            [[1., 0., 0.], [0., 1., 0.]], dtype=np.float32
        )

        self.tripwire = Tripwire(
            g.tripwire[0],
            g.tripwire[1],
            g.inside_point,
            cfg["gate"]["margin_px"],
            size_samples=cfg["gate"]["size_samples"],
            min_size_change=cfg["gate"]["min_size_change"],
        )

        self.slots = SlotManager(g.slots, frame_size, cfg["parking"])

        self.zone = (
            np.array(g.plate_zone, np.int32).reshape(-1, 1, 2)
            if g.plate_zone
            else None
        )


        self.registry = VehicleRegistry()
        self._entry_tripwire_handoffs = {}
        # Preserve genuine tripwire crossings while a track is still tentative.
        self._pending_tripwire_crossings = {}

        # Experimental OSNet observations; no cross-camera identity decisions here.
        from pathlib import Path
        from s1.reid import OSNetEmbedder
        reid_model = Path(__file__).resolve().parents[2] / "models" / "osnet_ain_x1_0_vehicle_reid.onnx"
        # With a shared HandoffCoordinator the multicam runner owns ReID (async,
        # GPU). Do not load a second blocking CPU OSNet per camera.
        self.reid_embedder = None
        if handoff is None and cfg.get("global_reid", {}).get("enabled", True) and reid_model.is_file():
            try:
                self.reid_embedder = OSNetEmbedder(reid_model)
            except Exception:
                Diagnostic(
                    "REID_MODEL_LOAD_FAILED", "VEHICLE_TRACKING", "OSNet", "initialize",
                    "initialization", "The optional identity model could not load.", True,
                    "partial", "Continued with camera-local tracking.",
                    context={"camera_id": self.cam},
                ).log(log, exc_info=True)
        self.reid_last_ts = {}
        self.reid_observations = {}

        self.stats = Stats()
        self.snap_dir = cfg["publisher"]["snapshot_dir"]
        self.last_tracks = []

        # All four lifecycle events remain pending until a finalized,
        # valid plate is available.
        self.pending_parking_events = []

        # Maps stable event IDs to their durable pending-event keys.
        self._durable_pending_keys = {}

        self._restore_pending_parking_events()

    # ------------------------------------------------------------------
    # Durable pending-event serialization and recovery
    # ------------------------------------------------------------------

    @staticmethod
    def _encode_crop(crop):
        if crop is None:
            return None

        try:
            ok, encoded = cv2.imencode(
                ".jpg",
                crop,
                [int(cv2.IMWRITE_JPEG_QUALITY), 85],
            )
            if not ok:
                return None
            return base64.b64encode(encoded.tobytes()).decode("ascii")
        except Exception:
            log.exception("Could not encode crop for pending-event storage")
            return None

    @staticmethod
    def _decode_crop(encoded):
        if not encoded:
            return None

        try:
            raw = base64.b64decode(encoded, validate=True)
            array = np.frombuffer(raw, dtype=np.uint8)
            return cv2.imdecode(array, cv2.IMREAD_COLOR)
        except Exception:
            log.exception("Could not restore crop for pending parking event")
            return None

    def _serialize_pending(self, pending):
        v = pending["vehicle"]

        return {
            "event_id": pending["event_id"],
            "event_type": pending["event_type"],
            "event_time": pending["event_time"],
            "detected_at": pending["detected_at"],
            "received_at": pending.get("received_at"),
            "captured_at": pending.get("captured_at"),
            "direction": pending["direction"],
            "slot": pending["slot"],
            "inferred": pending["inferred"],
            "created_at": pending["created_at"],
            "deadline": pending["deadline"],
            "timed_out": pending["timed_out"],
            "recovery_crop_retry": pending.get("recovery_crop_retry", False),
            "recovery_retry_due": pending.get("recovery_retry_due", False),
            "vehicle": {
                "uid": v.uid,
                "track_id": v.track_id,
                "first_ts": v.first_ts,
                "cls_votes": list(v.cls_votes.items()),
                "color_votes": list(v.color_votes.items()),
                "plate_reads": [list(read) for read in v.voter.reads],
                "plate_max_reads": v.voter.max_reads,
                "plate_final": v.plate_final,
                "plate_attempts": v.plate_attempts,
                "plate_inflight": v.plate_inflight,
                "last_plate_submit": v.last_plate_submit,
                "last_color_ts": v.last_color_ts,
                "last_crop": self._encode_crop(v.last_crop),
                "any_emitted": v.any_emitted,
                "emitted_plate": v.emitted_plate,
                "slot_id": v.slot_id,
            },
        }

    def _deserialize_pending(self, payload):
        if payload.get("event_type") not in LIFECYCLE_EVENT_TYPES:
            raise ValueError("Unsupported pending lifecycle event type")

        if not payload.get("event_id"):
            raise ValueError("Pending event has no stable event_id")

        vehicle_data = payload["vehicle"]

        v = Vehicle(
            uid=vehicle_data["uid"],
            track_id=int(vehicle_data["track_id"]),
            first_ts=float(vehicle_data["first_ts"]),
        )

        v.cls_votes = Counter(
            {
                int(key): int(value)
                for key, value in vehicle_data.get("cls_votes", [])
            }
        )
        v.color_votes = Counter(
            {
                str(key): int(value)
                for key, value in vehicle_data.get("color_votes", [])
            }
        )

        voter = PlateVoter()
        voter.max_reads = int(
            vehicle_data.get("plate_max_reads", voter.max_reads)
        )
        voter.reads = [
            (str(read[0]), float(read[1]), bool(read[2]))
            for read in vehicle_data.get("plate_reads", [])
        ]
        v.voter = voter

        v.plate_final = bool(vehicle_data.get("plate_final", False))
        v.plate_attempts = int(vehicle_data.get("plate_attempts", 0))
        v.last_plate_submit = float(
            vehicle_data.get("last_plate_submit", 0.0)
        )
        v.last_color_ts = float(vehicle_data.get("last_color_ts", 0.0))

        saved_inflight = bool(vehicle_data.get("plate_inflight", False))

        # OCR jobs from the previous process no longer exist.
        v.plate_inflight = False
        v.last_crop = self._decode_crop(vehicle_data.get("last_crop"))

        # An interrupted OCR job consumed an attempt but never delivered its
        # result. Credit that attempt back so the existing retry limit remains
        # useful after a restart.
        if saved_inflight and not v.plate_final:
            v.plate_attempts = max(0, v.plate_attempts - 1)

        can_retry_crop = bool(v.last_crop is not None and not v.plate_final)

        v.any_emitted = bool(vehicle_data.get("any_emitted", False))
        v.emitted_plate = vehicle_data.get("emitted_plate")
        v.slot_id = vehicle_data.get("slot_id")

        return {
            "event_id": payload["event_id"],
            "event_type": payload["event_type"],
            "vehicle": v,
            "event_time": float(payload["event_time"]),
            "detected_at": float(payload["detected_at"]),
            "direction": payload.get("direction"),
            "slot": payload.get("slot"),
            "inferred": bool(payload.get("inferred", False)),
            "created_at": float(payload["created_at"]),
            "deadline": float(payload["deadline"]),
            "timed_out": bool(payload.get("timed_out", False)),
            "recovery_crop_retry": bool(
                can_retry_crop
                and (
                    saved_inflight
                    or payload.get("recovery_crop_retry", False)
                    or v.last_crop is not None
                )
            ),
            "recovery_retry_due": bool(can_retry_crop),
        }

    def _save_pending_event(self, pending):
        save = getattr(self.publisher, "save_pending_event", None)
        if not callable(save):
            log.error(
                "Publisher does not support durable pending-event storage; "
                "event=%s remains memory-only",
                pending.get("event_id"),
            )
            return False

        event_key = pending["event_id"]

        try:
            save(event_key, self._serialize_pending(pending))
            self._durable_pending_keys[event_key] = event_key
            return True
        except Exception:
            log.exception(
                "Could not persist pending lifecycle event id=%s",
                event_key,
            )
            return False

    def _persist_all_pending_events(self):
        for pending in self.pending_parking_events:
            self._save_pending_event(pending)

    def _restore_pending_parking_events(self):
        load = getattr(self.publisher, "load_pending_events", None)
        if not callable(load):
            log.warning(
                "Publisher has no pending-event recovery interface"
            )
            return

        try:
            saved_events = load()
        except Exception:
            log.exception("Could not load durable pending lifecycle events")
            return

        for event_key, payload in saved_events:
            try:
                pending = self._deserialize_pending(payload)

                if event_key != pending["event_id"]:
                    raise ValueError(
                        "Stored event key does not match event_id"
                    )

                if not pending["vehicle"].uid:
                    raise ValueError("Pending event has no vehicle UID")

                self.pending_parking_events.append(pending)
                self._durable_pending_keys[event_key] = event_key

                # Keep OCR results addressable while this event is pending.
                self.registry.by_uid[pending["vehicle"].uid] = pending["vehicle"]

                log.info(
                    "Restored pending %s event_id=%s vehicle=%s "
                    "original_event_time=%s crop_retry=%s",
                    pending["event_type"],
                    event_key,
                    pending["vehicle"].uid,
                    pending["event_time"],
                    pending["recovery_crop_retry"],
                )

            except Exception:
                # Leave invalid rows in SQLite for inspection/recovery.
                log.exception(
                    "Could not restore pending event key=%s; "
                    "the database row has been retained",
                    event_key,
                )

        log.info(
            "Pending-event recovery loaded %d event(s)",
            len(self.pending_parking_events),
        )

    def _publish_event(self, event):
        """Persist to the outgoing outbox before deleting its pending row."""
        try:
            validate_event(event)
        except (TypeError, ValueError):
            Diagnostic(
                "EVENT_VALIDATION_FAILED", "EVENT_VALIDATION", "event envelope", "validate",
                "validation", "Rejected a malformed event.", True, "failed",
                "Skipped publication for this event.",
                context={"camera_id": self.cam, "event_type": event.get("event_type"),
                         "event_id": event.get("event_id")},
            ).log(log, exc_info=True)
            return False
        event_id = event["event_id"]
        pending_key = self._durable_pending_keys.get(event_id)

        # Outbox.publish commits to SQLite before returning.
        self.publisher.publish(event)

        if pending_key is None:
            return True

        delete = getattr(self.publisher, "delete_pending_event", None)
        if not callable(delete):
            log.error(
                "Outgoing event %s was queued, but pending storage "
                "cannot delete its row; it will be recovered again",
                event_id,
            )
            return True

        try:
            delete(pending_key)
            self._durable_pending_keys.pop(event_id, None)
        except Exception:
            # The stable event ID makes a later outbox INSERT OR IGNORE safe.
            log.exception(
                "Outgoing event %s was queued, but pending-row deletion failed",
                event_id,
            )
        return True

    # ------------------------------------------------------------------
    # Snapshots and event construction
    # ------------------------------------------------------------------

    def _snapshot(self, v, etype, ts):
        return self._write_snapshot(v, etype, ts, v.last_crop)

    def _write_snapshot(self, v, etype, ts, crop, suffix=""):
        if crop is None or not self.snap_dir:
            return None
        directory = os.path.join(self.snap_dir, time.strftime("%Y%m%d", time.gmtime(ts)))
        path = os.path.join(directory, f"{v.uid[:8]}_{etype}_{int(ts * 1000)}{suffix}.jpg")
        try:
            os.makedirs(directory, exist_ok=True)
            if not cv2.imwrite(path, crop):
                raise OSError("Image writer did not save evidence")
        except (OSError, cv2.error):
            Diagnostic(
                "SNAPSHOT_WRITE_FAILED", "REPORTING", "snapshot", "write",
                "output", "Could not save event evidence.", True, "partial",
                "Continued with a null snapshot URI.",
                context={"camera_id": self.cam, "vehicle_uid": v.uid, "event_type": etype},
            ).log(log, exc_info=True)
            return None
        return path

    def _plate_snapshot(self, v, etype, ts):
        _c = v.voter.consensus() if getattr(v, "plate_final", False) else None
        _crops = getattr(v, "plate_crops", None) or {}
        crop = None
        if _c and _crops:
            if _c.text in _crops:
                crop = _crops[_c.text][1]
            else:
                crop = max(_crops.values(), key=lambda t: t[0])[1]
        return self._write_snapshot(v, etype, ts, crop, suffix="_plate")

    def _plate_ready(self, v):
        """Require the configured finalized plate consensus before releasing lifecycle events."""
        pc = self.cfg["plate"]
        return v.voter.is_final(
            pc["confirm_reads"],
            pc["confirm_conf"],
        )

    def _build_and_append(self, out, pending):
        """Build an event while preserving its original time and stable ID."""
        v = pending["vehicle"]

        # Recompute confirmation before build_event reads vehicle.plate_final.
        # Restored/stale flags are not enough; the current vote set must still
        # satisfy the configured plate consensus policy.
        cand = v.voter.consensus()
        confirmed = self._plate_ready(v)
        v.plate_final = bool(confirmed and cand is not None and cand.valid)
        c = cand if v.plate_final else None

        ev = build_event(
            pending["event_type"],
            v,
            self.cam,
            pending["event_time"],
            pending["detected_at"],
            pending["direction"],
            pending["slot"],
            self._snapshot(
                v,
                pending["event_type"],
                pending["event_time"],
            ),
            pending["inferred"],
            captured_at=pending.get("captured_at"),
            received_at=pending.get("received_at"),
            parking_area_id=self.cfg["parking_area_id"],
            plate_snapshot_uri=self._plate_snapshot(
                v,
                pending["event_type"],
                pending["event_time"],
            ),
        )

        # Reuse the durable ID if a process restarts before publication.
        ev["event_id"] = pending["event_id"]

        # A cached flag or inherited identity is not sufficient evidence to
        # populate the registration field.
        v.emitted_plate = c.text if c else None
        ev["plate_status"] = "CONFIRMED" if c else "UNCONFIRMED"
        # An unconfirmed OCR candidate is diagnostic only; never put it in the
        # canonical plate field or silently borrow another vehicle's plate.
        ev["plate_candidate"] = cand.text if (cand and cand.valid and not c) else None

        log.info("EVENT_PLATE type=%s status=%s", pending["event_type"], ev["plate_status"])
        v.any_emitted = True
        out.append(ev)
        log.info(
            "Released %s event_id=%s vehicle=%s original_event_time=%s",
            pending["event_type"], pending["event_id"], v.uid, ev["timestamp"],
        )

    def _emit(
        self,
        out,
        etype,
        v,
        event_time,
        direction=None,
        sid=None,
        inferred=False,
        received_at=None,
        captured_at=None,
        _handoff_checked=False,
        _detected_at=None,
    ):
        if _detected_at is not None:
            detected_at = _detected_at
        else:
            now_fn = getattr(self, "now", None)
            detected_at = now_fn() if callable(now_fn) else event_time

        # Preserve the last occupied slot when a vehicle exits after PARK_END.
        # SlotManager may clear v.slot_id before the tripwire/handoff EXIT.
        if etype == "EXIT" and sid is None:
            sid = (
                getattr(v, "last_parked_slot", None)
                or getattr(v, "slot_id", None)
            )

        if etype == "ENTRY":
            log.info(
                "ENTRY_CROSSING_DIAG track=%s vehicle=%s crossing_ts=%.3f direction=%s handoff=%s checked=%s",
                getattr(v, "track_id", None), getattr(v, "uid", None),
                float(event_time), direction,
                getattr(self, "handoff", None) is not None, _handoff_checked,
            )

        if (
            getattr(self, "handoff", None) is not None
            and not _handoff_checked
            and etype in ("ENTRY", "EXIT")
        ):
            # Hold raw tripwire crossings until the cross-camera decision is
            # made; _release_handoff() re-emits real ones with the ORIGINAL
            # crossing timestamp.
            self.handoff.hold(
                self.cam, v.track_id, etype, event_time,
                getattr(self, "display_ts", event_time),
                dict(direction=direction, sid=sid, inferred=inferred,
                     received_at=received_at, captured_at=captured_at,
                     detected_at=detected_at, vehicle=v),
            )
            return

        # Authorize PARK_START only after the handoff decision accepts ENTRY.
        if etype == "ENTRY":
            v.entry_crossing_ts = float(event_time)
            log.info(
                "ENTRY_AUTHORIZED track=%s vehicle=%s entry_crossing_ts=%.3f",
                getattr(v, "track_id", None), getattr(v, "uid", None),
                float(v.entry_crossing_ts),
            )

        slot = self.slots.slot_info(sid) if sid else None

        pending = {
            "event_id": uuid.uuid4().hex,
            "event_type": etype,
            "vehicle": v,
            "event_time": event_time,
            "detected_at": detected_at,
            "received_at": received_at,
            "captured_at": captured_at,
            "direction": direction,
            "slot": slot,
            "inferred": inferred,
            "created_at": detected_at,
            "deadline": (
                detected_at
                + max(
                    0.0,
                    float(self.cfg["plate"].get("emit_deadline_s", 3.0)),
                )
            ),
            "timed_out": False,
            "recovery_crop_retry": False,
            "recovery_retry_due": False,
        }

        if etype in LIFECYCLE_EVENT_TYPES:
            # Persist before waiting for OCR or attempting publication.
            self.pending_parking_events.append(pending)
            self._save_pending_event(pending)

            # Start evidence capture at the original lifecycle-event time,
            # before OCR confirmation can delay publication.
            if self.evidence_event_callback is not None:
                try:
                    result = self.evidence_event_callback(
                        pending["event_id"],
                        pending["event_time"],
                    )
                    if result and not result.get("accepted", False):
                        log.warning(
                            "EVIDENCE_EVENT_REJECTED event_id=%s reason=%s",
                            pending["event_id"],
                            result.get("reason", "unknown"),
                        )
                except Exception:
                    log.exception(
                        "EVIDENCE_EVENT_START_FAILED event_id=%s",
                        pending["event_id"],
                    )

            log.info(
                "Holding %s event_id=%s vehicle=%s original_event_time=%s "
                "plate_ready=%s deadline=%.3f",
                etype,
                pending["event_id"],
                v.uid,
                event_time,
                self._plate_ready(v),
                pending["deadline"],
            )
            return

        # Non-lifecycle events retain their existing immediate behavior.
        self._build_and_append(out, pending)

    def _maybe_infer_exit(self, out, v):
        """EXIT for a car that left its slot and then left the scene.

        The tripwire only reports an exit when the apparent size changes
        enough while crossing. If the car vacated its slot (PARK_END) and its
        track later disappears without a tripwire crossing, the car is gone:
        emit an EXIT at the last time it was seen, flagged inferred=true so
        downstream can tell it from a tripwire-confirmed exit. Disable with
        lifecycle.infer_exit_on_track_loss: false.
        """
        lifecycle_cfg = self.cfg.get("lifecycle", {}) or {}
        if not lifecycle_cfg.get("infer_exit_on_track_loss", True):
            return
        ended = getattr(v, "park_ended_ts", None)
        if ended is None or v.has_parked or getattr(v, "exit_emitted", False):
            return
        exit_ts = max(float(v.last_seen_ts or ended), float(ended))
        log.info(
            "EXIT_INFERRED vehicle=%s track=%s park_end=%.3f last_seen=%.3f "
            "reason=track_lost_after_park_end",
            v.uid, v.track_id, float(ended), exit_ts,
        )
        v.exit_emitted = True
        v.pending_exit_crossing_ts = None
        self._emit(
            out, "EXIT", v, exit_ts, direction="exit",
            sid=getattr(v, "last_parked_slot", None), inferred=True,
        )

    def _in_zone(self, x, y):
        return (
            self.zone is None
            or cv2.pointPolygonTest(
                self.zone,
                (float(x), float(y)),
                False,
            ) >= 0
        )

    def _maybe_submit_plate(self, t, v, frame):
        pc = self.cfg["plate"]
        if not v.has_parked:
            return

        if (
            self.plates is None
            or not pc["enabled"]
            or v.plate_final
            or v.plate_inflight
        ):
            return

        if (
            v.plate_attempts >= pc["max_attempts"]
            or frame.ts - v.last_plate_submit < pc["interval_s"]
            or v.last_plate_frame_idx == frame.idx
        ):
            return

        x1, y1, x2, y2 = t.bbox

        if (
            (y2 - y1) < pc["min_vehicle_height_px"]
            or not self._in_zone((x1 + x2) / 2, y2)
        ):
            return

        H, W = frame.image.shape[:2]
        px, py = 0.05 * (x2 - x1), 0.05 * (y2 - y1)

        # Clamp every crop boundary to the image dimensions.
        xa = max(0, min(W, int(x1 - px)))
        ya = max(0, min(H, int(y1 - py)))
        xb = max(0, min(W, int(x2 + px)))
        yb = max(0, min(H, int(y2 + py)))
        if xb <= xa or yb <= ya:
            return

        crop = frame.image[ya:yb, xa:xb]
        accepted, quality = crop_quality(
            crop,
            min_brightness=float(pc.get("min_crop_brightness", 8.0)),
            max_brightness=float(pc.get("max_crop_brightness", 247.0)),
            min_contrast=float(pc.get("min_crop_contrast", 5.0)),
            min_laplacian_var=float(pc.get("min_crop_laplacian_var", 2.0)),
        )
        if not accepted:
            log.debug(
                "PLATE_CROP_REJECT uid=%s frame=%s reason=%s metrics=%s",
                v.uid, frame.idx, quality.get("reason"), quality,
            )
            return

        if crop.size and v.plate_attempts < 3:
            import os
            os.makedirs("data/debug_live_crops", exist_ok=True)
            debug_path = (
                f"data/debug_live_crops/{v.uid[:8]}_attempt{v.plate_attempts}.jpg"
            )
            cv2.imwrite(debug_path, crop)
        log.info("PLATE_CROP uid=%s shape=%s attempts=%s", v.uid, crop.shape, v.plate_attempts)

        if crop.size and self.plates.submit(v.uid, crop.copy()):
            # Save the exact crop immediately. If this OCR job is interrupted
            # by a restart, it can be resubmitted without the old track.
            # Keep the largest accepted crop for recovery retries.
            # Area is a proxy for available detail, not a guarantee of sharpness.
            if (
                v.last_crop is None
                or _plate_crop_rank(crop) > _plate_crop_rank(v.last_crop)
            ):
                v.last_crop = crop.copy()
            v.plate_inflight = True
            v.plate_inflight_observation_id = round(float(frame.ts), 6)
            v.last_plate_frame_idx = int(frame.idx)
            v.plate_attempts += 1
            v.last_plate_submit = frame.ts

    def _maybe_submit_recovered_crops(self, frame_ts):
        """Retry saved OCR crops for recovered events without live tracks."""
        pc = self.cfg["plate"]

        if self.plates is None or not pc["enabled"]:
            return

        live_uids = {
            v.uid for v in self.registry.by_track.values()
        }

        for pending in self.pending_parking_events:
            if not pending.get("recovery_crop_retry", False):
                continue

            v = pending["vehicle"]
            if not v.has_parked:
                continue

            if (
                v.uid in live_uids
                or v.plate_final
                or v.plate_inflight
                or v.last_crop is None
                or v.plate_attempts >= pc["max_attempts"]
            ):
                continue

            due_now = pending.get("recovery_retry_due", False)
            if (
                not due_now
                and frame_ts - v.last_plate_submit < pc["interval_s"]
            ):
                continue

            try:
                crop = v.last_crop.copy()
                if crop.size and self.plates.submit(v.uid, crop):
                    v.plate_inflight = True
                    v.plate_attempts += 1
                    v.last_plate_submit = frame_ts
                    pending["recovery_retry_due"] = False

                    log.info(
                        "Resubmitted saved OCR crop for pending %s "
                        "event_id=%s vehicle=%s attempt=%d/%d",
                        pending["event_type"],
                        pending["event_id"],
                        v.uid,
                        v.plate_attempts,
                        pc["max_attempts"],
                    )
            except Exception:
                log.exception(
                    "Could not resubmit saved OCR crop for event_id=%s",
                    pending["event_id"],
                )

    def _apply_plate_results(self):
        pc = self.cfg["plate"]

        # Deterministic replay: finish every submitted OCR job first so the
        # result is applied on the same frame in every run.
        if self.deterministic and self.plates is not None:
            self.plates.wait_idle()

        for uid, reads, crop in (self.plates.poll() if self.plates else []):
            v = self.registry.by_uid.get(uid)

            if v is None:
                for pending in self.pending_parking_events:
                    candidate = pending["vehicle"]
                    if candidate.uid == uid:
                        v = candidate
                        break

            if v is None:
                log.warning("Ignoring OCR result for unknown vehicle uid=%s", uid)
                continue

            observation_id = v.plate_inflight_observation_id
            v.plate_inflight_observation_id = None
            v.plate_inflight = False

            def acceptable_read(r):
                try:
                    conf, detector_conf = float(r.conf), float(r.det_conf)
                    x1, y1, x2, y2 = map(float, r.bbox)
                    if not np.isfinite([conf, detector_conf, x1, y1, x2, y2]).all():
                        return False
                    if not float(pc["min_ocr_conf"]) <= conf <= 1.0:
                        return False
                    if not float(pc.get("min_plate_detector_conf", 0.30)) <= detector_conf <= 1.0:
                        return False
                    width, height = x2 - x1, y2 - y1
                    if width < float(pc.get("min_plate_width_px", 24)):
                        return False
                    if height < float(pc.get("min_plate_height_px", 8)):
                        return False
                    ratio = width / max(height, 1e-6)
                    return bool(r.text) and (
                        float(pc.get("min_plate_aspect_ratio", 1.5))
                        <= ratio <= float(pc.get("max_plate_aspect_ratio", 7.0))
                    )
                except (AttributeError, TypeError, ValueError, OverflowError):
                    return False

            good = [r for r in reads if acceptable_read(r)]

            if good:
                # Prefer a format-valid Indian registration over a higher-score
                # but structurally invalid OCR string. If none are valid, keep
                # the strongest raw candidate for diagnostics/voting.
                valid_good = [r for r in good if fix_indian(r.text)[1]]
                pool = valid_good or good
                selected = max(
                    pool,
                    key=lambda r: float(r.conf) * float(r.det_conf),
                )

                v.voter.add(
                    selected.text,
                    selected.conf,
                    observation_id=observation_id,
                )

                if (
                    v.last_crop is None
                    or _plate_crop_rank(crop) > _plate_crop_rank(v.last_crop)
                ):
                    v.last_crop = crop.copy()

                plate_crop = cut_plate(crop, selected.bbox)
                plate_text, plate_valid = fix_indian(selected.text)

                if plate_crop is not None and plate_valid:
                    store = getattr(v, "plate_crops", None)
                    if store is None:
                        store = v.plate_crops = {}

                    previous = store.get(plate_text)
                    if previous is None or selected.conf >= previous[0]:
                        store[plate_text] = (selected.conf, plate_crop)

                elif plate_crop is not None:
                    # Diagnostic-only image: never used for plate confirmation.
                    previous_conf = float(
                        getattr(v, "plate_diagnostic_confidence", 0.0)
                    )
                    if float(selected.conf) >= previous_conf:
                        v.plate_diagnostic_crop = plate_crop.copy()
                        v.plate_diagnostic_confidence = float(selected.conf)

                v.plate_final = v.voter.is_final(
                    pc["confirm_reads"],
                    pc["confirm_conf"],
                )

                log.info(
                    "PLATE_STATE uid=%s state=%s ocr_conf=%.3f det_conf=%.3f reads=%d attempt=%d",
                    uid, v.plate_status, float(selected.conf), float(selected.det_conf),
                    len(v.voter.reads), v.plate_attempts,
                )

                if v.plate_final:
                    for pending in self.pending_parking_events:
                        if pending["vehicle"].uid == uid:
                            pending["recovery_crop_retry"] = False
                            pending["recovery_retry_due"] = False

    def _release_handoff(self, out, now_ts, final=False):
        if getattr(self, "handoff", None) is None:
            return
        for item, verdict, reason in self.handoff.poll(self.cam, now_ts, final=final):
            if verdict != "EMIT":
                continue
            p = item["payload"]
            log.info("HANDOFF %s released cam=%s track=%s reason=%s",
                     item["etype"], self.cam, item["local"], reason)
            self._emit(
                out, item["etype"], p["vehicle"], item["event_ts"],
                direction=p["direction"], sid=p["sid"], inferred=p["inferred"],
                received_at=p["received_at"], captured_at=p["captured_at"],
                _handoff_checked=True,
                _detected_at=p.get("detected_at"),
            )

    def _release_ready_parking_events(self, out, final=False):
        """Queue finalized lifecycle events; keep unresolved events durable."""
        now = self.now()
        remaining = []
        local_preview = getattr(self.publisher, 'local_preview', False)

        for pending in self.pending_parking_events:
            v = pending["vehicle"]

            if self._plate_ready(v):
                self._build_and_append(out, pending)
                continue

            if local_preview and (
                now >= pending["deadline"] or final
            ):
                log.warning(
                    "LOCAL_PREVIEW_PLATE_UNKNOWN event=%s event_id=%s vehicle=%s",
                    pending["event_type"],
                    pending["event_id"],
                    v.uid,
                )
                self._build_and_append(out, pending)
                continue

            if now >= pending["deadline"]:
                # Plate recognition must not block slot/timestamp telemetry
                # forever. Publish this lifecycle event with plate=null and
                # status UNCONFIRMED; a later confirmed read is handled as an
                # explicit VEHICLE_UPDATE. Never substitute the OCR candidate.
                if not pending["timed_out"]:
                    pending["timed_out"] = True
                    log.warning(
                        "PLATE_WAIT_TIMEOUT event=%s event_id=%s vehicle=%s "
                        "original_event_time=%s; releasing with plate UNCONFIRMED",
                        pending["event_type"],
                        pending["event_id"],
                        v.uid,
                        pending["event_time"],
                    )
                self._build_and_append(out, pending)
                continue

            if final:
                log.error(
                    "UNRESOLVED_LIFECYCLE_EVENT event=%s event_id=%s vehicle=%s "
                    "original_event_time=%s plate_final=%s; durable record "
                    "retained and event NOT sent",
                    pending["event_type"],
                    pending["event_id"],
                    v.uid,
                    pending["event_time"],
                    v.plate_final,
                )

            remaining.append(pending)

        self.pending_parking_events = remaining
        self._persist_all_pending_events()

    ENTRY_TRIPWIRE_HANDOFF_MAX_S = 2.5

    def _remember_entry_tripwire_state(self, track_id, vehicle, ts):
        """Retain only outside-side evidence for a verified short-gap rebind.

        EXIT-side history is deliberately never retained or transferred.
        """
        state = self.tripwire.state.get(track_id)
        if not state or vehicle is None:
            return

        outside_side = -int(self.tripwire.inside_side)
        if int(state.get("side", 0)) != outside_side:
            return

        last_ts = state.get("last_ts")
        last_d = state.get("last_d")
        if last_ts is None or last_d is None:
            return

        age = float(ts) - float(last_ts)
        if age < 0.0 or age > self.ENTRY_TRIPWIRE_HANDOFF_MAX_S:
            return

        self._entry_tripwire_handoffs[vehicle.uid] = {
            "side": outside_side,
            "last_d": float(last_d),
            "last_ts": float(last_ts),
            "saved_at": float(ts),
        }

    def _restore_entry_tripwire_state(self, track_id, vehicle, ts):
        """Restore ENTRY-only crossing history after registry-confirmed identity recovery."""
        if vehicle is None:
            return False

        saved = self._entry_tripwire_handoffs.pop(vehicle.uid, None)
        if saved is None:
            return False

        age = float(ts) - float(saved["last_ts"])
        if age < 0.0 or age > self.ENTRY_TRIPWIRE_HANDOFF_MAX_S:
            return False

        # Never overwrite an observation already made for this tracker ID.
        if track_id in self.tripwire.state:
            return False

        restored = self.tripwire._new_state()
        restored.update({
            "side": -int(self.tripwire.inside_side),
            "last_d": float(saved["last_d"]),
            "last_ts": float(saved["last_ts"]),
        })
        self.tripwire.state[track_id] = restored
        log.info(
            "ENTRY_TRIPWIRE_HANDOFF_RESTORED track=%s vehicle=%s gap_s=%.3f",
            track_id,
            vehicle.uid,
            age,
        )
        return True

    def process(self, frame, detections=None, detection_elapsed_ms=None):
        """Process one frame for this camera.

        ``detections`` may be supplied by a shared, serialized inference owner.
        When omitted, preserve the legacy single-camera behavior.
        """
        t0 = time.perf_counter()
        events = []
        self.last_frame_ok = False
        context = {"camera_id": self.cam, "frame_id": getattr(frame, "idx", None)}
        if not usable_frame(frame):
            Diagnostic(
                "VIDEO_FRAME_INVALID", "VIDEO_PROCESSING", "frame", "validate",
                "validation", "Frame pixels or capture metadata are invalid.", True,
                "missing", "Skipped frame before inference.", context=context,
            ).log(log)
            return events
        previous_ts = getattr(self, "display_ts", None)
        if previous_ts is not None and frame.ts < previous_ts:
            Diagnostic(
                "VIDEO_TIMESTAMP_OUT_OF_ORDER", "VIDEO_PROCESSING", "frame", "validate",
                "validation", "Capture timestamp moved backwards.", True, "missing",
                "Skipped frame to keep tracker and lifecycle time monotonic.", context=context,
            ).log(log)
            return events
        self.display_ts = float(frame.ts)

        # Capture recording start before tracker confirmation.
        if self.slots.stream_start_ts is None:
            self.slots.stream_start_ts = float(frame.ts)

        # MOG2 motion gate: use YOLO only when motion is meaningful,
        # or periodically as a safeguard. Each Pipeline owns its own model.
        self.motion_gate_frame_count += 1
        run_yolo = detections is not None or not self.motion_gate_enabled

        try:
            if detections is not None:
                dets = detections
            elif self.motion_gate_enabled:
                mask = self.motion_gate.apply(frame.image)
                # Ignore MOG2 shadow pixels (127); count only foreground (255).
                motion_pixels = int(cv2.countNonZero(
                    cv2.threshold(mask, 200, 255, cv2.THRESH_BINARY)[1]
                ))
                periodic_check = (
                    self.motion_gate_frame_count % self.motion_gate_force_every == 0
                )
                run_yolo = (
                    motion_pixels >= self.motion_gate_min_pixels or periodic_check
                )

                if run_yolo:
                    dets = self.detector.detect(frame.image)
                else:
                    dets = np.zeros((0, 6), dtype=np.float32)
            else:
                dets = self.detector.detect(frame.image)
        except Exception:
            Diagnostic(
                "YOLO_INFERENCE_FAILED", "VEHICLE_DETECTION", "detector", "infer",
                "inference", "Vehicle inference failed for this frame.", True,
                "missing", "Skipped frame without advancing tracking or parking.",
                context=context,
            ).log(log, exc_info=True)
            return events

        try:
            dets = np.asarray(dets, dtype=np.float32)
            if (dets.ndim != 2 or dets.shape[1] != 6
                    or not np.isfinite(dets).all()
                    or (dets[:, 2:4] <= dets[:, :2]).any()):
                raise ValueError("Expected finite N-by-6 detections with positive box dimensions.")
        except (TypeError, ValueError, OverflowError):
            Diagnostic(
                "DETECTION_OUTPUT_INVALID", "VEHICLE_DETECTION", "detector",
                "validate detections", "validation", "Detector output is malformed.",
                True, "missing", "Skipped frame without advancing tracking or parking.",
                context=context,
            ).log(log, exc_info=True)
            return events
        if run_yolo and len(dets) == 0:
            log.debug("NO_DETECTIONS camera=%s frame=%s", context["camera_id"], frame.idx)
        t1 = time.perf_counter()

        try:
            tracks, removed = self.tracker.update(dets, frame.ts)
        except Exception:
            Diagnostic(
                "TRACKER_UPDATE_FAILED", "VEHICLE_TRACKING", "tracker", "associate",
                "processing", "Tracker could not associate detections.", True,
                "partial", "Skipped downstream updates for this frame.",
                context=context,
            ).log(log, exc_info=True)
            return events
        try:
            ids = [track.id for track in (*tracks, *removed)]
            if any(not isinstance(track_id, Integral) or track_id < 0 for track_id in ids):
                raise ValueError("Track IDs must be nonnegative integers")
            if len(ids) != len(set(ids)):
                raise ValueError("Tracker returned duplicate IDs")
        except (AttributeError, TypeError, ValueError):
            Diagnostic(
                "TRACKER_OUTPUT_INVALID", "VEHICLE_TRACKING", "tracker", "validate",
                "validation", "Tracker returned missing or duplicate identifiers.", True,
                "partial", "Skipped downstream updates for this frame.", context=context,
            ).log(log, exc_info=True)
            return events
        t2 = time.perf_counter()

        # Estimate current->reference motion using current-frame track boxes as
        # masks. This keeps moving vehicles out of the background feature set,
        # including when the reference frame is first initialized. Detection
        # and tracking remain entirely in original source-frame coordinates.
        if self.camera_motion_enabled:
                self.camera_transform = self.camera_motion.update(
                        frame.image,
                        moving_boxes=[t.bbox for t in tracks],
                        frame_id=getattr(frame, "idx", None),
                )
        else:
                self.camera_transform = np.array(
                        [[1., 0., 0.], [0., 1., 0.]], dtype=np.float32
        )

        self.slots.set_camera_transform(self.camera_transform)

        # Release removed tracker IDs from parking state BEFORE processing
        # newly created tracks. This lets recover_lost_track() rebind a
        # replacement tracker to the original parked Vehicle identity.
        for t in removed:
            v = self.registry.by_track.get(t.id)
            self._remember_entry_tripwire_state(t.id, v, frame.ts)

            # Preserve an already-observed real crossing across tracker-ID recovery.
            pending_crossing = self._pending_tripwire_crossings.pop(
                int(t.id), None
            )
            if pending_crossing is not None and v is not None:
                v._pending_tripwire_crossing = pending_crossing
            self.tripwire.forget(t.id)
            self.slots.on_track_removed(t)

            if v is not None:
                try:
                    self._maybe_infer_exit(events, v)
                except Exception:
                    log.exception("EXIT_INFERENCE_FAILED vehicle=%s", v.uid)
                v.pending.clear()
            self.registry.drop_track(t.id)

        # ByteTracker returns only confirmed tracks to the parking pipeline.
        # Feed tripwire observations from all tracks updated on this frame,
        # including tentative tracks, so the approach-side state is not lost.
        active_tracker_tracks = [
            track for track in self.tracker.tracks
            if float(track.last_ts) == float(frame.ts)
        ]
        active_tracker_ids = {int(track.id) for track in self.tracker.tracks}
        for stale_id in list(self.tripwire.state):
            if int(stale_id) not in active_tracker_ids:
                self.tripwire.forget(stale_id)

        for observed_track in active_tracker_tracks:
            ox1, oy1, ox2, oy2 = map(float, observed_track.bbox)
            tripwire_point = transform_points(
                [((ox1 + ox2) / 2.0, oy2)], self.camera_transform
            )[0]
            observed_crossing = self.tripwire.update(
                observed_track.id,
                float(tripwire_point[0]),
                float(tripwire_point[1]),
                frame.ts,
            )
            if (
                observed_crossing is not None
                and int(observed_track.id) not in self._pending_tripwire_crossings
            ):
                self._pending_tripwire_crossings[int(observed_track.id)] = observed_crossing

        self.last_tracks = tracks
        cc = self.cfg["color"]

        for t in tracks:
            # IMPORTANT:
            # Try to restore the original parked Vehicle identity BEFORE
            # get_or_create() can create a fresh Vehicle for this new tracker ID.
            recovered = self.slots.recover_lost_track(
                t,
                frame.ts,
                self.registry,
            )

            if recovered:
                log.info(
                    "Recovered parked vehicle identity for track=%s",
                    t.id,
                )

            # If this is a fresh tracker ID but we already have a recently
            # identified vehicle, preserve that Vehicle object so its finalized
            # plate/type/color can be used when parking is confirmed.
            v = self.registry.get_or_create(t, frame.ts)
            self._restore_entry_tripwire_state(t.id, v, frame.ts)

            # Replay only previously observed crossing evidence for this vehicle.
            pending_crossing = getattr(
                v, "_pending_tripwire_crossing", None
            )
            if pending_crossing is not None:
                delattr(v, "_pending_tripwire_crossing")
                direction, crossing_ts = pending_crossing
                if (
                    direction == "entry"
                    and getattr(v, "entry_crossing_ts", None) is None
                ):
                    self._emit(
                        events,
                        "ENTRY",
                        v,
                        crossing_ts,
                        direction="entry",
                        sid=v.slot_id,
                        inferred=False,
                    )

            # Expire unused handoff evidence; it must never become long-term history.
            for uid, saved in list(self._entry_tripwire_handoffs.items()):
                if float(frame.ts) - float(saved["last_ts"]) > self.ENTRY_TRIPWIRE_HANDOFF_MAX_S:
                    self._entry_tripwire_handoffs.pop(uid, None)

            # Experimental appearance observation, sampled at most twice per second.
            reid_key = (self.cam, int(t.id))
            if self.reid_embedder is not None and float(frame.ts) - self.reid_last_ts.get(reid_key, -1e18) >= 0.5:
                rx1, ry1, rx2, ry2 = map(int, t.bbox)
                rh, rw = frame.image.shape[:2]
                rx1, rx2 = max(0, rx1), min(rw, rx2)
                ry1, ry2 = max(0, ry1), min(rh, ry2)
                if rx2 > rx1 and ry2 > ry1:
                    embedding = self.reid_embedder.encode(
                        frame.image[ry1:ry2, rx1:rx2]
                    )
                    if embedding is not None:
                        self.reid_observations[reid_key] = {
                            "camera_id": self.cam,
                            "track_id": int(t.id),
                            "timestamp": float(frame.ts),
                            "class_id": int(t.cls),
                            "embedding": embedding,
                        }
                        self.reid_last_ts[reid_key] = float(frame.ts)
            v.last_seen_ts = frame.ts
            v.cls_votes[t.cls] += 1

            x1, y1, x2, y2 = t.bbox

            if (
                frame.ts - v.last_color_ts >= cc["interval_s"]
                and (y2 - y1) >= cc["min_box_h"]
                and sum(v.color_votes.values()) < cc["max_samples"]
            ):
                frame_h, frame_w = frame.image.shape[:2]

                # Clip coordinates to actual frame dimensions.
                bx1 = max(0, min(frame_w, int(x1)))
                by1 = max(0, min(frame_h, int(y1)))
                bx2 = max(0, min(frame_w, int(x2)))
                by2 = max(0, min(frame_h, int(y2)))

                crop = (
                    frame.image[by1:by2, bx1:bx2]
                    if bx2 > bx1 and by2 > by1
                    else None
                )

                try:
                    col, agreement = (
                        dominant_color_details(crop)
                        if crop is not None and crop.size else (None, 0.0)
                    )
                except Exception:
                    Diagnostic(
                        "VEHICLE_ATTRIBUTE_FAILED", "VEHICLE_ATTRIBUTES", "colour",
                        "estimate", "processing", "Colour estimate failed.", True,
                        "partial", "Kept previous observations and continued parking processing.",
                        context={**context, "track_id": t.id},
                    ).log(log, exc_info=True)
                    col, agreement = None, 0.0

                # Only accept reasonably consistent colour observations.
                if col and agreement >= 0.65:
                    v.color_votes[col] += 1

                v.last_color_ts = frame.ts

            # Crossing evidence was collected from the track's first
            # detector observations, including its tentative period.
            crossing = self._pending_tripwire_crossings.pop(int(t.id), None)

            # Tripwire is used only as positional/directional evidence.
            # Parking state is authoritative for ENTRY/EXIT lifecycle events.
            # Tripwire confirms gate ENTRY/EXIT. SlotManager independently
            # owns PARK_START/PARK_END. Lifecycle events pass through
            # the existing plate-validation hold in _emit().
            if crossing:
                direction, crossing_ts = crossing

                if direction == "entry":
                    self._emit(
                        events,
                        "ENTRY",
                        v,
                        crossing_ts,
                        direction="entry",
                        sid=v.slot_id,
                        inferred=False,
                    )
                    v.pending_entry_received_at = None

                elif direction == "exit":
                    if v.has_parked:
                        v.pending_exit_crossing_ts = crossing_ts
                        log.info(
                            "EXIT_DEFERRED vehicle=%s crossing_ts=%.3f reason=parked",
                            v.uid, crossing_ts,
                        )
                    else:
                        self._emit(
                            events,
                            "EXIT",
                            v,
                            crossing_ts,
                            direction="exit",
                            sid=v.slot_id,
                            inferred=False,
                        )
                        v.pending_exit_crossing_ts = None
                        v.exit_emitted = True

                log.debug(
                    "Tripwire crossing track=%s direction=%s ts=%.3f",
                    t.id,
                    direction,
                    crossing_ts,
                )
            self._maybe_submit_plate(t, v, frame)


        # Retry recovered crops before polling so their results can be applied
        # by the normal result handler below.
        self._maybe_submit_recovered_crops(frame.ts)
        self._apply_plate_results()

        # SlotManager owns parking-slot transitions. Preserve their event types
        # rather than incorrectly translating them into gate ENTRY/EXIT.
        for slot_event, v, et, sid, inferred in self.slots.update(
            tracks,
            frame.ts,
            self.registry,
        ):
            if slot_event not in ("PARK_START", "PARK_END"):
                log.warning(
                    "Ignoring unknown SlotManager event type: %s",
                    slot_event,
                )
                continue

            event_type = slot_event

            self._emit(
                events,
                event_type,
                v,
                et,
                sid=sid,
                inferred=inferred,
            )

            if event_type == "PARK_START":
                v.has_parked = True
            elif event_type == "PARK_END":
                v.has_parked = False
                v.park_ended_ts = float(et)
                v.last_parked_slot = sid
                v.exit_emitted = False
                exit_ts = v.pending_exit_crossing_ts
                if exit_ts is not None:
                    v.exit_emitted = True
                    self._emit(
                        events,
                        "EXIT",
                        v,
                        exit_ts,
                        direction="exit",
                        sid=sid,
                        inferred=False,
                    )
                    v.pending_exit_crossing_ts = None

            if inferred:
                self.registry.release(v)

        self._release_handoff(events, frame.ts)
        self._release_ready_parking_events(events)

        # Preserve the existing late plate-update behavior.
        for v in list(self.registry.by_uid.values()):
            c = v.voter.consensus()

            plate_is_confirmed = bool(
                c
                and c.valid
                and self._plate_ready(v)
            )

            if (
                v.any_emitted
                and plate_is_confirmed
                and c.text != v.emitted_plate
            ):
                self._emit(
                    events,
                    "VEHICLE_UPDATE",
                    v,
                    frame.ts,
                )

        
        events = [event for event in events if self._publish_event(event)]

        # Keep the latest vehicle votes and OCR state durable for unresolved
        # lifecycle events, including after their vehicle track has disappeared.
        self._persist_all_pending_events()

        done = time.perf_counter()

        detect_ms = (
            float(detection_elapsed_ms)
            if detection_elapsed_ms is not None
            else (t1 - t0) * 1000
        )
        self.stats.add("detect", detect_ms)
        self.stats.add("track", (t2 - t1) * 1000)
        self.stats.add("total", (done - t0) * 1000)
        self.stats.add("e2e", (self.now() - frame.ts) * 1000)
        self.stats.frames += 1
        self.last_frame_ok = True

        return events

    def flush(self, ts):
        events = []

        # Collect completed OCR jobs; the worker may already be stopped.
        self._apply_plate_results()
        self._release_handoff(events, ts, final=True)
        self._release_ready_parking_events(events, final=True)

        for v in list(self.registry.by_uid.values()):
            v.pending.clear()

        events = [event for event in events if self._publish_event(event)]

        # Do not clear unresolved records. They must remain recoverable.
        self._persist_all_pending_events()

        return events

    def draw(self, img):
        g = self.geo

        inverse_transform = cv2.invertAffineTransform(self.camera_transform)
        current_tripwire = np.round(
            transform_points(g.tripwire, inverse_transform)
        ).astype(np.int32)
        cv2.line(
            img,
            tuple(map(int, current_tripwire[0])),
            tuple(map(int, current_tripwire[1])),
            (0, 255, 255),
            2,
        )

        for s in g.slots:
            occ = self.slots.occupants.get(s["slot_id"])
            col = (0, 0, 255) if occ else (0, 200, 0)
            current_polygon = np.round(transform_points(s["polygon"], inverse_transform)).astype(np.int32)

            cv2.polylines(
                img,
                [current_polygon],
                True,
                col,
                2,
            )

            cv2.putText(
                img,
                s["slot_id"],
                tuple(map(int, current_polygon[0])),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                col,
                1,
            )

        # Visible parking occupancy summary
        total_slots = len(g.slots)
        occupied_slots = [
            s["slot_id"]
            for s in g.slots
            if self.slots.occupants.get(s["slot_id"])
        ]
        occupied_count = len(occupied_slots)
        free_count = max(0, total_slots - occupied_count)

        cv2.rectangle(img, (10, 10), (560, 140), (20, 20, 20), -1)
        cv2.putText(
            img,
            f"SLOTS: {occupied_count}/{total_slots} occupied",
            (25, 45),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.9,
            (0, 255, 255),
            2,
        )
        cv2.putText(
            img,
            f"AVAILABLE: {free_count}",
            (25, 82),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 255, 0),
            2,
        )
        motion_quality = (
            self.camera_motion.quality if self.camera_motion_enabled else "disabled"
        )
        m = np.asarray(self.camera_transform, dtype=float)
        motion_scale = float(np.hypot(m[0, 0], m[1, 0]))
        cv2.putText(
            img,
            f"MOTION: {motion_quality}  dx={m[0, 2]:.0f} dy={m[1, 2]:.0f} scale={motion_scale:.2f}",
            (25, 119),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (255, 255, 255),
            1,
        )

        if occupied_slots:
            print(
                "SLOT_SUMMARY occupied=%s/%s slots=%s"
                % (occupied_count, total_slots, ",".join(occupied_slots)),
                flush=True,
            )

        for t in self.last_tracks:
            v = self.registry.by_track.get(t.id)

            x1, y1, x2, y2 = map(int, t.bbox)

            cv2.rectangle(
                img,
                (x1, y1),
                (x2, y2),
                (255, 120, 0),
                2,
            )

            consensus = v.voter.consensus() if v else None
            plate_confirmed = bool(
                v and consensus and consensus.valid and self._plate_ready(v)
            )
            c = consensus if plate_confirmed else None
            provisional_text = (
                consensus.text
                if consensus and consensus.valid and not plate_confirmed
                else None
            )

            try:
                _n = __import__('time').time()
                _d = self.__dict__.setdefault('_lp', {})
                if _n - _d.get(t.id, 0) >= 1.0:
                    _d[t.id] = _n
                    try:
                        _k = __import__('sqlite3').connect('data/live_attrs.db', timeout=2)
                        _k.execute('create table if not exists live_attrs(ts real, track integer, plate text, colour text)')
                        _k.execute('insert into live_attrs values (?,?,?,?)', (_n, t.id, (c.text if c else None), (v.color() if v else None)))
                        _k.commit(); _k.close()
                    except Exception:
                        pass
                    display_plate = (
                        c.text if c else (f"~{provisional_text}" if provisional_text else "?")
                    )
                    print('LIVE #%s plate=%s colour=%s' % (t.id, display_plate, (v.color() if v else '')), flush=True)
            except Exception:
                pass
            shown_plate = (
                c.text
                if c
                else (f"~{provisional_text}" if provisional_text else "PLATE?")
            )

            colour = v.color() if v and v.color() else "UNKNOWN"

            occupant = None
            bay_id = "NOT CONFIRMED"
            if v:
                for sid, candidate in self.slots.occupants.items():
                    if candidate.vehicle is v or candidate.track_id == t.id:
                        occupant = candidate
                        bay_id = sid
                        break

            if occupant is not None:
                if occupant.baseline:
                    parked_text = "Present at recording start"
                elif occupant.parked_at is None:
                    parked_text = "Occupied (entry unverified)"
                else:
                    video_start = self.slots.stream_start_ts
                    parked_offset = max(
                        0.0,
                        float(occupant.parked_at)
                        - float(video_start if video_start is not None else occupant.parked_at),
                    )
                    elapsed = max(
                        0.0,
                        float(getattr(self, "display_ts", occupant.last_seen))
                        - float(occupant.parked_at),
                    )

                    def _hms(seconds):
                        seconds = int(seconds)
                        return (
                            f"{seconds // 3600:02d}:"
                            f"{(seconds % 3600) // 60:02d}:"
                            f"{seconds % 60:02d}"
                        )

                    from datetime import datetime, timezone, timedelta
                    ist = timezone(timedelta(hours=5, minutes=30))
                    parked_at_ist = datetime.fromtimestamp(
                        float(occupant.parked_at), ist
                    ).strftime("%Y-%m-%d %H:%M:%S IST")
                    parked_text = (
                        f"Since {parked_at_ist} "
                        f"(duration {_hms(elapsed)})"
                    )
            else:
                parked_text = "Not parked"

            # Minimal, readable overlay: green text on a dark background.
            label_lines = [
                f"Number: {shown_plate}",
                f"Slot: {bay_id}",
                f"Time parked: {parked_text}",
                f"Colour: {colour}",
            ]

            font = cv2.FONT_HERSHEY_SIMPLEX
            font_scale = 0.68
            thickness = 2
            line_height = 30
            text_x = max(5, min(x1, img.shape[1] - 260))
            text_y = max(25, y1 - 10)

            if text_y - line_height * len(label_lines) < 5:
                text_y = min(
                    img.shape[0] - 8,
                    y2 + line_height * len(label_lines) + 8,
                )

            green = (0, 255, 0)
            for index, text_line in enumerate(label_lines):
                y = text_y - (len(label_lines) - 1 - index) * line_height
                (text_width, text_height), _ = cv2.getTextSize(
                    text_line, font, font_scale, thickness
                )

                left = max(0, text_x)
                top = max(0, y - text_height - 6)
                right = min(img.shape[1] - 1, text_x + text_width + 12)
                bottom = min(img.shape[0] - 1, y + 6)

                cv2.rectangle(
                    img, (left, top), (right, bottom), (0, 0, 0), -1
                )
                cv2.putText(
                    img,
                    text_line,
                    (text_x + 5, y),
                    font,
                    font_scale,
                    green,
                    thickness,
                    cv2.LINE_AA,
                )

        return img
