"""Inventory supplied project media and run bounded CPU detector/tracker probes."""
import argparse
import csv
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import subprocess

import cv2
import numpy as np
import yaml

from s1.vehicle_front.detection import YoloDetector
from s1.vehicle_front.tracking import ByteTracker


def annotate_frame(image, tracks, frame_idx, detection_count, injected=False):
    """Overlay probe observations without resampling the decoded image."""
    plane = image.copy()
    for track in tracks:
        x1, y1, x2, y2 = map(int, track.bbox)
        cv2.rectangle(plane, (x1, y1), (x2, y2), (30, 220, 100), 3)
        cv2.putText(plane, f"track {track.id}", (x1, max(26, y1)), 0, 1, (255, 255, 255), 2)
    label = f"sample {frame_idx}  detections {detection_count}"
    if injected:
        label += "  injected dropout"
    cv2.putText(plane, label, (10, 40), 0, 0.72, (255, 255, 255), 2)
    return plane


def pad_for_video_writer(image):
    """Pad odd dimensions for the encoder without altering existing pixels."""
    height, width = image.shape[:2]
    if height % 2 or width % 2:
        return cv2.copyMakeBorder(image, 0, height % 2, 0, width % 2, cv2.BORDER_REPLICATE)
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arg = parser.parse_args()
    arg.output.mkdir(parents=True, exist_ok=True)
    cfg = yaml.safe_load(Path("config/config.local.yaml").read_text())
    inputs = {}
    for cam in cfg["cameras"]:
        source = Path(cam["source"].replace(chr(92), "/"))
        inputs[source.stem.casefold()] = (source.name, cam)
    media = sorted(p for p in arg.assets.rglob("*") if p.is_file()
                   and p.suffix.lower() in {".mp4", ".mov", ".mkv", ".avi", ".m4v"})
    inventory, selected = [], {}
    for path in media:
        name = path.relative_to(arg.assets).as_posix()
        digest = hashlib.file_digest(path.open("rb"), "sha256").hexdigest()
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                                "-select_streams", "v:0", "-of", "json", str(path)],
                               capture_output=True, text=True, timeout=20)
        row = {"reference": name, "sha256": digest, "bytes": path.stat().st_size}
        if probe.returncode != 0:
            row.update(status="FAIL", note="ffprobe rejected file", diagnostic=probe.stderr.strip().splitlines()[-1])
        else:
            result = json.loads(probe.stdout)
            streams = result.get("streams", [])
            if not streams:
                row.update(status="FAIL", note="No video stream")
            else:
                stream = streams[0]
                cap = cv2.VideoCapture(str(path))
                ok, image = cap.read()
                cap.release()
                row.update(status="PASS" if ok else "FAIL", note="First frame decoded" if ok else "First frame unreadable",
                           duration=stream.get("duration") or result.get("format", {}).get("duration"),
                           width=stream["width"], height=stream["height"], fps=stream["avg_frame_rate"],
                           frame_count_metadata=stream.get("nb_frames"), codec=stream["codec_name"])
        inventory.append(row)
        for stem, (_, cam) in inputs.items():
            if row["status"] == "PASS" and stem in path.stem.casefold() and "annotated" in path.stem.casefold():
                if stem not in selected or path.stat().st_size > selected[stem][0].stat().st_size:
                    selected[stem] = path, cam, row
    for original, cam in inputs.values():
        matches = [p for p in media if p.name.casefold() == original.casefold()]
        inventory.append({"reference": original, "camera": cam["camera_id"],
                          "status": "PASS" if matches else "BLOCKED",
                          "note": "Configured raw media found" if matches else "Configured raw media absent"})
    (arg.output / "video-inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")
    d = cfg["detector"]
    model = YoloDetector(d["model_path"], d.get("input_size", 640), d.get("score_thr", .25),
                         d.get("nms_thr", .45), device="cpu")
    results = []
    for number, (path, cam, source_info) in enumerate(selected.values(), 1):
        cap = cv2.VideoCapture(str(path))
        fps = float(Fraction(source_info["fps"]))
        seek_sec = min(10, float(source_info["duration"]) / 4)
        start_idx = round(fps * seek_sec)
        step = max(1, int(fps / 6))
        tracker = ByteTracker(**{k: v for k, v in cfg["tracker"].items()
                                if k in {"high_thr","low_thr","match_iou","low_match_iou","max_age_s","min_hits"}})
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_idx)
        writer = None
        rows = []
        ever_ids = set()
        interrupted_id = set()
        try:
            for offset in range(24 * step):
                ok, image = cap.read()
                if not ok:
                    break
                if offset % step:
                    continue
                frame_idx = start_idx + offset
                sample = len(rows)
                detections = model.detect(image)
                assert detections.ndim == 2 and detections.shape[1] == 6 and np.isfinite(detections).all()
                assert np.all(detections[:, 2:4] > detections[:, :2])
                times = frame_idx / fps
                # Deliberate empty observation in the middle of a real footage probe.
                injected = sample in {10, 11}
                if injected and sample == 10:
                    interrupted_id.update(ever_ids)
                tracks, removed = tracker.update(np.empty((0, 6)) if injected else detections, times)
                active = [track.id for track in tracks]
                assert len(set(active)) == len(active)
                ever_ids.update(active)
                rows.append({"frame": frame_idx, "seconds": times, "detections": len(detections),
                             "confidence": [round(float(x), 4) for x in detections[:, 4]],
                             "boxes": detections[:, :4].astype(int).tolist(),
                             "confirmed_track_ids": active, "removed": [t.id for t in removed],
                             "injected_missing_observations": injected})
                plane = annotate_frame(image, tracks, frame_idx, len(detections), injected)
                plane = pad_for_video_writer(plane)
                if writer is None:
                    video = arg.output / f"tracking-probe-{number}.mp4"
                    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), fps / step,
                                             (plane.shape[1], plane.shape[0]))
                    assert writer.isOpened()
                writer.write(plane)
                if sample in {0, 12, 23}:
                    assert cv2.imwrite(str(arg.output / f"probe-{number}-sample-{sample}.jpg"), plane)
        finally:
            cap.release()
            if writer:
                writer.release()
        verify = cv2.VideoCapture(str(video))
        assert verify.read()[0]
        verify.release()
        assert len(rows) == 24
        after = {tid for row in rows[12:] for tid in row["confirmed_track_ids"]}
        results.append({"source": source_info["reference"], "source_sha256": source_info["sha256"],
                        "camera": cam["camera_id"], "geometry": cam["geometry_file"],
                        "sample_count": len(rows), "total_detections": sum(row["detections"] for row in rows),
                        "peak_detections": max(row["detections"] for row in rows),
                        "tracks": sorted(ever_ids), "reobserved_after_injection": sorted(interrupted_id & after),
                        "video": video.name, "frames": rows})
    (arg.output / "tracking-probes.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps([{k: v for k, v in r.items() if k != "frames"} for r in results], indent=2))


if __name__ == "__main__":
    main()
