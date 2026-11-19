import numpy as np

from s1.logic import SlotManager


def _slot(slot_id, x1, x2):
    return {
        "slot_id": slot_id,
        "level": None,
        "polygon": [[x1, 50], [x2, 50], [x2, 100], [x1, 100]],
    }


def _manager(slots, **overrides):
    cfg = {
        "footprint_frac": 0.45,
        "slot_boundary_margin_px": 6.0,
        "slot_boundary_switch_fraction": 0.55,
        "slot_overlap_fallback_min": 0.35,
        "slot_overlap_fallback_margin": 0.15,
    }
    cfg.update(overrides)
    return SlotManager(slots, (120, 120), cfg)


def test_overlap_fallback_recovers_clipped_ground_contact_when_one_slot_is_clear():
    manager = _manager([_slot("BAY-A", 30, 80)])
    # Bottom-centre x=25 falls outside the polygon, but the lower footprint
    # overlaps 40% of the box with this one calibrated slot.
    assert manager.contact_slot((0, 0, 50, 100), track_id=1) == "BAY-A"


def test_overlap_fallback_rejects_ambiguous_adjacent_slot_overlap():
    manager = _manager([
        _slot("BAY-A", 5, 25),
        _slot("BAY-B", 25, 45),
    ])
    # The bbox has similar overlap with both bays: no arbitrary assignment.
    assert manager.contact_slot((0, 0, 50, 100), track_id=1) is None


def test_overlap_fallback_respects_existing_slot_reservations():
    manager = _manager([_slot("BAY-A", 30, 80)])
    from types import SimpleNamespace
    manager.occupants["BAY-A"] = SimpleNamespace(track_id=99)
    assert manager.contact_slot((0, 0, 50, 100), track_id=1) is None

def _entry_gate_fixture(entry_crossing_ts=None, baseline=False):
    from s1.tracker import Track
    from s1.vehicles import VehicleRegistry

    cfg = {
        "min_overlap": 0.20,
        "leave_overlap": 0.30,
        "moved_frac": 0.35,
        "still_frac": 0.12,
        "park_dwell_s": 2.0,
        "leave_dwell_s": 1.0,
        "lost_grace_s": 30.0,
        "baseline_window_s": 1.0,
        "baseline_confirm_frames": 2,
        "baseline_stationary_s": 0.5,
        "candidate_gap_grace_s": 1.0,
        "candidate_switch_confirm_s": 1.0,
    }

    manager = _manager([_slot("BAY-A", 30, 80)], **cfg)
    manager.stream_start_ts = 10.0 if baseline else 0.0

    track = Track(1, (35, 55, 65, 95), 0.9, 2, 10.0)
    registry = VehicleRegistry()
    vehicle = registry.get_or_create(track, 10.0)
    vehicle.entry_crossing_ts = entry_crossing_ts

    return manager, track, registry, vehicle


def test_live_parking_requires_confirmed_entry_crossing():
    manager, track, registry, vehicle = _entry_gate_fixture()

    events = []
    for ts in (10.0, 11.0, 12.0, 13.0):
        track.update((35, 55, 65, 95), 0.9, 2, ts)
        events.extend(manager.update([track], ts, registry))

    assert not any(event[0] == "PARK_START" for event in events)
    assert "BAY-A" not in manager.occupants


def test_confirmed_entry_allows_park_start_after_configured_dwell():
    manager, track, registry, vehicle = _entry_gate_fixture(
        entry_crossing_ts=10.0
    )

    events = []
    for ts in (10.0, 11.0, 12.0):
        track.update((35, 55, 65, 95), 0.9, 2, ts)
        events.extend(manager.update([track], ts, registry))

    park_events = [event for event in events if event[0] == "PARK_START"]

    assert len(park_events) == 1
    assert park_events[0][1] is vehicle
    assert park_events[0][2] == 10.0
    assert park_events[0][3] == "BAY-A"
    assert manager.occupants["BAY-A"].occupancy_source == "LIVE_ENTRY"


def test_startup_baseline_occupant_does_not_emit_park_start():
    manager, track, registry, vehicle = _entry_gate_fixture(baseline=True)

    events = []
    for ts in (10.0, 10.5, 11.1):
        track.update((35, 55, 65, 95), 0.9, 2, ts)
        events.extend(manager.update([track], ts, registry))

    assert not any(event[0] == "PARK_START" for event in events)
    assert manager.occupants["BAY-A"].vehicle is vehicle
    assert manager.occupants["BAY-A"].occupancy_source == "INITIAL_SCAN"
    assert vehicle.has_parked is True

