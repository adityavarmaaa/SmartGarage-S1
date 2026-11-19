import copy
import math

import yaml

from .errors import ConfigurationError, Diagnostic

DEFAULTS = {
    "camera_id": "gate-cam-01",
    "source": "sample.mp4",
    "replay": "realtime",          # realtime = drop frames like a live camera | sync = every frame
    "replay_start_time": None,     # epoch seconds for file replay; None = now
    "geometry_file": "config/geometry.json",
    "detector": {
        "model_path": "models/yolox_nano.onnx",
        "input_size": 416,
        "score_thr": 0.15,
        "nms_thr": 0.45,
        "classes": {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"},
        "max_fps": 5,
        "threads": 2,
    },
    "tracker": {
        "high_thr": 0.45, "low_thr": 0.15,
        "match_iou": 0.25, "low_match_iou": 0.15,
        "max_age_s": 2.0, "min_hits": 3,
    },
    "gate": {"margin_px": 12, "size_samples": 3, "min_size_change": 0.15},
    "color": {"interval_s": 1.0, "max_samples": 8, "min_box_h": 60},
    "plate": {
        "enabled": True,
        "min_vehicle_height_px": 100,
        "interval_s": 0.3,
        "detector_model": "yolo-v9-t-384-license-plate-end2end",
        "ocr_model": "cct-xs-v2-global-model",
        "min_ocr_conf": 0.5,
        "confirm_reads": 3,
        "confirm_conf": 0.75,
        "max_attempts": 40,
        "emit_deadline_s": 3.0,
        "min_crop_brightness": 8.0,
        "max_crop_brightness": 247.0,
        "min_crop_contrast": 5.0,
        "min_crop_laplacian_var": 2.0,
        "min_plate_detector_conf": 0.30,
        "min_plate_width_px": 24,
        "min_plate_height_px": 8,
        "min_plate_aspect_ratio": 1.5,
        "max_plate_aspect_ratio": 7.0,
    },
    "parking": {
        "footprint_frac": 0.25,
        "min_overlap": 0.18,  # allow stable, lower-overlap bay candidates
        "leave_overlap": 0.3,
        "park_dwell_s": 4.0,
        "leave_dwell_s": 3.0,
        "still_frac": 0.12,
        "moved_frac": 0.35,
        "gone_frac": 1.0,
        "lost_grace_s": 30.0,
        "baseline_window_s": 2.0,
        # Short gaps and slot-boundary flicker must not reset parking dwell.
        "candidate_gap_grace_s": 0.6,
        "candidate_switch_confirm_s": 0.6,
        "publish_exit": False,
    },
    "publisher": {
        "s2_url": "https://smart-parking-data-service.onrender.com/internal/events/vehicle",
        "outbox_db": "data/outbox.db",
        "snapshot_dir": "data/snapshots",
    },
    "inference": {
        "frame_buffer_capacity": 3,
        "max_frame_age_ms": 1500,
        "max_camera_wait_ms": 750,
        "scheduler_poll_ms": 5,
        "activity_threshold": 0.025,
        "camera_startup_wait_s": 10,
        "motion_gate": {
            "enabled": True,
            "on_threshold": 0.025,
            "off_ratio": 0.5,
            "hold_s": 3.0,
            "wake_frames": 1,
            "idle_track_heartbeat_s": 0.5,
            "idle_empty_heartbeat_s": 3.0,
        },
    },
    "gpu": {"aux_device": "auto"},
    "demo_video": {"enabled": False, "path": "data/demo_multicam.mp4", "fps": 10, "tile_height": 720},
    "handoff": {
        "enabled": True,
        "entry_wait_s": 4.0,
        "exit_wait_s": 8.0,
        "transfer_window_s": 30.0,
    },
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=None, overrides=None):
    data = {}
    try:
        if path:
            with open(path, "r", encoding="utf-8") as stream:
                data = yaml.safe_load(stream)
            if data is None:
                data = {}
        for name, section in (("YAML root", data), ("overrides", overrides)):
            if section is not None and not isinstance(section, dict):
                raise ValueError(f"{name} must be a mapping")
        cfg = _merge(_merge(DEFAULTS, data), overrides)
        for name, default in DEFAULTS.items():
            if isinstance(default, dict) and not isinstance(cfg[name], dict):
                raise ValueError(f"{name} must be a mapping")
        if cfg["replay"] not in ("realtime", "sync"):
            raise ValueError("replay must be realtime or sync")
        fps = cfg["detector"]["max_fps"]
        if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or fps <= 0:
            raise ValueError("detector.max_fps must be a finite positive number")
        camera_configs(cfg)
        return cfg
    except (OSError, yaml.YAMLError, ValueError) as exc:
        reason = type(exc).__name__ if isinstance(exc, yaml.YAMLError) else str(exc)
        raise ConfigurationError(Diagnostic(
            "CONFIG_VALIDATION_FAILED", "CONFIGURATION", "config", "load",
            "validation", f"Configuration could not be loaded: {reason}", False,
            "failed", "Stopped initialization.", context={"file": str(path) if path else None},
        )) from exc


def camera_configs(cfg):
    """Return validated, fully merged per-camera configs.

    Legacy single-camera YAML remains a one-camera configuration. In a
    multi-camera YAML, each entry overrides the shared top-level defaults.
    """
    if "cameras" not in cfg:
        return [copy.deepcopy(cfg)]
    cameras = cfg.get("cameras")
    if not isinstance(cameras, list) or not cameras:
        raise ValueError("'cameras' must be a non-empty list")

    shared = {k: v for k, v in cfg.items() if k != "cameras"}
    result = []
    seen = set()
    for index, camera in enumerate(cameras):
        if not isinstance(camera, dict):
            raise ValueError(f"cameras[{index}] must be a mapping")
        camera_id = str(camera.get("camera_id", "")).strip()
        source = camera.get("source")
        geometry_file = camera.get("geometry_file")
        if not camera_id:
            raise ValueError(f"cameras[{index}] is missing camera_id")
        if camera_id in seen:
            raise ValueError(f"duplicate camera_id: {camera_id}")
        if source is None or (isinstance(source, str) and not source.strip()):
            raise ValueError(f"camera {camera_id} is missing source")
        if not geometry_file:
            raise ValueError(f"camera {camera_id} is missing geometry_file")
        seen.add(camera_id)
        merged = _merge(shared, camera)
        merged["camera_id"] = camera_id
        merged.pop("cameras", None)
        result.append(merged)
    return result


def validate_geometry_for_camera(camera_cfg, geometry):
    """Validate calibrated slot geometry without fabricating missing polygons."""
    size = geometry.frame_size
    if size != (0, 0) and (
        len(size) != 2 or any(not isinstance(value, int) or isinstance(value, bool)
                              or value <= 0 for value in size)
    ):
        raise ValueError(f"camera {camera_cfg['camera_id']} frame_size needs two positive pixel dimensions")
    slots = geometry.slots
    slot_ids = set()
    for index, slot in enumerate(slots):
        sid = str(slot.get("slot_id", "")).strip()
        if not sid:
            raise ValueError(
                f"camera {camera_cfg['camera_id']} geometry slot {index} is missing slot_id"
            )
        if sid in slot_ids:
            raise ValueError(
                f"camera {camera_cfg['camera_id']} has duplicate slot_id {sid!r}"
            )
        slot_ids.add(sid)
        polygon = slot.get("polygon")
        if not isinstance(polygon, list) or len(polygon) < 3:
            raise ValueError(
                f"camera {camera_cfg['camera_id']} slot {sid!r} needs a polygon with at least 3 points"
            )
        for point in polygon:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError(
                    f"camera {camera_cfg['camera_id']} slot {sid!r} has an invalid polygon point"
                )
            try:
                valid = all(math.isfinite(float(value)) for value in point)
            except (TypeError, ValueError):
                valid = False
            if not valid:
                raise ValueError(
                    f"camera {camera_cfg['camera_id']} slot {sid!r} has non-finite coordinates"
                )
    def finite_point(point):
        if not isinstance(point, (tuple, list)) or len(point) != 2:
            return False
        return all(isinstance(value, (int, float)) and not isinstance(value, bool)
                   and math.isfinite(value) for value in point)

    if (len(geometry.tripwire) != 2
            or not all(finite_point(point) for point in geometry.tripwire)
            or geometry.tripwire[0] == geometry.tripwire[1]
            or not finite_point(geometry.inside_point)):
        raise ValueError(
            f"camera {camera_cfg['camera_id']} geometry needs distinct, finite tripwire points and an inside_point"
        )
    return True
