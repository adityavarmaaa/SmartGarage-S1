from uuid import uuid4

from vehicle_intelligence.config import load_config
from vehicle_intelligence.logic import SlotManager
from vehicle_intelligence.tracker import Track
from vehicle_intelligence.vehicles import VehicleRegistry


def test_full_lot_does_not_assign_an_already_occupied_slot():
    config = load_config()["parking"]
    config.update(baseline_window_s=0.8, baseline_stationary_s=0.2, park_dwell_s=0.2)
    width, height = 180, 100
    slots = []
    tracks = []
    registry = VehicleRegistry()
    for index in range(2):
        left = 12 + index * (width // 2)
        coords = [(left, 50), (left + 56, 50), (left + 56, 92), (left, 92)]
        slots.append({"slot_id": uuid4().hex, "polygon": coords, "level": None})
        track = Track(index + 1, (left + 5, 40, left + 45, 88), 0.95, 2, 100.0)
        tracks.append(track)
        registry.get_or_create(track, 100.0)
    manager = SlotManager(slots, (width, height), config)
    manager.stream_start_ts = 100.0
    for tick in (100.0, 100.3, 100.6, 101.0):
        for track in tracks:
            track.update(track.bbox, 0.95, 2, tick)
        assert manager.update(tracks, tick, registry) == []
    assert len(manager.occupants) == len(slots)
    intruder = Track(len(tracks) + 1, tracks[0].bbox.copy(), 0.94, 2, 101.1)
    vehicle = registry.get_or_create(intruder, 101.1)
    vehicle.entry_crossing_ts = 101.1
    for tick in (101.1, 101.4, 101.8):
        for track in [*tracks, intruder]:
            track.update(track.bbox, 0.95, 2, tick)
        assert manager.update([*tracks, intruder], tick, registry) == []
    assert {row["state"] for row in manager.slot_states()} == {"OCCUPIED"}
    assert {row["track_id"] for row in manager.slot_states()} == {track.id for track in tracks}
    assert vehicle.slot_id is None
