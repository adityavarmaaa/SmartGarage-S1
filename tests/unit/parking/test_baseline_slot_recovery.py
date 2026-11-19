from s1.logic import SlotManager, Occupant


def _manager():
    return SlotManager(
        [{"slot_id": "B-03", "polygon": [[5, 5], [90, 5], [90, 90], [5, 90]]}],
        (100, 100),
        {"baseline_window_s": 15.0},
    )


def test_confirmed_baseline_slot_survives_later_contact_loss():
    manager = _manager()
    manager.stream_start_ts = 100.0
    state = {"baseline_slot": "B-03", "candidate_slot": None}

    assert manager._confirmed_baseline_slot(4, state, 116.0) == "B-03"


def test_confirmed_baseline_slot_is_not_consumed_inside_scan_window():
    manager = _manager()
    manager.stream_start_ts = 100.0
    state = {"baseline_slot": "B-03"}

    assert manager._confirmed_baseline_slot(4, state, 115.0) is None


def test_confirmed_baseline_slot_is_not_recovered_if_another_vehicle_owns_it():
    manager = _manager()
    manager.stream_start_ts = 100.0
    manager.occupants["B-03"] = type("Occ", (), {"track_id": 9})()
    state = {"baseline_slot": "B-03"}

    assert manager._confirmed_baseline_slot(4, state, 116.0) is None


def test_confirmed_baseline_slot_is_not_recovered_if_another_track_claims_it():
    manager = _manager()
    manager.stream_start_ts = 100.0
    manager.tstate[9] = {"slot": None, "candidate_slot": "B-03"}
    state = {"baseline_slot": "B-03"}

    assert manager._confirmed_baseline_slot(4, state, 116.0) is None


def test_update_registers_remembered_baseline_even_when_contact_is_lost(monkeypatch):
    from collections import deque

    class Track:
        id = 4
        bbox = (20, 20, 40, 60)
        first_ts = 100.0

    class Vehicle:
        entry_crossing_ts = None
        slot_id = None
        uid = "V1"
        has_parked = False

    class Registry:
        def __init__(self):
            self.by_track = {4: Vehicle()}

        def rebind(self, *args):
            pass

    cfg = {
        "baseline_window_s": 15.0,
        "min_overlap": 0.2,
        "park_dwell_s": 4.0,
        "still_frac": 0.12,
        "moved_frac": 0.35,
        "leave_overlap": 0.3,
        "lost_grace_s": 30.0,
        "baseline_confirm_frames": 3,
        "footprint_frac": 0.3,
        "candidate_gap_grace_s": 1.0,
        "candidate_switch_confirm_s": 1.0,
    }
    manager = SlotManager(
        [{"slot_id": "B-03", "polygon": [[5, 5], [90, 5], [90, 90], [5, 90]]}],
        (100, 100), cfg,
    )
    manager.stream_start_ts = 100.0
    manager.tstate[4] = {
        "slot": None, "candidate_slot": None, "candidate_since": None,
        "candidate_last_seen": None, "pending_slot": None,
        "pending_slot_since": None, "anchor": None, "depart_since": None,
        "baseline_slot": "B-03", "baseline_frac": 0.4,
        "_reference_history": deque(),
    }
    monkeypatch.setattr(manager, "contact_slot", lambda *args, **kwargs: None)

    events = manager.update([Track()], 116.0, Registry())

    assert events == []
    assert "B-03" in manager.occupants
    assert manager.occupants["B-03"].baseline is True
    assert manager.occupants["B-03"].occupancy_source == "INITIAL_SCAN"
