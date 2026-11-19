"""Create a local, publishing-disabled preview configuration from an authorized video."""
import argparse
from pathlib import Path
import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--geometry", type=Path)
    parser.add_argument("--camera-index", type=int, default=0)
    parser.add_argument("--template", type=Path, default=Path("config/config.local.yaml"))
    args = parser.parse_args()
    config = yaml.safe_load(args.template.read_text())
    cameras = config.get("cameras", [])
    if not 0 <= args.camera_index < len(cameras):
        parser.error("camera index does not select a configured calibration")
    camera = dict(cameras[args.camera_index])
    geometry = args.geometry or Path(camera["geometry_file"])
    model = Path(config["detector"]["model_path"])
    for path in (args.source, geometry, model):
        if not path.is_file():
            parser.error(f"required local file missing: {path}")
    directory = args.output.resolve().parent
    directory.mkdir(parents=True, exist_ok=True)
    camera.update(source=str(args.source.resolve()), geometry_file=str(geometry.resolve()), replay="sync")
    camera["plate"] = {**camera.get("plate", {}), "enabled": False}
    config.update({
        "cameras": [camera], "replay": "sync",
        "detector": {**config["detector"], "device": "cpu", "model_path": str(model.resolve())},
        "global_reid": {"enabled": False}, "camera_motion": {"enabled": False},
        "exit_restore": {"enabled": False, "preview_db_dir": str(directory / "preview-db")},
        "publisher": {"s2_url": "http://127.0.0.1:1/isolated", "outbox_db": str(directory / "outbox.db"),
                      "snapshot_dir": str(directory / "snapshots")},
        "report": {"path": str(directory / "report.json"), "plate_evidence_dir": str(directory / "evidence")},
        "demo_video": {"enabled": False},
    })
    with args.output.open("x") as stream:
        yaml.safe_dump(config, stream, sort_keys=False)
    print(args.output)


if __name__ == "__main__":
    main()
