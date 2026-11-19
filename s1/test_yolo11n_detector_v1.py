r"""Manual YOLO11 detector diagnostic for a supplied video file.

Example (PowerShell):
    python .\s1\test_yolo11n_detector_v1.py --video "C:\path\VN20261005_103538.mp4"

This is intentionally a command-line utility, not an import-time pytest test.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import cv2
from ultralytics import YOLO


def main():
    parser = argparse.ArgumentParser(description="Inspect YOLO vehicle detections at selected video timestamps")
    parser.add_argument("--video", required=True, help="Full path to the video to inspect")
    parser.add_argument("--model", default="yolo11n.pt", help="YOLO model path")
    parser.add_argument("--out", default="data/yolo11n_test", help="Directory for annotated frames")
    parser.add_argument("--seconds", nargs="+", type=float, default=[10, 14, 16, 18, 20],
                        help="Video timestamps in seconds to inspect")
    args = parser.parse_args()

    video = Path(args.video).expanduser()
    model_path = Path(args.model).expanduser()
    out_dir = Path(args.out)
    if not video.is_file():
        parser.error(f"video file not found: {video}")
    if not model_path.is_file():
        parser.error(f"YOLO model file not found: {model_path}")

    out_dir.mkdir(parents=True, exist_ok=True)
    model = YOLO(str(model_path))
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        parser.error(f"OpenCV could not open video: {video}")

    try:
        for sec in args.seconds:
            cap.set(cv2.CAP_PROP_POS_MSEC, sec * 1000)
            ok, frame = cap.read()
            if not ok or frame is None:
                print(f"{sec:g}s: frame unavailable")
                continue

            started = time.perf_counter()
            result = model.predict(
                source=frame, imgsz=640, conf=0.15, iou=0.45,
                classes=[2, 3, 5, 7], verbose=False, device="cpu",
            )[0]
            elapsed_ms = (time.perf_counter() - started) * 1000
            annotated = frame.copy()
            boxes = result.boxes
            print(f"\n=== {sec:g}s | {len(boxes)} detections | {elapsed_ms:.1f} ms ===")
            for box in boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                score = float(box.conf[0])
                class_id = int(box.cls[0])
                label = f"{model.names[class_id]} {score:.2f}"
                print(f"  {label}: [{x1}, {y1}, {x2}, {y2}]")
                cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), 5)
                cv2.putText(annotated, label, (x1, max(40, y1 - 10)),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 0), 3)

            output_path = out_dir / f"frame_{sec:g}s.jpg"
            if not cv2.imwrite(str(output_path), annotated):
                raise OSError(f"Could not save annotated frame: {output_path}")
            print("Saved:", output_path)
    finally:
        cap.release()

    print("\nOffline detector diagnostic complete. No Service 2 events were sent.")


if __name__ == "__main__":
    main()
