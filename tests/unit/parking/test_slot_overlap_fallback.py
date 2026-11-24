import numpy as np

from vehicle_intelligence.logic import SlotManager


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
    from vehicle_intelligence.tracker import Track
    from vehicle_intelligence.vehicles import VehicleRegistry

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


def test_spatially_confirmed_occupancy_does_not_invent_a_gate_event():
    manager, track, registry, vehicle = _entry_gate_fixture()

    events = []
    for ts in (10.0, 11.0):
        track.update((35, 55, 65, 95), 0.9, 2, ts)
        events.extend(manager.update([track], ts, registry))

    assert "BAY-A" not in manager.occupants
    track.update((35, 55, 65, 95), 0.9, 2, 12.0)
    events.extend(manager.update([track], 12.0, registry))

    assert events == []
    assert manager.occupants["BAY-A"].vehicle is vehicle
    assert manager.occupants["BAY-A"].parked_at is None
    assert manager.slot_states()[0]["occupancy_source"] == "OBSERVED_OCCUPANCY"
    assert manager.slot_states()[0]["state"] == "OCCUPIED"


def test_observed_occupancy_is_reflected_in_report_and_ends_on_departure():
    from types import SimpleNamespace

    from vehicle_intelligence.reporting.report import slot_records_for

    manager, track, registry, vehicle = _entry_gate_fixture()
    other_camera, _, _, _ = _entry_gate_fixture()
    for ts in (10.0, 11.0, 12.0):
        track.update((35, 55, 65, 95), 0.9, 2, ts)
        assert manager.update([track], ts, registry) == []

    def records(slots, camera_id):
        return slot_records_for(SimpleNamespace(
            cfg={"camera_id": camera_id}, pipeline=SimpleNamespace(slots=slots),
        ))

    occupied = records(manager, "north")[0]
    assert occupied["state"] == "OCCUPIED"
    assert occupied["vehicle_uid"] == vehicle.uid
    assert occupied["parked_at_epoch"] is None
    assert occupied["parked_at_label"] == "Occupied (entry unverified)"
    assert records(other_camera, "south")[0]["state"] == "FREE"

    track.update((85, 55, 115, 95), 0.9, 2, 13.0)
    assert manager.update([track], 13.0, registry) == []
    track.update((85, 55, 115, 95), 0.9, 2, 14.0)
    assert manager.update([track], 14.0, registry) == [
        ("PARK_END", vehicle, 12.0, "BAY-A", False),
    ]
    assert records(manager, "north")[0]["state"] == "FREE"


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
    assert manager.occupants["BAY-A"].parked_at is None
    assert vehicle.has_parked is True


def test_baseline_survives_temporary_track_loss_then_expires():
    from vehicle_intelligence.tracker import Track

    manager, track, registry, vehicle = _entry_gate_fixture(baseline=True)
    for ts in (10.0, 10.5, 11.1):
        track.update((35, 55, 65, 95), 0.9, 2, ts)
        assert manager.update([track], ts, registry) == []

    manager.on_track_removed(track)
    registry.drop_track(track.id)
    assert manager.update([], 13.0, registry) == []
    assert manager.slot_states()[0]["state"] == "OCCUPIED"
    replacement = Track(2, (35, 55, 65, 95), 0.9, 2, 13.0)
    assert manager.recover_lost_track(replacement, 13.0, registry)
    assert registry.by_track[replacement.id] is vehicle
    assert manager.slot_states()[0]["track_id"] == replacement.id
    assert manager.occupants["BAY-A"].parked_at is None

    manager.on_track_removed(replacement)
    assert manager.expire(43.0) == [("PARK_END", vehicle, 13.0, "BAY-A", True)]
    assert manager.slot_states()[0]["state"] == "FREE"


def test_fresh_parking_cannot_rebind_an_expired_reservation():
    from vehicle_intelligence.tracker import Track

    manager, track, registry, original = _entry_gate_fixture(baseline=True)
    for ts in (10.0, 10.5, 11.1):
        track.update((35, 55, 65, 95), 0.9, 2, ts)
        assert manager.update([track], ts, registry) == []
    manager.on_track_removed(track)
    registry.drop_track(track.id)

    replacement = Track(2, (35, 55, 65, 95), 0.9, 2, 39.1)
    current = registry.get_or_create(replacement, 39.1)
    assert current is not original
    assert manager.update([replacement], 39.1, registry) == []
    replacement.update((35, 55, 65, 95), 0.9, 2, 41.2)

    assert manager.update([replacement], 41.2, registry) == [
        ("PARK_END", original, 11.1, "BAY-A", True),
    ]
    assert manager.occupants["BAY-A"].vehicle is current

