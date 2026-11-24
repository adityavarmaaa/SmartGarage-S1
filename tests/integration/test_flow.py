"""Tests for motion gate, async ReID worker and cross-camera hand-off (no GPU/models needed)."""
import time
import numpy as np

from vehicle_intelligence.flow import (AsyncGlobalReID, GpuArbiter, HandoffCoordinator, MotionGate,
                     resolve_aux_device)
from vehicle_intelligence.global_identity import GlobalIdentityRegistry


# ---------------- MotionGate ----------------
def test_idle_camera_is_skipped_and_wakes_on_motion():
    g = MotionGate(on_threshold=0.025, hold_s=3.0, idle_empty_heartbeat_s=3.0)
    assert g.allow(100.0, 99.9, False)[0] is False          # no motion, no tracks
    g.observe(0.2, 100.0)
    ok, why = g.allow(100.1, 100.0, False)
    assert ok and why == "motion"
    assert g.allow(102.9, 102.0, False)[0] is True           # still inside hold
    assert g.allow(103.5, 103.4, False)[0] is False          # hold expired -> idle again


def test_heartbeats_keep_tracks_alive_and_catch_missed_motion():
    g = MotionGate(idle_track_heartbeat_s=0.5, idle_empty_heartbeat_s=3.0)
    assert g.allow(10.0, 9.9, True)[0] is False
    ok, why = g.allow(10.5, 10.0, True)
    assert ok and why == "heartbeat_tracks"
    assert g.allow(12.0, 10.0, False)[0] is False
    ok, why = g.allow(13.1, 10.0, False)
    assert ok and why == "heartbeat_empty"


def test_hysteresis_noise_between_thresholds_does_not_extend_hold():
    g = MotionGate(on_threshold=0.04, off_ratio=0.5, hold_s=1.0)
    g.observe(0.03, 50.0)                                    # between off(0.02) and on(0.04)
    assert g.allow(50.1, 49.0, False)[0] is False


def test_heartbeat_never_exceeds_half_tracker_max_age():
    g = MotionGate.from_cfg({"motion_gate": {"idle_track_heartbeat_s": 5.0}}, tracker_max_age_s=2.0)
    assert g.track_hb <= 1.0


def test_disabled_gate_passes_everything():
    g = MotionGate(enabled=False)
    assert g.allow(1.0, 0.99, False)[0] is True


def test_aux_device_resolution():
    assert resolve_aux_device("cpu") == ("cpu", False)
    assert resolve_aux_device("cuda:0", "cuda:1") == ("cuda:1", False)
    assert resolve_aux_device("cuda:0", "cuda:0") == ("cuda:0", True)


# ---------------- HandoffCoordinator ----------------
def _hold(h, cam, local, etype, ts, now=None):
    h.hold(cam, local, etype, ts, ts if now is None else now, {"vehicle": None})


def test_entry_on_cam2_after_seen_on_cam1_is_transfer_not_entry():
    h = HandoffCoordinator()
    h.note_sighting("CAM-01", 5, "GID-1", 100.0)
    h.note_sighting("CAM-01", 5, "GID-1", 110.0)
    h.note_sighting("CAM-02", 9, "GID-1", 112.0)
    _hold(h, "CAM-02", 9, "ENTRY", 111.5, now=112.0)
    (it, verdict, reason), = h.poll("CAM-02", 112.0)
    assert verdict == "SUPPRESS" and reason.startswith("TRANSFER_IN")
    assert h.transfers[0]["global_id"] == "GID-1"


def test_first_real_entry_emits_and_duplicate_view_is_suppressed():
    h = HandoffCoordinator()
    h.note_sighting("CAM-01", 1, "GID-7", 50.0)
    h.note_sighting("CAM-02", 4, "GID-7", 50.4)              # overlapping view of the same car
    _hold(h, "CAM-01", 1, "ENTRY", 50.0, now=50.5)
    (_, v1, r1), = h.poll("CAM-01", 50.5)
    # CAM-01 saw it first: the other camera's overlap sighting makes this a transfer only if
    # that sighting began BEFORE the crossing, which it did not -> real entry
    assert v1 == "EMIT" and r1 == "NEW_ENTRY"
    _hold(h, "CAM-02", 4, "ENTRY", 50.4, now=50.6)
    (_, v2, r2), = h.poll("CAM-02", 50.6)
    assert v2 == "SUPPRESS"                                  # ALREADY_INSIDE


def test_entry_waits_for_gid_then_fails_open():
    h = HandoffCoordinator(entry_wait_s=4.0)
    _hold(h, "CAM-01", 3, "ENTRY", 10.0, now=10.0)
    assert h.poll("CAM-01", 12.0) == []                      # still waiting for ReID
    (_, v, r), = h.poll("CAM-01", 14.1)
    assert v == "EMIT" and r == "GID_UNKNOWN_FAIL_OPEN"


def test_entry_released_as_soon_as_gid_arrives():
    h = HandoffCoordinator()
    _hold(h, "CAM-01", 3, "ENTRY", 10.0, now=10.0)
    assert h.poll("CAM-01", 10.5) == []
    h.note_sighting("CAM-01", 3, "GID-2", 10.6)
    (_, v, r), = h.poll("CAM-01", 10.7)
    assert v == "EMIT" and r == "NEW_ENTRY" and h.inside["GID-2"] is True


def test_exit_followed_by_sighting_on_other_camera_is_transfer_out():
    h = HandoffCoordinator(exit_wait_s=8.0)
    h.note_sighting("CAM-01", 2, "GID-3", 200.0)
    _hold(h, "CAM-01", 2, "EXIT", 205.0, now=205.0)
    assert h.poll("CAM-01", 206.0) == []                     # holding
    h.note_sighting("CAM-02", 8, "GID-3", 207.0)
    (_, v, r), = h.poll("CAM-01", 207.5)
    assert v == "SUPPRESS" and r.startswith("TRANSFER_OUT")


def test_real_exit_emitted_after_wait_with_original_timestamp():
    h = HandoffCoordinator(exit_wait_s=8.0)
    h.note_sighting("CAM-01", 2, "GID-4", 300.0)
    h.inside["GID-4"] = True
    _hold(h, "CAM-01", 2, "EXIT", 305.0, now=305.0)
    assert h.poll("CAM-01", 310.0) == []
    (it, v, r), = h.poll("CAM-01", 313.1)
    assert v == "EMIT" and r == "REAL_EXIT" and it["event_ts"] == 305.0
    assert h.inside["GID-4"] is False


def test_final_flush_never_drops_held_events():
    h = HandoffCoordinator()
    _hold(h, "CAM-01", 1, "ENTRY", 1.0, now=1.0)
    (_, v, _), = h.poll("CAM-01", 1.1, final=True)
    assert v == "EMIT"


def test_reentry_after_real_exit_is_a_new_entry():
    h = HandoffCoordinator(exit_wait_s=1.0)
    h.note_sighting("CAM-01", 1, "GID-5", 0.0)
    _hold(h, "CAM-01", 1, "ENTRY", 0.0, now=0.0); h.poll("CAM-01", 0.1)
    _hold(h, "CAM-01", 1, "EXIT", 100.0, now=100.0); h.poll("CAM-01", 101.5)
    h.note_sighting("CAM-01", 6, "GID-5", 500.0)
    _hold(h, "CAM-01", 6, "ENTRY", 500.0, now=500.0)
    (_, v, r), = h.poll("CAM-01", 500.1)
    assert v == "EMIT" and r == "NEW_ENTRY"


# ---------------- AsyncGlobalReID ----------------
class _Track:
    def __init__(self, i, bbox):
        self.id, self.bbox, self.confirmed = i, bbox, True


class _FakeEmbedder:
    """Encodes a crop by its mean colour so identical cars match across cameras."""
    def __init__(self, delay=0.0):
        self.delay = delay

    def encode(self, crop):
        time.sleep(self.delay)
        rng = np.random.default_rng(int(crop.mean()) % 997)
        v = rng.normal(size=512).astype(np.float32)
        return v / np.linalg.norm(v)


def _frame(colour):
    f = np.zeros((200, 300, 3), np.uint8)
    f[:] = colour
    return f


def test_async_reid_never_blocks_and_matches_same_car_across_cameras():
    h = HandoffCoordinator()
    r = AsyncGlobalReID(_FakeEmbedder(delay=0.05), GlobalIdentityRegistry(0.75), handoff=h)
    try:
        t0 = time.perf_counter()
        r.update("CAM-01", _frame(80), [_Track(1, (10, 10, 120, 120))], 10.0)
        assert time.perf_counter() - t0 < 0.02              # submit is non-blocking
        r.update("CAM-02", _frame(80), [_Track(7, (10, 10, 120, 120))], 12.0)
        r.update("CAM-02", _frame(200), [_Track(8, (10, 10, 120, 120))], 12.1)
        deadline = time.time() + 3
        while r.embedded < 3 and time.time() < deadline:
            time.sleep(0.01)
        assert r.gid("CAM-01", 1) == r.gid("CAM-02", 7) is not None
        assert r.gid("CAM-02", 8) not in (None, r.gid("CAM-01", 1))
        assert h.local_gid[("CAM-02", 7)] == r.gid("CAM-01", 1)
    finally:
        r.stop()


def test_async_reid_drops_instead_of_blocking_when_queue_full():
    r = AsyncGlobalReID(_FakeEmbedder(delay=0.5), GlobalIdentityRegistry(0.75), queue_size=2)
    try:
        for i in range(20):
            r.update("CAM-01", _frame(i * 10), [_Track(i, (0, 0, 100, 100))], float(i))
        assert r.dropped > 0
    finally:
        r.stop()


def test_gpu_arbiter_blocks_background_while_yolo_runs():
    a = GpuArbiter()
    a.yolo_enter()
    t0 = time.perf_counter()
    assert a.wait_free(0.05) is False
    a.yolo_exit()
    assert a.wait_free(0.05) is True


# ---------------- Pipeline integration (stubbed) ----------------
def test_pipeline_holds_entry_and_reemits_only_real_ones():
    from vehicle_intelligence.pipeline import Pipeline

    class V:  # minimal vehicle
        track_id = 3
        uid = "veh-3"

    emitted = []
    p = Pipeline.__new__(Pipeline)
    p.handoff = HandoffCoordinator()
    p.cam = "CAM-02"
    p.display_ts = 20.0
    # record what _emit would build once the hold is lifted
    orig_emit = Pipeline._emit

    def spy(self, out, etype, v, event_time, **kw):
        if kw.get("_handoff_checked"):
            emitted.append((etype, event_time))
            return
        return orig_emit(self, out, etype, v, event_time, **kw)

    Pipeline._emit = spy
    try:
        p._emit([], "ENTRY", V(), 19.5)                       # raw tripwire crossing: held
        assert emitted == []
        p.handoff.note_sighting("CAM-01", 1, "GID-9", 15.0)
        p.handoff.note_sighting("CAM-02", 3, "GID-9", 20.0)    # same car came from CAM-01
        out = []
        p._release_handoff(out, 20.1)
        assert emitted == [] and p.handoff.transfers[0]["reason"].startswith("TRANSFER_IN")

        p.display_ts = 40.0
        p._emit([], "ENTRY", V(), 39.0)
        p.handoff.note_sighting("CAM-02", 3, "GID-9", 41.0)
        p.handoff.sightings["GID-9"].pop("CAM-01")             # pretend it was never elsewhere
        p.handoff.inside["GID-9"] = False
        p._release_handoff(out, 41.0)
        assert emitted == [("ENTRY", 39.0)]                    # original crossing time preserved
    finally:
        Pipeline._emit = orig_emit
