import cv2
import json
import shutil
import argparse
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--video", required=True)
parser.add_argument("--geometry", required=True)
args = parser.parse_args()

video_path = Path(args.video)
geometry_path = Path(args.geometry)

with geometry_path.open("r", encoding="utf-8") as f:
    geometry = json.load(f)

cap = cv2.VideoCapture(str(video_path))
ok, frame = cap.read()
cap.release()

if not ok:
    raise RuntimeError("Could not read video.")

frame_h, frame_w = frame.shape[:2]
expected = geometry.get("frame_size")

if not expected or len(expected) != 2:
    raise ValueError("Geometry frame_size is missing or invalid.")

geometry_w, geometry_h = map(int, expected)

scale = min(1100 / frame_w, 850 / frame_h, 1.0)
display = cv2.resize(
    frame, None, fx=scale, fy=scale,
    interpolation=cv2.INTER_AREA
)

points = []
window = "ENTRY TRIPWIRE CALIBRATION"

def on_mouse(event, x, y, flags, userdata):
    if event == cv2.EVENT_LBUTTONDOWN and len(points) < 2:
        video_x = x / scale
        video_y = y / scale

        geometry_x = video_x * geometry_w / frame_w
        geometry_y = video_y * geometry_h / frame_h

        points.append((
            round(geometry_x),
            round(geometry_y)
        ))

cv2.namedWindow(window, cv2.WINDOW_NORMAL)
cv2.setMouseCallback(window, on_mouse)

print("Click TWO points across the actual vehicle entry path.")
print("Coordinates will be scaled to the geometry resolution.")
print("Press R to reset, ENTER to save, ESC to cancel.")

while True:
    view = display.copy()

    for gx, gy in points:
        vx = round(gx * frame_w / geometry_w * scale)
        vy = round(gy * frame_h / geometry_h * scale)
        cv2.circle(view, (vx, vy), 7, (0, 255, 255), -1)

    if len(points) == 2:
        p1 = (
            round(points[0][0] * frame_w / geometry_w * scale),
            round(points[0][1] * frame_h / geometry_h * scale)
        )
        p2 = (
            round(points[1][0] * frame_w / geometry_w * scale),
            round(points[1][1] * frame_h / geometry_h * scale)
        )
        cv2.line(view, p1, p2, (0, 255, 0), 3)

    cv2.imshow(window, view)
    key = cv2.waitKey(30) & 0xFF

    if key == 27:
        print("Cancelled. Geometry unchanged.")
        break

    if key in (ord("r"), ord("R")):
        points.clear()

    if key in (10, 13) and len(points) == 2:
        backup = geometry_path.with_suffix(
            geometry_path.suffix + ".before_tripwire.bak"
        )
        shutil.copy2(geometry_path, backup)

        geometry["tripwire"] = [
            [points[0][0], points[0][1]],
            [points[1][0], points[1][1]]
        ]

        temp_path = geometry_path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as f:
            json.dump(geometry, f, indent=2)

        temp_path.replace(geometry_path)

        print("TRIPWIRE UPDATED:", geometry["tripwire"])
        print("Backup:", backup)
        break

cv2.destroyAllWindows()
