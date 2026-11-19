r"""One-command runner for a laptop / workstation with an NVIDIA GPU.

    python run_local.py                       # videos taken from ./videos
    python run_local.py --videos D:\clips     # or any folder holding the .MOV files
    python run_local.py --publish             # send to Service 2 (default: preview only)

Writes: smart_parking_final_report.json (see "lifecycle_timeline"), data/plate_evidence/,
logs/s1.log. File replay is deterministic: same videos -> same result every run.
"""
import argparse
import glob
import logging
import os
from pathlib import PureWindowsPath
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.chdir(ROOT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/config.local.yaml")
    ap.add_argument("--videos", default=os.environ.get("SMART_PARKING_VIDEOS", "videos"))
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--no-plates", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    from s1.obs import setup_logging
    run_id = setup_logging(level="INFO", log_dir="logs", quiet=a.quiet)
    log = logging.getLogger("run_local")
    print(f"[run] run_id={run_id}  log: {os.path.abspath('logs/s1.log')}")

    from s1.config import load_config
    cfg = load_config(a.config, {"plate": {"enabled": False}} if a.no_plates else {})

    problems = []
    for cam in cfg.get("cameras", [cfg]):
        src = str(cam["source"])
        if a.no_plates:
            cam["plate"] = {**cam.get("plate", {}), "enabled": False}
        if "://" not in src and not src.isdecimal() and not os.path.exists(src):
            name = PureWindowsPath(src).name
            hits = sorted(glob.glob(os.path.join(a.videos, "**", glob.escape(name)), recursive=True))
            if len(hits) == 1:
                cam["source"] = hits[0]
            elif hits:
                problems.append(f"{cam['camera_id']}: several videos match '{name}'; configure one path")
            else:
                log.warning("VIDEO_SOURCE_NOT_FOUND camera=%s name=%s", cam["camera_id"], name)
    if not os.path.exists(cfg["detector"]["model_path"]):
        problems.append(f"YOLO weights missing: {cfg['detector']['model_path']}")
    if problems:
        log.error("cannot start: %s", "; ".join(problems))
        print("\nCannot start:\n  - " + "\n  - ".join(problems))
        sys.exit(2)

    if str(cfg["detector"].get("device", "cpu")).startswith("cuda"):
        try:
            import torch
            if not torch.cuda.is_available():
                log.warning("no CUDA GPU visible -> YOLO on CPU (slow). Install the CUDA build of torch.")
                cfg["detector"]["device"] = "cpu"
            else:
                log.info("GPU: %s", torch.cuda.get_device_name(0))
        except Exception as exc:
            log.warning("torch import failed (%s) -> YOLO on CPU", exc)
            cfg["detector"]["device"] = "cpu"

    from s1.main import run
    run(cfg, show=False, no_publisher=not a.publish)
    rep = (cfg.get("report", {}) or {}).get("path", "smart_parking_final_report.json")
    print(f"\nDone. Report: {os.path.abspath(rep)}")


if __name__ == "__main__":
    main()
