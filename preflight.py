"""Check configuration and input prerequisites without starting the pipeline."""
import argparse
from pathlib import Path

from vehicle_intelligence.common.config import camera_configs, load_config, validate_geometry_for_camera
from vehicle_intelligence.common.errors import ConfigurationError
from vehicle_intelligence.geometry import Geometry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", nargs="?", default="config/config.local.yaml")
    options = parser.parse_args()
    try:
        cfg = load_config(options.config)
    except ConfigurationError as exc:
        print(f"FAIL {exc}")
        return 1

    def check(name, ok, detail="", optional=False):
        print(f"{'OK' if ok else 'WARN' if optional else 'FAIL'} {name}"
              + (f" — {detail}" if detail else ""))
        return ok or optional

    ready = check("detector weights", Path(cfg["detector"]["model_path"]).is_file(),
                  cfg["detector"]["model_path"])
    ready_cameras = 0
    for camera in camera_configs(cfg):
        camera_id = camera["camera_id"]
        source = str(camera["source"])
        source_ready = "://" in source or source.isdecimal() or Path(source).exists()
        check(f"{camera_id} source", source_ready,
              "Local input is missing" if not source_ready else "Availability checked during startup")
        try:
            geometry = Geometry.load(camera["geometry_file"])
            validate_geometry_for_camera(camera, geometry)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            check(f"{camera_id} geometry", False, str(exc))
        else:
            check(f"{camera_id} geometry", True)
            ready_cameras += int(source_ready)
    try:
        from ultralytics import YOLO  # noqa: F401
    except ImportError as exc:
        ready = check("Ultralytics import", False, str(exc))
    else:
        check("Ultralytics import", True)
    if any(camera["plate"].get("enabled", False) for camera in camera_configs(cfg)):
        try:
            import fast_alpr  # noqa: F401
        except ImportError:
            check("ANPR dependency", False, "Install fast-alpr for plate reads", optional=True)
    ready = ready and bool(ready_cameras)
    print("READY" if ready else "NOT READY")
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
