"""Tests for the ROI motion mask, ReID fallback, report fixes and S2 sender."""
import json
import os
import sys
from types import SimpleNamespace

import cv2
import numpy as np

from s1.geometry import Geometry
from s1.motion_roi import build_motion_mask, masked_diff_score
from s1.multicam import _activity_score
from s1.reid import HistogramEmbedder
from s1.global_identity import GlobalIdentityRegistry
from s1 import report as rpt


def _geom():
    return Geometry({
        "frame_size": [640, 360], "tripwire": [[0, 280], [640, 280]], "inside_point": [320, 130],
        "slots": [{"slot_id": "S1", "polygon": [[100, 80], [260, 80], [260, 195], [100, 195]]}],
    })


def test_roi_excludes_top_strip_and_scales_with_resolution():
    mask, info = build_motion_mask(_geom(), (1280, 720))
    assert info["enabled"] and mask.shape == (90, 160)
    assert mask[2:8, :].sum() == 0          # sky/tree strip at the top is outside the ROI
    assert mask[45, 60] == 255              # inside the slot area


def test_custom_motion_roi_overrides_hull():
    g = _geom()
    g.motion_roi = [(0, 0), (320, 0), (320, 360), (0, 360)]
    mask, info = build_motion_mask(g, (640, 360))
    assert info["source"] == "geometry.motion_roi"
    assert mask[45, 20] == 255 and mask[45, 140] == 0


def test_small_blobs_ignored_large_blob_counts():
    prev = np.full((90, 160), 100, np.uint8)
    leaf = prev.copy()
    for x in (10, 40, 70, 100):
        leaf[50:52, x:x + 2] = 220          # 4 px leaf-sized flickers
    car = prev.copy()
    car[40:70, 50:100] = 220                # big moving object
    assert masked_diff_score(prev, leaf, None, min_blob_px=6) == 0.0
    assert masked_diff_score(prev, car, None, min_blob_px=6) > 0.05


def test_activity_score_backward_compatible_signature():
    img = np.zeros((360, 640, 3), np.uint8)
    gray, score = _activity_score(None, img)
    assert gray.shape == (90, 160) and score == 1.0


def test_histogram_embedder_is_512d_and_separates_colours():
    emb = HistogramEmbedder()
    red = np.zeros((80, 120, 3), np.uint8); red[:] = (0, 0, 220)
    red2 = red.copy(); red2[10:20, 10:20] = (0, 0, 200)
    blue = np.zeros((80, 120, 3), np.uint8); blue[:] = (220, 0, 0)
    a, b, c = emb.encode(red), emb.encode(red2), emb.encode(blue)
    assert a.shape == (512,) and abs(float(np.linalg.norm(a)) - 1) < 1e-5
    assert float(a @ b) > 0.93 > float(a @ c)
    reg = GlobalIdentityRegistry(threshold=0.93)
    g1 = reg.assign("camA", 1, a, 0.0)["global_id"]
    g2 = reg.assign("camB", 7, b, 30.0)["global_id"]
    g3 = reg.assign("camB", 8, c, 31.0)["global_id"]
    assert g1 == g2 != g3


class _Veh:
    uid = "u1"
    voter = None
    emitted_plate = None
    plate_final = False

    def vehicle_type(self):
        return "car"

    def color(self):
        return "green"


def test_slot_record_calls_vehicle_type_and_color():
    occ = SimpleNamespace(vehicle=_Veh(), track_id=3, parked_at=15.0, baseline=False)
    slots = SimpleNamespace(
        slot_states=lambda: [{"slot_id": "S1", "state": "OCCUPIED"}, {"slot_id": "S2", "state": "FREE"}],
        occupants={"S1": occ}, stream_start_ts=10.0)
    rt = SimpleNamespace(cfg={"camera_id": "cam"}, pipeline=SimpleNamespace(slots=slots))
    recs = rpt.slot_records_for(rt)
    assert recs[0]["vehicle_type"] == "car" and recs[0]["colour"] == "green"   # not a bound-method repr
    assert recs[0]["parked_at_label"] == "Video +00:00:05"
    assert recs[1]["plate"] is None


def test_vehicles_kept_when_global_id_missing():
    obs = {"camera_id": "cam", "local_track_id": 4, "global_id": None, "track_confirmed": True,
           "first_seen_video_seconds": 1.0, "last_seen_video_seconds": 9.0, "vehicle_uid": "u1",
           "vehicle_type": "car", "colour": "red", "number_plate": "AP37CL3789",
           "plate_status": "CONFIRMED", "ocr_confidence": 0.9, "ocr_consensus_reads": 3}
    rt = SimpleNamespace(track_records={"cam:4": obs})
    slot_table = {"cam": [{"slot_id": "S1", "state": "OCCUPIED", "vehicle_uid": "u1", "camera_id": "cam",
                           "baseline": False, "parked_at_label": "Video +00:00:05"}]}
    vehicles = rpt.build_vehicles({"cam": rt}, slot_table)
    assert len(vehicles) == 1
    v = vehicles[0]
    assert v["id_scope"] == "local" and v["slot_id"] == "S1" and v["state"] == "PARKED"
    assert v["number_plate"] == "AP37CL3789" and v["plate_status"] == "CONFIRMED"


def test_send_report_dry_run_orders_entry_before_exit(tmp_path, capsys):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
    import send_report_to_s2 as s
    ev = [{"event_type": "EXIT", "captured_at": "2025-01-01T10:00:00", "parking": {"slot_id": "A"}},
          {"event_type": "ENTRY", "captured_at": "2025-01-01T10:00:00", "parking": {"slot_id": "A"}}]
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"service2_events": ev}))
    assert [e["event_type"] for e in s.load_events(str(p))] == ["ENTRY", "EXIT"]
    assert s.main([str(p)]) == 0
    assert "would send" in capsys.readouterr().out
