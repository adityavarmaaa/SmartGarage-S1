"""Config-driven multi-camera orchestration with one shared inference owner.

Each camera keeps its own capture source, geometry, tracker/pipeline, OCR worker,
publisher/outbox and lifecycle state. A single scheduler owns detector.detect(),
so a model instance is never called concurrently.
"""
import logging
from vehicle_intelligence.common.errors import Diagnostic, ProcessingError
import os
import sys
from vehicle_intelligence.common.data_models import usable_frame
import time
import threading
from dataclasses import dataclass

import cv2
import numpy as np

from vehicle_intelligence.config import camera_configs, validate_geometry_for_camera
from vehicle_intelligence.geometry import Geometry
from vehicle_intelligence.grabber import open_source
from vehicle_intelligence.pipeline import Pipeline
from vehicle_intelligence.demo_video import DemoRecorder
from vehicle_intelligence.flow import (
    AsyncGlobalReID, GpuArbiter, HandoffCoordinator, MotionGate,
    resolve_aux_device,
)
from vehicle_intelligence.plates import PlateReader, PlateWorker
from vehicle_intelligence.determinism import is_file_source, resolve_deterministic, seed_everything
from vehicle_intelligence.s2_mapper import S2Mapper
from vehicle_intelligence.uploader import UploadingOutbox

log = logging.getLogger("multicam")


@dataclass
class CameraRuntime:
    cfg: dict
    source: object
    geometry: object
    pipeline: object = None
    plate_worker: object = None
    publisher: object = None
    first_frame: object = None
    last_idx: int = -1
    last_infer_monotonic: float = 0.0
    last_scheduled_monotonic: float = 0.0
    pending_frame: object = None
    last_gray: object = None
    activity: float = 0.0
    frames_seen: int = 0
    dropped_frames: int = 0
    processed_frames: int = 0
    occupancy_unavailable: bool = False
    last_metrics_log: float = 0.0
    event_counts: object = None
    event_history: object = None
    track_records: object = None
    gate: object = None
    skipped_idle: int = 0
    latest_raw: object = None
    motion_mask: object = None
    motion_roi_info: object = None

    def __post_init__(self):
        self.event_counts = {}
        self.event_history = []
        self.track_records = {}


def _lucas_kanade_score(previous_gray, gray, mask=None):
    """Return a bounded feature-motion score for scheduler prioritization.

    This is deliberately advisory: it does not classify objects, discard frames,
    or create parking events. Background features (e.g. leaves) can also move.
    """
    points = cv2.goodFeaturesToTrack(
        previous_gray,
        maxCorners=80,
        qualityLevel=0.01,
        minDistance=5,
        blockSize=7,
        mask=mask,
    )
    if points is None or len(points) < 3:
        return 0.0

    next_points, status, _error = cv2.calcOpticalFlowPyrLK(
        previous_gray,
        gray,
        points,
        None,
        winSize=(15, 15),
        maxLevel=2,
        criteria=(
            cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT,
            20,
            0.03,
        ),
    )
    if next_points is None or status is None:
        return 0.0

    valid = status.reshape(-1).astype(bool)
    if not np.any(valid):
        return 0.0

    old_xy = points.reshape(-1, 2)[valid]
    new_xy = next_points.reshape(-1, 2)[valid]
    displacement = np.linalg.norm(new_xy - old_xy, axis=1)
    displacement = displacement[np.isfinite(displacement)]
    if displacement.size == 0:
        return 0.0

    # Ignore sub-pixel jitter. The moving-feature fraction prevents a single
    # noisy point from dominating; displacement is capped to keep the score
    # bounded and comparable across cameras.
    moving = displacement[displacement >= 0.75]
    if moving.size == 0:
        return 0.0

    moving_fraction = float(moving.size) / float(max(len(points), 1))
    median_motion = float(np.median(moving))
    motion_strength = min(1.0, median_motion / 5.0)
    return float(np.clip(moving_fraction * motion_strength, 0.0, 1.0))


def _activity_score(previous_gray, image, roi_mask=None, min_blob_px=0):
    """Motion score of the parking area only (roi_mask) with small-blob filtering.

    roi_mask / min_blob_px default to "whole frame, no filtering" so older
    callers and tests behave exactly as before.
    """
    small = cv2.resize(image, (160, 90), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    if previous_gray is None:
        return gray, 1.0

    from vehicle_intelligence.motion_roi import masked_diff_score
    if roi_mask is not None and roi_mask.shape != gray.shape:
        roi_mask = None
    difference_score = masked_diff_score(
        previous_gray, gray, mask=roi_mask, min_blob_px=min_blob_px
    )

    # Lucas-Kanade is an additional motion signal, not a replacement for
    # frame differencing. It only looks at features inside the ROI.
    optical_flow_score = _lucas_kanade_score(previous_gray, gray, mask=roi_mask)
    combined_score = max(difference_score, 0.5 * optical_flow_score)
    return gray, min(1.0, combined_score)


class _NullReID:
    """Used when global_reid.enabled is false: every vehicle keeps its local id."""
    embedded = 0
    dropped = 0

    def update(self, camera_id, frame, tracks, timestamp=None):
        return {}

    def summary(self):
        return []

    def stop(self):
        pass


class _PreviewPublisher:
    """No-publisher mode that still shows EXACTLY what Service 2 would receive.

    Raw Service-1 lifecycle events are kept in `raw_events`; the S2Mapper turns
    PARK_START/PARK_END into the Service 2 ENTRY/EXIT contract, and those
    payloads land in `s2_events` (printed as JSON, never sent anywhere).
    """
    local_preview = True

    def __init__(self, camera_id, db_dir="data"):
        import json as _json
        from vehicle_intelligence.main import MemoryPublisher
        self._json = _json
        self.camera_id = camera_id
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in camera_id)
        os.makedirs(db_dir, exist_ok=True)
        db_path = os.path.join(db_dir, f"lifecycle_preview.{safe}.db")
        if os.path.exists(db_path):
            os.remove(db_path)  # every preview run starts from a clean lifecycle state
        self.sink = MemoryPublisher()
        self.mapper = S2Mapper(self.sink, db_path=db_path)
        self.local_preview = True

    @property
    def s2_events(self):
        return self.sink.report_events

    @property
    def raw_events(self):
        return self.mapper.raw_events

    def publish(self, event):
        self.mapper.publish(event)

    def stop(self, wait=True, timeout=5.0):
        try:
            self.mapper._db.close()
        except Exception:
            pass
        return True

    def __getattr__(self, name):
        return getattr(self.sink, name)





def _seed_exit_preview_sessions(publisher, restore_cfg):
    """Seed verified ENTRY lifecycle events for restored occupants dynamically."""
    import json
    from pathlib import Path

    if not getattr(publisher, "local_preview", False):
        raise RuntimeError("EXIT session seeding requires local preview mode")

    camera_id = str(publisher.camera_id)
    report_path = restore_cfg.get("entry_report_path")
    manifest_path = restore_cfg.get("manifest_path")

    if not report_path or not Path(report_path).is_file():
        raise FileNotFoundError(f"ENTRY report not found: {report_path}")
    if not manifest_path or not Path(manifest_path).is_file():
        raise FileNotFoundError(f"EXIT restoration manifest not found: {manifest_path}")

    with open(report_path, "r", encoding="utf-8") as handle:
        report = json.load(handle)
    with open(manifest_path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)

    records = [
        record for record in manifest.get("records", [])
        if str(record.get("camera_id")) == camera_id
    ]
    live_uids = {
        str(record["vehicle_uid"])
        for record in records
        if record.get("vehicle_uid")
        and not bool(record.get("baseline"))
    }

    events_by_uid = {}
    seen_event_ids = set()

    # Only original S1 lifecycle records are replayed; never transformed S2 payloads.
    for event in report.get("lifecycle_events", []):
        if not isinstance(event, dict):
            continue

        uid = event.get("vehicle_uid")
        event_camera = event.get("camera_id")
        event_type = str(event.get("event_type", "")).upper()
        event_id = event.get("event_id")

        if not uid or str(uid) not in live_uids:
            continue
        if str(event_camera) != camera_id:
            continue
        if event_type not in {"ENTRY", "PARK_START"}:
            continue
        if not event_id:
            raise RuntimeError("A matching lifecycle event has no event_id.")
        if event_id in seen_event_ids:
            raise RuntimeError(f"Duplicate lifecycle event ID: {event_id}")

        seen_event_ids.add(event_id)
        events_by_uid.setdefault(str(uid), []).append(event)

    selected = []
    for uid in sorted(live_uids):
        uid_events = events_by_uid.get(uid, [])
        entry_events = [
            event for event in uid_events
            if str(event.get("event_type", "")).upper() == "ENTRY"
        ]
        park_events = [
            event for event in uid_events
            if str(event.get("event_type", "")).upper() == "PARK_START"
        ]

        if len(entry_events) > 1 or len(park_events) != 1:
            raise RuntimeError(
                f"{camera_id}: UID {uid} has "
                f"{len(entry_events)} ENTRY and {len(park_events)} PARK_START events."
            )

        selected.extend(entry_events)
        selected.extend(park_events)

    selected.sort(
        key=lambda event: (
            event.get("event_time")
            or event.get("timestamp")
            or ""
        )
    )

    for event in selected:
        if (
            str(event.get("camera_id")) != camera_id
            or str(event.get("vehicle_uid")) not in live_uids
        ):
            raise RuntimeError("Lifecycle event identity validation failed.")
        publisher.publish(event)

    return {
        "camera_id": camera_id,
        "restored_live_vehicles": len(live_uids),
        "seeded_original_events": len(selected),
        "event_ids": [event["event_id"] for event in selected],
    }


def _make_publisher(cfg, no_publisher):
    if no_publisher:
        preview_cfg = cfg.get("exit_restore", {}) or {}
        preview_db_dir = preview_cfg.get("preview_db_dir", "data")
        return _PreviewPublisher(
            cfg["camera_id"],
            db_dir=preview_db_dir,
        )
    base = cfg["publisher"]["outbox_db"]
    root, ext = os.path.splitext(base)
    camera_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in cfg["camera_id"])
    # Separate durable pending-event tables per camera prevent every pipeline
    # from restoring every other camera's pending OCR/lifecycle events.
    db_path = f"{root}.{camera_id}{ext or '.db'}"
    return S2Mapper(UploadingOutbox(
        db_path,
        cfg["publisher"]["s2_url"],
        timeout=float(cfg["publisher"].get("timeout", 30.0)),
    ))


def _make_evidence_callback(source, camera_id):
    """Bind lifecycle events to this camera's rolling evidence buffer."""
    def start_event(event_id, event_time):
        evidence_start = getattr(source, "start_evidence_event", None)
        if not callable(evidence_start):
            log.info(
                "EVIDENCE_EVENT_SKIPPED camera=%s event_id=%s "
                "reason=source_has_no_rolling_evidence_buffer",
                camera_id, event_id,
            )
            return {
                "accepted": False,
                "reason": "source_has_no_rolling_evidence_buffer",
            }

        result = evidence_start(
            event_id=event_id,
            trigger_ts=event_time,
            pre_event_seconds=2.0,
            post_event_seconds=2.0,
        )
        if result.get("accepted"):
            log.info(
                "EVIDENCE_EVENT_STARTED camera=%s event_id=%s trigger_ts=%s",
                camera_id, event_id, event_time,
            )
        else:
            log.warning(
                "EVIDENCE_EVENT_REJECTED camera=%s event_id=%s reason=%s",
                camera_id, event_id, result.get("reason", "unknown"),
            )
        return result

    return start_event


def run_multi_camera(cfg, detector, show=False, no_publisher=False, cancel_event=None, frame_sink=None):
    """Run all configured cameras through a shared detector and fair scheduler."""
    cancel_event = cancel_event or threading.Event()
    configs = camera_configs(cfg)
    inference_cfg = cfg.get("inference", {})

    # Deterministic file replay (see vehicle_intelligence/determinism.py). "auto" turns it on only
    # when every source is a recorded file; live cameras are never affected.
    det = resolve_deterministic(cfg, configs)
    log.info("DETERMINISM enabled=%s reason=%s timestamp_source=%s seed=%d",
             det.enabled, det.reason, det.timestamp_source, det.seed)
    if det.enabled:
        seed_everything(det.seed)
        for camera_cfg in configs:
            camera_cfg["deterministic"] = True
            camera_cfg["replay"] = "sync"          # read EVERY frame, in order
            if (det.timestamp_source == "laptop" and is_file_source(camera_cfg["source"])
                    and camera_cfg.get("replay_start_time") is None):
                # One laptop-clock instant shared by all cameras: absolute IST
                # = laptop start + video offset, so the same video always gives
                # the same relative timeline and cameras stay mutually aligned.
                camera_cfg["replay_start_time"] = det.run_start_epoch
    cam_order = {c["camera_id"]: i for i, c in enumerate(configs)}

    def _clock(frame):
        """Scheduler clock: video time when deterministic, else monotonic."""
        return float(frame.ts) if det.enabled else time.monotonic()
    max_frame_age_ms = float(inference_cfg.get("max_frame_age_ms", 1500))
    fairness_s = float(inference_cfg.get("max_camera_wait_ms", 750)) / 1000.0
    poll_s = max(0.002, float(inference_cfg.get("scheduler_poll_ms", 5)) / 1000.0)
    activity_threshold = float(inference_cfg.get("activity_threshold", 0.025))
    startup_wait_s = float(inference_cfg.get("camera_startup_wait_s", 10.0))
    roi_cfg = inference_cfg.get("motion_roi", {}) or {}
    roi_enabled = bool(roi_cfg.get("enabled", True))
    min_blob_px = int(roi_cfg.get("min_blob_px", 6))

    runtimes = {}
    failed_cameras = set()
    processing_failures = 0
    recorder = None
    out_path = None
    transfers_reported = 0
    plate_engine_error = False
    batch_stats = {"calls": 0, "frames": 0, "max_size": 0, "sizes": {}}

    # One shared OSNet/ReID registry across every camera pipeline. Local
    # ByteTrack instances remain independent; only identity association is shared.
    from pathlib import Path
    reid_cfg = cfg.get("global_reid", {})
    reid_model = Path(__file__).resolve().parents[2] / "models" / reid_cfg.get(
        "model_name", "osnet_ain_x1_0_vehicle_reid.onnx"
    )
    arbiter = GpuArbiter()
    handoff = HandoffCoordinator.from_cfg(cfg) if cfg.get("handoff", {}).get("enabled", True) else None

    # ReID backend: OSNet if the ONNX file exists, else a colour-histogram
    # fallback (clearly labelled in the report) so the pipeline still runs end to
    # end instead of crashing. reid.enabled=false turns cross-camera ID off.
    from vehicle_intelligence.global_identity import GlobalIdentityRegistry
    reid_info = {"backend": "disabled", "model": None, "threshold": None}
    embedder = None
    threshold = float(reid_cfg.get("similarity_threshold", 0.75))
    aux_dev, shares = resolve_aux_device(
        getattr(detector, "device", "cpu"), cfg.get("gpu", {}).get("aux_device", "auto")
    )
    if reid_cfg.get("enabled", True):
        if reid_model.exists():
            from vehicle_intelligence.reid import OSNetEmbedder
            embedder = OSNetEmbedder(str(reid_model), device=aux_dev)
            reid_info = {"backend": "osnet", "model": str(reid_model), "threshold": threshold}
        else:
            from vehicle_intelligence.reid import HistogramEmbedder
            embedder = HistogramEmbedder()
            threshold = float(reid_cfg.get("fallback_similarity_threshold", 0.93))
            shares = False
            reid_info = {"backend": "histogram_fallback", "model": None, "threshold": threshold}
            log.warning(
                "OSNet model not found at %s -> using colour-histogram ReID fallback "
                "(weak: cannot separate same-colour cars). Put the .onnx in models/ for real ReID.",
                reid_model,
            )
    log.info("REID backend=%s device=%s shares_with_yolo=%s", reid_info["backend"], aux_dev, shares)
    if embedder is not None:
        identity_registry = GlobalIdentityRegistry(
            threshold=threshold,
            update_rate=float(reid_cfg.get("update_rate", 0.15)),
        )

        # Reserve every global ID recorded during ENTRY.
        restore_identity_cfg = cfg.get("exit_restore", {}) or {}
        entry_report_for_ids = restore_identity_cfg.get("entry_report_path")

        if restore_identity_cfg.get("enabled") and entry_report_for_ids:
            report_path = Path(entry_report_for_ids)
            if not report_path.is_absolute():
                report_path = Path.cwd() / report_path

            if report_path.is_file():
                import json
                with report_path.open("r", encoding="utf-8") as handle:
                    entry_identity_report = json.load(handle)

                reserved = []
                for vehicle_record in entry_identity_report.get("vehicles", []):
                    values = (
                        vehicle_record.get("global_ids")
                        or vehicle_record.get("reid_ids")
                        or []
                    )
                    if isinstance(values, str):
                        values = [values]
                    reserved.extend(values)

                    for key in ("global_id", "vehicle_id"):
                        if vehicle_record.get(key):
                            reserved.append(vehicle_record[key])

                identity_registry.reserve_existing_ids(reserved)
                log.info(
                    "Reserved %d ENTRY global IDs; next GID counter=%d",
                    len(set(reserved)), identity_registry.counter
                )

        global_reid = AsyncGlobalReID(
            embedder,
            identity_registry,
            handoff=handoff, arbiter=arbiter, shares_gpu_with_yolo=shares,
            embedding_interval_s=float(reid_cfg.get("embedding_interval_s", 0.5)),
            deterministic=det.enabled,
        )
    else:
        handoff = None
        global_reid = _NullReID()
    try:
        # Open every capture independently. One failed camera does not prevent
        # other configured cameras from starting.
        for camera_cfg in configs:
            if cancel_event.is_set():
                return None
            camera_cfg.setdefault(
                "frame_buffer_capacity",
                max(1, int(inference_cfg.get("frame_buffer_capacity", 3))),
            )
            cam = camera_cfg["camera_id"]
            try:
                geometry = Geometry.load(camera_cfg["geometry_file"])
                validate_geometry_for_camera(camera_cfg, geometry)
            except Exception:
                Diagnostic(
                    "SLOT_GEOMETRY_INVALID", "SLOT_GEOMETRY", "camera geometry",
                    "load and validate", "validation", "Camera geometry could not be loaded.",
                    True, "missing", "Skipped this camera.",
                    context={"camera_id": cam, "file": camera_cfg["geometry_file"]},
                ).log(log, exc_info=True)
                failed_cameras.add(cam)
                continue
            try:
                source = open_source(camera_cfg)
                runtimes[cam] = CameraRuntime(camera_cfg, source, geometry)
                source_label = "RTSP" if str(camera_cfg["source"]).lower().startswith(("rtsp://", "rtsps://")) else "file/device"
                log.info("camera %s capture started (%s source)", cam, source_label)
            except Exception:
                Diagnostic(
                    "VIDEO_SOURCE_OPEN_FAILED", "VIDEO_PROCESSING", "capture", "open",
                    "availability", "Camera source could not be opened.", True,
                    "missing", "Skipped this camera.", context={"camera_id": cam},
                ).log(log, exc_info=True)
                failed_cameras.add(cam)

        if cancel_event.is_set():
            return None
        if not runtimes:
            raise ProcessingError(Diagnostic(
                "MULTICAMERA_NO_CAMERAS_INITIALIZED", "MULTICAMERA", "capture", "initialize",
                "availability", "No configured cameras could be initialized.", False,
                "failed", "Stopped this run.", context={"camera_ids": list(cam_order)},
            ))

        # Start each camera's own pipeline after its first frame arrives.
        deadline = time.monotonic() + startup_wait_s
        while (not cancel_event.is_set() and time.monotonic() < deadline
               and any(rt.pipeline is None and cam not in failed_cameras for cam, rt in runtimes.items())):
            for cam, rt in runtimes.items():
                if cancel_event.is_set():
                    break
                if rt.pipeline is not None or cam in failed_cameras:
                    continue
                try:
                    frame = rt.source.read(-1, timeout=0.001)
                except Exception:
                    log.exception("VIDEO_FRAME_READ_FAILED camera=%s during startup", cam)
                    failed_cameras.add(cam)
                    continue
                if frame is None:
                    if getattr(rt.source, "eof", False):
                        log.error("VIDEO_FIRST_FRAME_UNAVAILABLE camera=%s", cam)
                        failed_cameras.add(cam)
                    continue
                if not usable_frame(frame):
                    Diagnostic(
                        "VIDEO_FRAME_INVALID", "VIDEO_PROCESSING", "capture", "first frame",
                        "validation", "Could not calibrate from an invalid starting frame.",
                        True, "missing", "Skipped this camera.", context={"camera_id": cam},
                    ).log(log)
                    failed_cameras.add(cam)
                    continue
                rt.first_frame = frame
                rt.last_idx = frame.idx
                rt.last_scheduled_monotonic = time.monotonic()
                h, w = frame.image.shape[:2]
                if roi_enabled:
                    from vehicle_intelligence.motion_roi import build_motion_mask
                    try:
                        rt.motion_mask, rt.motion_roi_info = build_motion_mask(
                            rt.geometry, (w, h),
                            margin_frac=float(roi_cfg.get("margin_frac", 0.06)),
                        )
                    except Exception:
                        log.exception("camera %s motion ROI failed; using whole frame", cam)
                        rt.motion_mask, rt.motion_roi_info = None, {"enabled": False, "error": True}
                    log.info("camera %s motion_roi=%s", cam, rt.motion_roi_info)
                try:
                    rt.publisher = _make_publisher(rt.cfg, no_publisher)

                    restore_cfg = rt.cfg.get("exit_restore", {}) or {}
                    if restore_cfg.get("enabled", False):
                        seed_result = _seed_exit_preview_sessions(
                            rt.publisher,
                            restore_cfg,
                        )
                        log.info(
                            "EXIT_PREVIEW_SESSION_SEED camera=%s result=%s",
                            cam,
                            seed_result,
                        )

                    if rt.cfg["plate"].get("enabled", True):
                        pc = rt.cfg["plate"]
                        try:
                            rt.plate_worker = PlateWorker(PlateReader(
                                pc["detector_model"], pc["ocr_model"],
                                min_ocr_conf=pc.get("min_ocr_conf", 0.5),
                                min_detector_conf=pc.get("min_plate_detector_conf", 0.25),
                            ), deterministic=det.enabled)
                        except Exception:
                            # fast-alpr missing / models could not download (Kaggle
                            # Internet OFF). Keep running: plates will be UNKNOWN.
                            Diagnostic(
                                "ALPR_MODEL_LOAD_FAILED", "ANPR_OCR", "plate worker", "initialize",
                                "initialization", "The plate reader could not initialize.",
                                True, "partial", "Continued without plate reads.",
                                context={"camera_id": cam},
                            ).log(log, exc_info=True)
                            rt.plate_worker = None
                            plate_engine_error = True
                    rt.pipeline = Pipeline(
                        rt.cfg,
                        rt.geometry,
                        (w, h),
                        detector,
                        rt.publisher,
                        rt.plate_worker,
                        evidence_event_callback=_make_evidence_callback(rt.source, cam),
                        handoff=handoff,
                    )

                    # Optional EXIT-only restoration from the original ENTRY manifest.
                    restore_cfg = rt.cfg.get("exit_restore", {}) or {}
                    if restore_cfg.get("enabled", False):
                        from vehicle_intelligence.exit_restore import restore_camera_occupants

                        manifest_path = restore_cfg.get(
                            "manifest_path",
                            "data/parking_session_manifest.exit_restore_v3.json",
                        )
                        restore_result = restore_camera_occupants(
                            rt.pipeline,
                            cam,
                            float(frame.ts),
                            manifest_path,
                            restore_cfg.get("entry_report_path"),
                        )

                        log.info(
                            "EXIT_RESTORE camera=%s restored=%d errors=%s",
                            cam,
                            restore_result["restored_count"],
                            restore_result["errors"],
                        )

                        if restore_result["errors"]:
                            raise RuntimeError(
                                "EXIT restoration validation failed: "
                                + "; ".join(restore_result["errors"])
                            )

                    rt.gate = MotionGate.from_cfg(
                        inference_cfg, tracker_max_age_s=rt.cfg["tracker"].get("max_age_s", 2.0)
                    )
                except (Exception, SystemExit):
                    Diagnostic(
                        "CAMERA_PIPELINE_INIT_FAILED", "MULTICAMERA", "pipeline",
                        "initialize", "initialization", "Camera pipeline could not initialize.",
                        True, "missing", "Skipped this camera.", context={"camera_id": cam},
                    ).log(log, exc_info=True)
                    failed_cameras.add(cam)
                    continue
                rt.pending_frame = frame
                rt.latest_raw = frame.image
                rt.frames_seen += 1
                log.info("camera %s ready: %dx%d slots=%d", cam, w, h, len(rt.geometry.slots))
            if any(rt.pipeline is None and cam not in failed_cameras for cam, rt in runtimes.items()):
                cancel_event.wait(poll_s)

        if cancel_event.is_set():
            return None
        for cam, rt in list(runtimes.items()):
            if rt.pipeline is None or cam in failed_cameras:
                if cam not in failed_cameras:
                    Diagnostic(
                        "CAMERA_STARTUP_TIMEOUT", "MULTICAMERA", "capture", "first frame",
                        "timeout", "Camera did not provide a usable frame before the startup deadline.",
                        True, "missing", "Skipped this camera.", context={"camera_id": cam},
                    ).log(log)
                    failed_cameras.add(cam)
                for resource in (rt.source, rt.plate_worker, rt.publisher):
                    if resource is not None:
                        try:
                            resource.stop()
                        except Exception:
                            log.exception("CAMERA_RESOURCE_STOP_FAILED camera=%s", cam)
                del runtimes[cam]
        if not runtimes:
            raise ProcessingError(Diagnostic(
                "MULTICAMERA_NO_USABLE_CAMERA", "MULTICAMERA", "capture", "first frame",
                "availability", "No configured cameras produced a usable frame.", False,
                "failed", "Stopped this run.", context={"camera_ids": list(cam_order)},
            ))

        recorder = None
        demo_cfg = cfg.get("demo_video", {}) or {}
        if demo_cfg.get("enabled", False):
            recorder = DemoRecorder(
                demo_cfg.get("path", "data/demo_multicam.mp4"), list(runtimes.keys()),
                fps=float(demo_cfg.get("fps", 10)), tile_height=int(demo_cfg.get("tile_height", 720)),
                frame_sink=frame_sink,
            )
            log.info("DEMO_VIDEO recording to %s", recorder.path)
        transfers_reported = 0

        if show:
            for cam in runtimes:
                cv2.namedWindow(f"garage-s1-{cam}", cv2.WINDOW_NORMAL)
                cv2.resizeWindow(f"garage-s1-{cam}", 600, 900)

        while not cancel_event.is_set():
            now_mono = time.monotonic()
            now_wall = time.time()
            any_live = False

            if recorder is not None:
                recorder.tick(
                    runtimes, {"reid": global_reid, "handoff": handoff},
                    max((float(getattr(r.pipeline, "display_ts", 0.0)) for r in runtimes.values()),
                        default=0.0) if det.enabled else now_mono,
                )
                if handoff is not None and len(handoff.transfers) > transfers_reported:
                    for tr in handoff.transfers[transfers_reported:]:
                        kind = "TRANSFER_IN" if tr["raw_event"] == "ENTRY" else "TRANSFER_OUT"
                        recorder.add_event(tr["camera_id"], kind, f"{tr['global_id']} {tr['reason']}", tr["event_ts"])
                    transfers_reported = len(handoff.transfers)

            # Poll all cameras without blocking. Each grabber owns its own
            # decoder thread and bounded latest-frame storage.
            for cam, rt in runtimes.items():
                if cam in failed_cameras or getattr(rt.source, "eof", False):
                    continue
                any_live = True
                # Do not pull another frame while one is awaiting inference.
                # The capture worker continues independently and fills only its
                # configured bounded buffer.
                if rt.pending_frame is not None:
                    continue
                try:
                    frame = rt.source.read(rt.last_idx, timeout=0.0)
                except Exception:
                    Diagnostic(
                        "VIDEO_FRAME_READ_FAILED", "VIDEO_PROCESSING", "capture", "read",
                        "decode", "Camera source could not supply its next frame.", True,
                        "partial", "Stopped scheduling this camera.",
                        context={"camera_id": cam, "last_frame_id": rt.last_idx},
                    ).log(log, exc_info=True)
                    failed_cameras.add(cam)
                    continue
                if frame is None:
                    continue
                if not usable_frame(frame, rt.first_frame.image.shape):
                    processing_failures += 1
                    if isinstance(getattr(frame, "idx", None), int):
                        rt.last_idx = max(rt.last_idx, frame.idx)
                    Diagnostic(
                        "VIDEO_FRAME_INVALID", "VIDEO_PROCESSING", "capture", "validate",
                        "validation", "Frame is invalid or differs from the calibrated frame size.",
                        True, "missing", "Skipped this frame.", context={"camera_id": cam},
                    ).log(log)
                    continue
                if rt.pending_frame is not None:
                    rt.dropped_frames += 1
                rt.pending_frame = frame
                rt.latest_raw = frame.image
                rt.frames_seen += 1
                rt.last_idx = frame.idx
                rt.last_gray, rt.activity = _activity_score(
                    rt.last_gray, frame.image, rt.motion_mask, min_blob_px
                )
                if rt.gate is not None:
                    rt.gate.observe(rt.activity, _clock(frame))

            candidates = [(cam, rt) for cam, rt in runtimes.items() if rt.pending_frame is not None]
            if not candidates:
                if not any_live:
                    break
                cancel_event.wait(poll_s)
                continue

            # Select a fair batch of currently ready camera frames.
            batch_size = max(1, int(inference_cfg.get("batch_size", 4)))
            eligible = []

            for candidate_cam, candidate_rt in candidates:
                candidate_frame = candidate_rt.pending_frame
                candidate_fps = max(
                    0.1, float(candidate_rt.cfg["detector"].get("max_fps", 5))
                )
                candidate_interval = (
                    0.0 if candidate_rt.cfg.get("replay") == "sync"
                    else 1.0 / candidate_fps
                )
                candidate_clock = _clock(candidate_frame)

                if det.enabled:
                    # Same max_fps cap as live replay, but measured on the video
                    # clock: frames inside the interval are skipped, not queued.
                    if candidate_clock - candidate_rt.last_infer_monotonic < candidate_interval * 0.999:
                        candidate_rt.pending_frame = None
                        continue
                elif now_mono - candidate_rt.last_infer_monotonic < candidate_interval:
                    continue

                candidate_age_ms = max(
                    0.0, (now_wall - candidate_frame.ts) * 1000.0
                )
                # Stale-frame dropping is for LIVE cameras. A replayed file with a
                # fixed replay_start_time has timestamps unrelated to wall clock,
                # so every frame would look "stale" and be dropped.
                if (
                    candidate_rt.cfg.get("replay") != "sync"
                    and not getattr(candidate_rt.source, "is_file", False)
                    and candidate_age_ms > max_frame_age_ms
                ):
                    candidate_rt.pending_frame = None
                    candidate_rt.dropped_frames += 1
                    candidate_rt.last_scheduled_monotonic = now_mono
                    log.warning(
                        "camera=%s dropping stale frame age=%.0fms max=%.0fms",
                        candidate_cam, candidate_age_ms, max_frame_age_ms,
                    )
                    continue

                # Pre-YOLO motion gate: idle cameras are not sent to the GPU.
                if candidate_rt.gate is not None:
                    has_tracks = bool(candidate_rt.pipeline.last_tracks)
                    run_it, why = candidate_rt.gate.allow(
                        candidate_clock, candidate_rt.last_infer_monotonic, has_tracks
                    )
                    if not run_it:
                        candidate_rt.pending_frame = None
                        candidate_rt.skipped_idle += 1
                        continue

                waited = candidate_clock - candidate_rt.last_scheduled_monotonic
                is_overdue = waited >= fairness_s
                eligible.append((
                    candidate_cam, candidate_rt, candidate_frame,
                    candidate_age_ms, waited, is_overdue,
                ))

            if not eligible:
                if not det.enabled:
                    cancel_event.wait(poll_s)
                continue

            # Overdue cameras first; then activity and waiting time.
            if det.enabled:
                # Fixed order => identical batch composition every run.
                eligible.sort(key=lambda item: (item[2].ts, cam_order[item[0]]))
            else:
                eligible.sort(
                    key=lambda item: (
                        not item[5],
                        -item[4],
                        -item[1].activity,
                    )
                )
            selected = eligible[:batch_size]

            if cancel_event.is_set():
                break
            batch_images = []
            for candidate_cam, candidate_rt, candidate_frame, *_ in selected:
                candidate_rt.pending_frame = None
                batch_images.append(candidate_frame.image)

            try:
                batch_start = time.perf_counter()
                arbiter.yolo_enter()
                try:
                    batch_detections = detector.detect_batch(batch_images)
                finally:
                    arbiter.yolo_exit()
                batch_elapsed_ms = (
                    time.perf_counter() - batch_start
                ) * 1000.0

                if len(batch_detections) != len(selected):
                    raise RuntimeError(
                        f"Batch output mismatch: {len(batch_detections)} "
                        f"outputs for {len(selected)} frames"
                    )

                per_frame_infer_ms = batch_elapsed_ms / len(selected)
                batch_stats["calls"] += 1
                batch_stats["frames"] += len(selected)
                batch_stats["max_size"] = max(batch_stats["max_size"], len(selected))
                batch_stats["sizes"][len(selected)] = batch_stats["sizes"].get(len(selected), 0) + 1

                log.info(
                    "YOLO_BATCH size=%d elapsed_ms=%.1f avg_frame_ms=%.1f",
                    len(selected), batch_elapsed_ms, per_frame_infer_ms,
                )

                # Preserve independent per-camera pipeline/tracker processing.
                for item, detections in zip(selected, batch_detections):
                    cam, rt, frame, frame_age_ms, *_ = item
                    try:
                        events = rt.pipeline.process(
                            frame,
                            detections=detections,
                            detection_elapsed_ms=per_frame_infer_ms,
                        )

                        if not rt.pipeline.last_frame_ok:
                            processing_failures += 1
                            rt.last_scheduled_monotonic = _clock(frame)
                            continue

                        # Attach global identity to each camera-local track. OCR,
                        # type and colour continue to come from the existing
                        # per-camera VehicleRegistry; no tracker is replaced.
                        assignments = global_reid.update(
                            cam, frame.image, rt.pipeline.last_tracks, frame.ts
                        )
                        for track in rt.pipeline.last_tracks:
                            local_id = int(track.id)
                            gid = assignments.get(local_id)
                            vehicle = rt.pipeline.registry.by_track.get(local_id)
                            consensus = None
                            if vehicle is not None:
                                try:
                                    consensus = vehicle.voter.consensus()
                                except Exception:
                                    consensus = None
                            cls_total = sum(getattr(vehicle, "cls_votes", {}).values()) if vehicle else 0
                            cls_top = max(getattr(vehicle, "cls_votes", {}).items(), key=lambda x: x[1]) if vehicle and vehicle.cls_votes else (None, 0)
                            colour_total = sum(getattr(vehicle, "color_votes", {}).values()) if vehicle else 0
                            colour_top = max(getattr(vehicle, "color_votes", {}).items(), key=lambda x: x[1]) if vehicle and vehicle.color_votes else (None, 0)
                            rt.track_records[f"{cam}:{local_id}"] = {
                                "camera_id": cam,
                                "local_track_id": local_id,
                                "global_id": gid,
                                "track_confirmed": bool(getattr(track, "confirmed", False)),
                                "first_seen_source_clock_seconds": float(getattr(track, "first_ts", frame.ts)),
                                "last_seen_source_clock_seconds": float(frame.ts),
                                "first_seen_video_seconds": float(getattr(track, "first_ts", frame.ts) - rt.first_frame.ts),
                                "last_seen_video_seconds": float(frame.ts - rt.first_frame.ts),
                                "vehicle_uid": getattr(vehicle, "uid", None),
                                "vehicle_type": vehicle.vehicle_type() if vehicle else None,
                                "vehicle_type_confidence": (float(cls_top[1]) / cls_total) if cls_total else None,
                                "colour": vehicle.color() if vehicle else None,
                                "colour_confidence": (float(colour_top[1]) / colour_total) if colour_total else None,
                                "number_plate": getattr(consensus, "text", None) if consensus else None,
                                "plate_status": ("CONFIRMED" if vehicle and getattr(vehicle, "plate_final", False) and consensus and consensus.valid else ("CANDIDATE" if consensus else "UNKNOWN")),
                                "ocr_confidence": float(consensus.conf) if consensus else None,
                                "ocr_consensus_reads": int(consensus.n_reads) if consensus else 0,
                            }
                        rt.processed_frames += 1
                        rt.last_infer_monotonic = _clock(frame)
                        rt.last_scheduled_monotonic = rt.last_infer_monotonic
                        rt.activity *= 0.5

                        for ev in events:
                            event_type = str(ev.get("event_type") or "UNKNOWN")
                            rt.event_counts[event_type] = (
                                rt.event_counts.get(event_type, 0) + 1
                            )
                            rt.event_history.append({
                                "event_type": event_type,
                                "event_time": ev.get("event_time") or ev.get("timestamp"),
                                "plate": (ev.get("plate") or {}).get("text"),
                                "plate_status": ev.get("plate_status", "UNKNOWN"),
                                "slot": (ev.get("bay") or {}).get("id"),
                            })
                            if len(rt.event_history) > 5000:
                                del rt.event_history[:1000]

                            if recorder is not None:
                                recorder.add_event(
                                    cam, event_type,
                                    f"plate={(ev.get('plate') or {}).get('text') or '?'} slot={(ev.get('bay') or {}).get('id') or '-'}",
                                    frame.ts,
                                )
                            log.info(
                                "camera=%s event=%s plate=%s slot=%s event_time=%s",
                                cam, event_type,
                                (ev.get("plate") or {}).get("text"),
                                (ev.get("bay") or {}).get("id"),
                                ev.get("event_time") or ev.get("timestamp"),
                            )

                        if show:
                            display = rt.pipeline.draw(frame.image.copy())
                            cv2.imshow(f"garage-s1-{cam}", display)
                            key = cv2.waitKey(1) & 0xFF
                            if key == ord("q"):
                                cancel_event.set()
                            elif key == ord(" "):
                                log.info(
                                    "Multi-camera pause is not enabled; capture remains independent"
                                )

                        if time.monotonic() - rt.last_metrics_log >= 10:
                            log.info(
                                "CAMERA_METRICS camera=%s seen=%d processed=%d dropped=%d "
                                "frame_age_ms=%.1f infer_ms=%.1f activity=%.3f idle_skipped=%d",
                                cam, rt.frames_seen, rt.processed_frames,
                                rt.dropped_frames + int(
                                    getattr(rt.source, "dropped_frames", 0)
                                ),
                                frame_age_ms, per_frame_infer_ms, rt.activity,
                                rt.skipped_idle,
                            )
                            rt.last_metrics_log = time.monotonic()

                    except Exception:
                        processing_failures += 1
                        log.exception(
                            "camera=%s frame processing failed; other cameras continue",
                            cam,
                        )
                        rt.last_scheduled_monotonic = time.monotonic()

            except Exception:
                processing_failures += len(selected)
                Diagnostic(
                    "YOLO_BATCH_INFERENCE_FAILED", "VEHICLE_DETECTION", "detector", "infer batch",
                    "inference", "Shared inference produced no usable batch.", True,
                    "missing", "Dropped affected frames and scheduled subsequent frames.",
                    context={"frames": [{"camera_id": cam, "frame_id": frame.idx}
                                        for cam, _, frame, *_ in selected]},
                ).log(log, exc_info=True)
                for cam, rt, frame, *_ in selected:
                    rt.last_scheduled_monotonic = time.monotonic()

    except KeyboardInterrupt:
        cancel_event.set()
        log.info("multi-camera shutdown requested")
    finally:
        processing_exception = sys.exc_info()[0] is not None
        if cancel_event.is_set():
            log.info("MULTICAMERA_CANCELLED finalizing processed frames and events")
        for cam, rt in runtimes.items():
            try:
                rt.source.stop()
            except Exception:
                log.exception("camera %s capture shutdown failed", cam)
        for cam, rt in runtimes.items():
            try:
                if rt.plate_worker is not None:
                    rt.plate_worker.stop(wait=True)
            except Exception:
                log.exception("camera %s OCR shutdown failed", cam)
        try:
            global_reid.stop()
        except Exception:
            log.exception("MULTICAMERA_REID_STOP_FAILED")
        try:
            if recorder is not None:
                if not cancel_event.is_set() or any(rt.processed_frames for rt in runtimes.values()):
                    recorder.tick(runtimes, {"reid": global_reid, "handoff": handoff}, time.monotonic(), force=True)
                out_path = recorder.close(cancel_event=cancel_event)
                if out_path is None:
                    log.error("VIDEO_OUTPUT_UNAVAILABLE No playable output was produced")
                else:
                    print(f"DEMO VIDEO: {out_path} ({recorder.frames_written} frames, {recorder.errors} render errors)")
        except Exception:
            log.exception("demo video finalisation failed")
        for cam, rt in runtimes.items():
            try:
                if rt.pipeline is not None:
                    if det.enabled or cancel_event.is_set():
                        last_ts = float(getattr(rt.pipeline, "display_ts", time.time()))
                    else:
                        last_ts = (handoff.clock.get(cam, time.time()) if handoff else time.time())
                    rt.pipeline.flush(last_ts)
            except Exception:
                log.exception("camera %s pipeline flush failed", cam)
        for cam, rt in runtimes.items():
            try:
                if rt.publisher is not None:
                    rt.publisher.stop(wait=True)
            except Exception:
                log.exception("camera %s publisher shutdown failed", cam)
        plate_evidence = []
        try:
            from vehicle_intelligence.plate_evidence import save_best_plate_evidence
            evidence_dir = (cfg.get("report", {}) or {}).get("plate_evidence_dir", "data/plate_evidence")
            plate_evidence = save_best_plate_evidence(runtimes, evidence_dir)
        except Exception:
            log.exception("plate evidence export failed; report continues without it")
        report_error = None
        try:
            from vehicle_intelligence.report import build_final_report, write_report, print_summary
            for cam, runtime in runtimes.items():
                runtime.occupancy_unavailable = cam in failed_cameras or runtime.processed_frames == 0
            report = build_final_report(
                runtimes, handoff, global_reid, reid_info, cfg,
                no_publisher, batch_stats, plate_engine_error,
                determinism=det.as_dict(), plate_evidence=plate_evidence,
            )
            report_path = (cfg.get("report", {}) or {}).get("path", "smart_parking_final_report.json")
            report["failed_camera_ids"] = sorted(failed_cameras)
            report["processing_failures"] = processing_failures
            report["processing_status"] = (
                "cancelled" if cancel_event.is_set() else
                "failed" if processing_exception or not any(rt.processed_frames for rt in runtimes.values()) else
                "partial" if failed_cameras or processing_failures or plate_engine_error
                or (recorder and (recorder.errors or not out_path)) else
                "completed"
            )
            write_report(report, report_path)
            print(f"JSON report saved: {report_path}")
            print_summary(report)
        except Exception as exc:
            diagnostic = Diagnostic(
                "REPORT_GENERATION_FAILED", "REPORTING", "multicamera report", "write",
                "output", "The final report could not be generated.", True,
                "partial", "Kept processed events and any independent outputs.",
            )
            diagnostic.log(log, exc_info=True)
            report_error = (diagnostic, exc)

        if show:
            cv2.destroyAllWindows()
        if not processing_exception and report_error is not None:
            diagnostic, cause = report_error
            raise ProcessingError(diagnostic) from cause

    if not cancel_event.is_set() and not any(rt.processed_frames for rt in runtimes.values()):
        raise ProcessingError(Diagnostic(
            "MULTICAMERA_NO_FRAMES_PROCESSED", "MULTICAMERA", "scheduler", "process",
            "availability", "No camera completed frame processing.", False,
            "failed", "Released camera resources.",
            context={"camera_ids": list(cam_order)},
        ))
    return out_path
