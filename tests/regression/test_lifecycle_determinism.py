import threading
import time

import numpy as np

from s1.determinism import is_file_source, resolve_deterministic
from s1.lifecycle_timeline import build_timeline
from s1.plates import PlateWorker


def test_file_vs_live_sources():
    assert is_file_source("/data/a.mp4")
    assert is_file_source("C:\\videos\\a.MOV")
    assert not is_file_source("rtsp://cam/stream")
    assert not is_file_source("http://cam/mjpg")
    assert not is_file_source(0)
    assert not is_file_source("0")


def test_auto_mode_only_for_all_files():
    files = [{"camera_id": "A", "source": "a.mp4"}, {"camera_id": "B", "source": "b.mp4"}]
    mixed = files + [{"camera_id": "C", "source": "rtsp://x/y"}]
    assert resolve_deterministic({}, files).enabled
    d = resolve_deterministic({}, mixed)
    assert not d.enabled and "C" in d.reason
    assert resolve_deterministic({"deterministic": True}, mixed).enabled
    assert not resolve_deterministic({"deterministic": "off"}, files).enabled


def test_fixed_start_epoch_and_timestamp_source():
    d = resolve_deterministic({"determinism": {"fixed_start_epoch": 123.0, "timestamp_source": "bogus"}},
                              [{"camera_id": "A", "source": "a.mp4"}])
    assert d.run_start_epoch == 123.0 and d.timestamp_source == "laptop"


class _SlowReader:
    def __init__(self):
        self.seen = []

    def read(self, crop):
        time.sleep(0.01)
        self.seen.append(crop)
        return []


def test_deterministic_plate_worker_never_drops_and_drains():
    w = PlateWorker(_SlowReader(), deterministic=True)
    try:
        for i in range(10):                    # live mode would drop after 2 queued
            assert w.submit(f"k{i}", np.zeros((8, 8, 3), np.uint8))
        assert w.wait_idle(timeout=10.0)
        assert len(w.poll()) == 10
    finally:
        w.stop(wait=True, timeout=5.0)


def test_live_plate_worker_still_bounded():
    gate = threading.Event()

    class Blocked:
        def read(self, crop):
            gate.wait(5)
            return []

    w = PlateWorker(Blocked(), deterministic=False)
    try:
        accepted = [w.submit(f"k{i}", np.zeros((8, 8, 3), np.uint8)) for i in range(10)]
        assert not all(accepted)               # unchanged live behaviour: bounded queue
    finally:
        gate.set()
        w.stop(wait=True, timeout=5.0)


def _ev(t, when, slot=None, plate=None, inferred=False):
    return {"event_type": t, "camera_id": "C1", "vehicle_uid": "u1", "event_time": when,
            "bay": {"id": slot} if slot else None, "inferred": inferred,
            "plate": {"text": plate}, "plate_status": "CONFIRMED" if plate else "UNCONFIRMED",
            "vehicle_details": {"color": "red", "type": "car"}}


def test_timeline_full_cycle_and_duration():
    rows = build_timeline([
        _ev("EXIT", "2026-11-14T10:10:00.000+05:30", inferred=True),
        _ev("ENTRY", "2026-11-14T10:00:00.000+05:30"),
        _ev("PARK_END", "2026-11-14T10:09:00.000+05:30", slot="B-01", plate="TS07AB1234"),
        _ev("PARK_START", "2026-11-14T10:01:00.000+05:30", slot="B-01"),
    ])
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "EXITED" and r["exit_inferred"] is True
    assert r["slot_id"] == "B-01"
    assert r["plate"] is None
    assert r["plate_status"] == "CONFIRMED"
    assert r["parked_duration_s"] == 480.0


def test_timeline_second_park_cycle_is_a_new_row():
    rows = build_timeline([
        _ev("PARK_START", "2026-11-14T10:00:00.000+05:30", slot="B-01"),
        _ev("PARK_END", "2026-11-14T10:05:00.000+05:30", slot="B-01"),
        _ev("PARK_START", "2026-11-14T10:20:00.000+05:30", slot="B-02"),
    ])
    assert [r["status"] for r in rows] == ["LEFT_SLOT", "PARKED"]
    assert [r["slot_id"] for r in rows] == ["B-01", "B-02"]


def test_timeline_ignores_unknown_and_missing_stages():
    rows = build_timeline([{"event_type": "VEHICLE_UPDATE", "camera_id": "C1", "vehicle_uid": "u1"},
                           _ev("PARK_START", "2026-11-14T10:00:00.000+05:30", slot="B-01")])
    assert len(rows) == 1 and rows[0]["park_end_time_ist"] is None and rows[0]["exit_time_ist"] is None
