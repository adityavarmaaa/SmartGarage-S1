"""One-command Kaggle runner.

    python run_kaggle.py                 # test mode: JSON previewed, NOTHING is sent
    python run_kaggle.py --publish       # send ENTRY/EXIT to Service 2 while running
    python run_kaggle.py --config config/config.kaggle_flow.yaml --no-plates

What it does for you:
  * finds the videos under /kaggle/input if the configured path is wrong
  * falls back to CPU if no GPU is visible
  * warns (does not crash) if OSNet / plate models are missing
  * runs the pipeline, writes smart_parking_final_report.json, prints a summary
"""
import argparse
import glob
import logging
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.chdir(ROOT)


def resolve_sources(cfg):
    problems = []
    for cam in cfg["cameras"]:
        src = str(cam["source"])
        if src.lower().startswith(("rtsp://", "rtsps://", "http")) or os.path.isfile(src):
            continue
        name = os.path.basename(src)
        hits = glob.glob(os.path.join("/kaggle/input", "**", name), recursive=True)
        if hits:
            print(f"[fix] {cam['camera_id']}: {src} -> {hits[0]}")
            cam["source"] = hits[0]
        else:
            problems.append(f"{cam['camera_id']}: video not found: {src} (attach the Kaggle dataset)")
        if not os.path.isfile(cam["geometry_file"]):
            problems.append(f"{cam['camera_id']}: geometry missing: {cam['geometry_file']}")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/config.kaggle_flow.yaml")
    ap.add_argument("--publish", action="store_true", help="send events to Service 2 (default: preview only)")
    ap.add_argument("--no-plates", action="store_true")
    ap.add_argument("--replay", choices=["realtime", "sync"])
    ap.add_argument("--demo-video", action="store_true", help="force the tiled demo mp4 on")
    ap.add_argument("--quiet", action="store_true", help="WARNING-level logs only")
    a = ap.parse_args()

    try:
        from s1.obs import setup_logging
        run_id = setup_logging(level="INFO", log_dir="logs", quiet=a.quiet)
        print(f"[run] run_id={run_id}  log file: {os.path.abspath('logs/s1.log')}")
    except Exception as exc:  # never let logging setup block a run
        logging.basicConfig(level=logging.WARNING if a.quiet else logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        logging.getLogger("run_kaggle").error("production logging setup failed (%s); using basic logging", exc)

    from s1.config import load_config
    over = {}
    if a.no_plates:
        over["plate"] = {"enabled": False}
    if a.replay:
        over["replay"] = a.replay
    if a.demo_video:
        over["demo_video"] = {"enabled": True}
    cfg = load_config(a.config, over)

    problems = resolve_sources(cfg)
    if not os.path.exists(cfg["detector"]["model_path"]):
        problems.append(f"YOLO weights missing: {cfg['detector']['model_path']}")
    if problems:
        print("\nCannot start:\n  - " + "\n  - ".join(problems))
        sys.exit(2)

    dev = str(cfg["detector"].get("device", "cpu"))
    if dev.startswith("cuda"):
        try:
            import torch
            if not torch.cuda.is_available():
                print("[warn] no CUDA GPU visible -> YOLO on CPU (turn on a GPU accelerator in Kaggle)")
                cfg["detector"]["device"] = "cpu"
        except Exception:
            cfg["detector"]["device"] = "cpu"

    if not os.path.exists(os.path.join("models", cfg.get("global_reid", {}).get(
            "model_name", "osnet_ain_x1_0_vehicle_reid.onnx"))):
        print("[warn] OSNet .onnx not in models/ -> weak colour-histogram ReID fallback will be used")

    from s1.main import run
    run(cfg, show=False, no_publisher=not a.publish)

    rep = cfg.get("report", {}).get("path", "smart_parking_final_report.json")
    print(f"\nDone. Report: {os.path.abspath(rep)}")
    if not a.publish:
        print(f"Nothing was sent. To send the ENTRY/EXIT payloads:\n"
              f"  python tools/send_report_to_s2.py {rep} --send")


if __name__ == "__main__":
    main()
