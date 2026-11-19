"""Demo recorder test with stubbed pipelines (no models needed)."""
import os
import tempfile
from types import SimpleNamespace as NS

import cv2
import numpy as np

from s1.demo_video import DemoRecorder
from s1.flow import HandoffCoordinator


def _rt(cam, w, h):
    frame = np.full((h, w, 3), 90, np.uint8)
    cv2.rectangle(frame, (100, 200), (300, 400), (30, 60, 200), -1)
    voter = NS(consensus=lambda: NS(valid=True, text="TS07KB0955"))
    veh = NS(voter=voter, color=lambda: "red", emitted_plate="TS07KB0955")
    track = NS(id=4, bbox=(100, 200, 300, 400))
    occ = NS(track_id=4, vehicle=veh, baseline=False, parked_at=12.0)
    pipe = NS(
        camera_transform=np.array([[1., 0., 0.], [0., 1., 0.]], np.float32),
        geo=NS(tripwire=np.array([[0, 100], [w, 100]], np.float32),
               slots=[{"slot_id": "B-08", "polygon": np.array([[90, 190], [310, 190], [310, 420], [90, 420]], np.float32)},
                      {"slot_id": "B-09", "polygon": np.array([[320, 190], [500, 190], [500, 420], [320, 420]], np.float32)}]),
        slots=NS(occupants={"B-08": occ}, stream_start_ts=0.0),
        last_tracks=[track], registry=NS(by_track={4: veh}),
        _plate_ready=lambda v: True, display_ts=20.0,
    )
    return NS(latest_raw=frame, pipeline=pipe, processed_frames=7, skipped_idle=31,
              gate=NS(last_reason="motion"))


def test_two_cameras_tiled_into_one_playable_video():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "demo.mp4")
    rec = DemoRecorder(path, ["CAM-01", "CAM-02"], fps=10, tile_height=360)
    rts = {"CAM-01": _rt("CAM-01", 640, 480), "CAM-02": _rt("CAM-02", 480, 640)}
    h = HandoffCoordinator()
    h.transfers.append({"camera_id": "CAM-02", "local_track_id": 4, "global_id": "GID-00001",
                        "raw_event": "ENTRY", "event_ts": 19.0, "reason": "TRANSFER_IN from ['CAM-01']"})
    class R:
        def gid(self, cam, lid): return "GID-00001"
    rec.add_event("CAM-01", "PARK_START", "plate=TS07KB0955 slot=B-08", 12.0)
    rec.add_event("CAM-02", "TRANSFER_IN", "GID-00001 from CAM-01", 19.0)
    for i in range(12):
        rec.tick(rts, {"reid": R(), "handoff": h}, now_mono=i * 0.2)
    out = rec.close()
    assert out == path and os.path.getsize(path) > 1000
    assert rec.errors == 0
    import shutil, subprocess
    if shutil.which("ffprobe"):
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                                "-show_entries", "stream=codec_name,width,height,nb_read_frames",
                                "-of", "default=nw=1", path], capture_output=True, text=True).stdout
        info = dict(l.split("=") for l in probe.split())
        assert info["codec_name"] == "h264" and int(info["nb_read_frames"]) >= 10
        assert int(info["width"]) > int(info["height"]) * 0.8   # tiles side by side + event panel
    else:
        cap = cv2.VideoCapture(path)
        assert cap.read()[0]


def test_render_error_never_raises():
    rec = DemoRecorder(os.path.join(tempfile.mkdtemp(), "x.mp4"), ["CAM-01"])
    bad = NS(latest_raw=np.zeros((10, 10, 3), np.uint8), pipeline=None)
    rec.tick({"CAM-01": bad}, {}, 1.0)                       # must not raise
    assert rec.errors == 1
