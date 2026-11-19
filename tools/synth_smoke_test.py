"""End-to-end smoke test WITHOUT GPU, real videos, YOLO weights or OSNet.

It renders two small synthetic parking cameras, runs the REAL pipeline
(motion gate + ROI, shared batching, ByteTrack, ReID fallback, tripwire, slot
logic, S2 mapper, final report) with a trivial colour-blob "car detector" in
place of YOLO, and checks the JSON that would go to the DB service.

    python tools/synth_smoke_test.py            # ~60-70 s (real-time replay)

It proves the plumbing and the lifecycle logic. It does NOT prove YOLO/plate/
OSNet accuracy on your real footage - that needs the Kaggle run.
"""
import json
import os
import sys
import time

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

W, H, FPS = 640, 360, 10
OUT = os.path.join(ROOT, "data", "synth")

GREEN = (0, 200, 0)
BLUE = (200, 40, 0)


def _bg():
    img = np.full((H, W, 3), 150, np.uint8)
    for x in range(0, W, 40):
        cv2.line(img, (x, 0), (x, H), (130, 130, 130), 1)
    for y in range(0, H, 40):
        cv2.line(img, (0, y), (W, y), (170, 170, 170), 1)
    rng = np.random.default_rng(3)
    for _ in range(250):
        cv2.circle(img, (int(rng.integers(0, W)), int(rng.integers(0, H))), 2,
                   (int(rng.integers(90, 200)),) * 3, -1)
    return img


def _car(img, cx, cy, y_scale, color):
    w, h = int(150 * y_scale), int(80 * y_scale)
    cv2.rectangle(img, (int(cx - w / 2), int(cy - h / 2)), (int(cx + w / 2), int(cy + h / 2)), color, -1)
    cv2.rectangle(img, (int(cx - w / 4), int(cy - h / 2)), (int(cx + w / 4), int(cy - h / 4)), (240, 240, 240), -1)


def _scale_at(y):          # nearer the camera (bottom) = bigger
    return 0.55 + 0.45 * (y / 400.0)


def _leaves(img, t):
    """Small flickering blobs + a bird in the top strip (outside the parking ROI)."""
    rng = np.random.default_rng(int(t * 10))
    for k in range(10):
        x = 20 + k * 55 + int(rng.integers(-6, 7))
        y = 12 + int(rng.integers(-5, 6))
        cv2.circle(img, (x, y), 3, (30, 90, 30), -1)
    bx = int((t * 60) % (W + 40)) - 20
    cv2.ellipse(img, (bx, 20), (7, 3), 0, 0, 360, (20, 20, 20), -1)


def render_cam(path, car_script, baseline_slot_center=None, duration=36):
    vw = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    bg = _bg()
    for i in range(duration * FPS):
        t = i / FPS
        f = bg.copy()
        _leaves(f, t)
        if baseline_slot_center:
            _car(f, *baseline_slot_center, 0.62, BLUE)
        pos = car_script(t)
        if pos is not None:
            cx, cy = pos
            _car(f, cx, cy, _scale_at(cy), GREEN)
        vw.write(f)
    vw.release()


def lerp(a, b, u):
    return a + (b - a) * min(1.0, max(0.0, u))


def script_cam_a(t):
    if t < 3 or t >= 28:
        return None
    if t < 9:                                   # drive in, up through the tripwire
        u = (t - 3) / 6
        return lerp(320, 180, u), lerp(420, 135, u)
    if t < 22:                                  # parked
        return 180, 135
    u = (t - 22) / 6                            # drive out, back down through the tripwire
    return lerp(180, 320, u), lerp(135, 420, u)


GEOM_A = {
    "frame_size": [W, H],
    "tripwire": [[0, 280], [W, 280]],
    "inside_point": [320, 130],
    "plate_zone": None,
    "slots": [
        {"slot_id": "A-01", "level": "L1", "polygon": [[100, 80], [260, 80], [260, 195], [100, 195]]},
        {"slot_id": "A-02", "level": "L1", "polygon": [[380, 80], [540, 80], [540, 195], [380, 195]]},
    ],
}


class ColourBlobDetector:
    """Stand-in for YOLO: any large saturated blob is a 'car' (class 2)."""
    device = "cpu"

    def _one(self, img):
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        m = ((hsv[..., 1] > 120) & (hsv[..., 2] > 100)).astype(np.uint8)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
        n, _, st, _ = cv2.connectedComponentsWithStats(m)
        out = [[x, y, x + w, y + h, 0.9, 2.0] for x, y, w, h, a in st[1:] if a > 2500]
        return np.array(out, np.float32).reshape(-1, 6)

    def detect(self, img):
        return self._one(img)

    def detect_batch(self, imgs):
        return [self._one(i) for i in imgs]


def script_cam_b(t):
    """Same car shows up on camera B after leaving camera A's slot, parks in B-01."""
    if t < 24 or t >= 60:
        return None
    if t < 30:
        u = (t - 24) / 6
        return lerp(320, 180, u), lerp(420, 135, u)
    return 180, 135


GEOM_B = json.loads(json.dumps(GEOM_A))
for _s, _n in zip(GEOM_B["slots"], ("B-01", "B-02")):
    _s["slot_id"] = _n


def run_transfer():
    """Car parks on cam A, leaves its slot, drives out of cam A and parks on cam B."""
    os.makedirs(OUT, exist_ok=True)
    va, vb = os.path.join(OUT, "t_cam_a.mp4"), os.path.join(OUT, "t_cam_b.mp4")
    ga, gb = os.path.join(OUT, "t_geom_a.json"), os.path.join(OUT, "t_geom_b.json")
    print("rendering two synthetic cameras ...")
    render_cam(va, script_cam_a, duration=40)
    render_cam(vb, script_cam_b, duration=40)
    json.dump(GEOM_A, open(ga, "w")); json.dump(GEOM_B, open(gb, "w"))
    from s1.config import load_config
    cfg = load_config(os.path.join(ROOT, "config", "config.kaggle_flow.yaml"))
    cfg["cameras"] = [
        {"camera_id": "synth-cam-a", "source": va, "geometry_file": ga},
        {"camera_id": "synth-cam-b", "source": vb, "geometry_file": gb},
    ]
    cfg["detector"]["device"] = "cpu"
    cfg["plate"]["enabled"] = False
    cfg["replay"] = "realtime"
    cfg["replay_start_time"] = 1_760_000_000.0
    cfg["demo_video"]["enabled"] = False
    cfg["report"] = {"path": os.path.join(OUT, "report_transfer.json")}
    cfg["camera_motion"]["enabled"] = False
    cfg["publisher"]["outbox_db"] = os.path.join(OUT, "outbox_t.db")
    from s1.multicam import run_multi_camera
    run_multi_camera(cfg, ColourBlobDetector(), show=False, no_publisher=True)
    rep = json.load(open(cfg["report"]["path"]))
    print("\nS2 events :", [(e["event_type"], e["camera_id"], e["parking"]["slot_id"]) for e in rep["service2_events"]])
    print("transfers :", [(t["raw_event"], t["camera_id"], t["global_id"], t["reason"]) for t in rep["camera_transfers"]])
    print("identities:", rep["global_reid"]["identities"])
    print("vehicles  :", [(v["vehicle_id"], v["cameras"], v["slot_id"], v["state"]) for v in rep["vehicles"]])
    sys.exit(0)


def main():
    if "--transfer" in sys.argv:
        return run_transfer()
    os.makedirs(OUT, exist_ok=True)
    vid = os.path.join(OUT, "cam_a.mp4")
    geo = os.path.join(OUT, "geometry_a.json")
    print("rendering synthetic camera ...")
    render_cam(vid, script_cam_a, baseline_slot_center=(460, 135))
    json.dump(GEOM_A, open(geo, "w"), indent=2)

    from s1.config import load_config
    cfg = load_config(os.path.join(ROOT, "config", "config.kaggle_flow.yaml"))
    cfg["cameras"] = [{"camera_id": "synth-cam-a", "source": vid, "geometry_file": geo}]
    cfg["detector"]["device"] = "cpu"
    cfg["plate"]["enabled"] = False
    cfg["replay"] = "realtime"
    cfg["replay_start_time"] = 1_760_000_000.0
    cfg["demo_video"]["enabled"] = False
    cfg["report"] = {"path": os.path.join(OUT, "report.json")}
    cfg["camera_motion"]["enabled"] = False
    cfg["publisher"]["outbox_db"] = os.path.join(OUT, "outbox.db")

    from s1.multicam import run_multi_camera, _activity_score
    from s1.motion_roi import build_motion_mask
    from s1.geometry import Geometry

    # ---- ROI check: leaves/bird alone must not look like motion, a car must ----
    g = Geometry(GEOM_A)
    mask, info = build_motion_mask(g, (W, H))
    cap = cv2.VideoCapture(vid)
    frames = []
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        frames.append(fr)
    cap.release()
    def score(a, b, roi, blob):
        _, prev = _activity_score(None, a, roi, blob)
        prev_gray = cv2.cvtColor(cv2.resize(a, (160, 90), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        return _activity_score(prev_gray, b, roi, blob)[1]
    leaf_old = np.mean([score(frames[i], frames[i + 1], None, 0) for i in range(5, 25)])
    leaf_new = np.mean([score(frames[i], frames[i + 1], mask, 6) for i in range(5, 25)])
    car_new = np.mean([score(frames[i], frames[i + 1], mask, 6) for i in range(45, 85)])
    print(f"motion score  leaves/bird only: old(whole-frame)={leaf_old:.3f} -> new(ROI+blob)={leaf_new:.3f} | "
          f"car driving in: new={car_new:.3f} | ROI covers {info['area_frac']*100:.0f}% of frame")
    assert leaf_new < 0.025 <= car_new, "ROI motion must ignore leaves/birds and still see the car"

    t0 = time.time()
    run_multi_camera(cfg, ColourBlobDetector(), show=False, no_publisher=True)
    print(f"pipeline wall time: {time.time() - t0:.1f}s")

    rep = json.load(open(cfg["report"]["path"]))
    s2 = rep["service2_events"]
    kinds = [e["event_type"] for e in s2]
    slot_ids = {e.get("parking", {}).get("slot_id") for e in s2}
    print("service2_events:", kinds, "slots:", slot_ids)
    checks = {
        "ENTRY sent for the car that parked": "ENTRY" in kinds,
        "EXIT sent when it left": "EXIT" in kinds,
        "ENTRY is slot A-01": "A-01" in slot_ids,
        "pre-parked (baseline) car did NOT create ENTRY/EXIT": not any(
            (e.get("parking") or {}).get("slot_id") == "A-02" for e in s2),
        "baseline car is listed as occupying A-02": any(
            r["slot_id"] == "A-02" and r["state"] == "OCCUPIED" and r["baseline"]
            for r in rep["cameras"]["synth-cam-a"]["slots"]),
        "ReID fallback labelled": rep["pipeline_health"]["reid_backend"] == "histogram_fallback",
    }
    ok = True
    for k, v in checks.items():
        print(("PASS  " if v else "FAIL  ") + k)
        ok &= bool(v)
    print("\nreport:", cfg["report"]["path"])
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
