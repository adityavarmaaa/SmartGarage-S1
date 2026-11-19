from types import SimpleNamespace

from s1.report import build_vehicles


def _runtime(camera_id, observations):
    return SimpleNamespace(track_records={str(i): obs for i, obs in enumerate(observations)})


def _obs(camera, track, uid, gid=None, plate=None, status="UNKNOWN", conf=None, reads=0):
    return {
        "camera_id": camera,
        "local_track_id": track,
        "vehicle_uid": uid,
        "global_id": gid,
        "track_confirmed": True,
        "first_seen_video_seconds": float(track),
        "last_seen_video_seconds": float(track) + 5,
        "vehicle_type": "car",
        "colour": "red",
        "number_plate": plate,
        "plate_status": status,
        "ocr_confidence": conf,
        "ocr_consensus_reads": reads,
    }


def test_track_fragments_rebound_to_same_vehicle_uid_count_once_and_keep_one_slot():
    runtime = _runtime("CAM-01", [
        _obs("CAM-01", 4, "vehicle-A", "GID-1", "TS07FN1199", "CANDIDATE", .91, 1),
        _obs("CAM-01", 5, "vehicle-A", "GID-2", "TS07FN1199", "CONFIRMED", .98, 2),
    ])
    slots = {"CAM-01": [{
        "camera_id": "CAM-01", "slot_id": "B-01", "state": "OCCUPIED",
        "vehicle_uid": "vehicle-A", "baseline": False,
        "parked_at_label": "Video +00:00:09", "plate": "TS07FN1199",
        "plate_status": "CONFIRMED", "colour": "red",
    }]}

    vehicles = build_vehicles({"CAM-01": runtime}, slots, {"backend": "histogram_fallback"})

    assert len(vehicles) == 1
    assert vehicles[0]["state"] == "PARKED"
    assert vehicles[0]["slot_id"] == "B-01"
    assert vehicles[0]["plate_status"] == "CONFIRMED"
    assert len(vehicles[0]["camera_observations"]) == 2


def test_same_global_id_does_not_merge_two_different_same_camera_vehicle_uids():
    runtime = _runtime("CAM-01", [
        _obs("CAM-01", 1, "vehicle-A", "GID-1"),
        _obs("CAM-01", 2, "vehicle-B", "GID-1"),
    ])
    vehicles = build_vehicles({"CAM-01": runtime}, {"CAM-01": []}, {"backend": "osnet"})
    assert len(vehicles) == 2


def test_osnet_global_id_merges_one_local_vehicle_per_camera():
    r1 = _runtime("CAM-01", [_obs("CAM-01", 1, "vehicle-A", "GID-1")])
    r2 = _runtime("CAM-02", [_obs("CAM-02", 2, "vehicle-B", "GID-1")])
    vehicles = build_vehicles(
        {"CAM-01": r1, "CAM-02": r2},
        {"CAM-01": [], "CAM-02": []},
        {"backend": "osnet"},
    )
    assert len(vehicles) == 1
    assert set(vehicles[0]["cameras"]) == {"CAM-01", "CAM-02"}
    assert vehicles[0]["global_id"] == "GID-1"


def test_histogram_fallback_does_not_merge_cross_camera_by_global_id_alone():
    r1 = _runtime("CAM-01", [_obs("CAM-01", 1, "vehicle-A", "GID-1")])
    r2 = _runtime("CAM-02", [_obs("CAM-02", 2, "vehicle-B", "GID-1")])
    vehicles = build_vehicles(
        {"CAM-01": r1, "CAM-02": r2},
        {"CAM-01": [], "CAM-02": []},
        {"backend": "histogram_fallback"},
    )
    assert len(vehicles) == 2


def test_confirmed_plate_can_link_local_identities_across_cameras():
    r1 = _runtime("CAM-01", [_obs("CAM-01", 1, "vehicle-A", "GID-1", "TS07FN1199", "CONFIRMED", .98, 2)])
    r2 = _runtime("CAM-02", [_obs("CAM-02", 2, "vehicle-B", "GID-2", "TS07FN1199", "CONFIRMED", .99, 2)])
    vehicles = build_vehicles(
        {"CAM-01": r1, "CAM-02": r2},
        {"CAM-01": [], "CAM-02": []},
        {"backend": "histogram_fallback"},
    )
    assert len(vehicles) == 1
    assert set(vehicles[0]["cameras"]) == {"CAM-01", "CAM-02"}
    assert vehicles[0]["plate_status"] == "CONFIRMED"


def test_async_reid_shutdown_drains_accepted_jobs():
    import time
    import numpy as np
    from types import SimpleNamespace
    from s1.flow import AsyncGlobalReID
    from s1.global_identity import GlobalIdentityRegistry

    class Embedder:
        def encode(self, crop):
            time.sleep(0.01)
            vector = np.zeros(512, dtype=np.float32)
            vector[0] = 1.0
            return vector

    worker = AsyncGlobalReID(Embedder(), GlobalIdentityRegistry())
    track = SimpleNamespace(confirmed=True, id=7, bbox=(0, 0, 24, 24))
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    worker.update("CAM-01", frame, [track], timestamp=1.0)

    assert worker.stop(timeout=2.0) is True
    assert worker.gid("CAM-01", 7) is not None
    assert worker.q.unfinished_tasks == 0


def test_duplicate_confirmed_plate_in_same_camera_is_flagged_not_silently_merged():
    runtime = _runtime("CAM-01", [
        _obs("CAM-01", 1, "vehicle-A", "GID-1", "TS07FN1199", "CONFIRMED", .98, 2),
        _obs("CAM-01", 2, "vehicle-B", "GID-2", "TS07FN1199", "CONFIRMED", .99, 2),
    ])
    vehicles = build_vehicles({"CAM-01": runtime}, {"CAM-01": []}, {"backend": "histogram_fallback"})
    assert len(vehicles) == 2
    assert all(v["plate_status"] == "CONFLICT" for v in vehicles)


def test_one_identity_claiming_two_active_slots_is_reported_as_conflict():
    r1 = _runtime("CAM-01", [_obs("CAM-01", 1, "vehicle-A", "GID-1")])
    r2 = _runtime("CAM-02", [_obs("CAM-02", 2, "vehicle-B", "GID-1")])
    slots = {
        "CAM-01": [{"camera_id": "CAM-01", "slot_id": "B-01", "state": "OCCUPIED",
                     "vehicle_uid": "vehicle-A", "baseline": False}],
        "CAM-02": [{"camera_id": "CAM-02", "slot_id": "B-02", "state": "OCCUPIED",
                     "vehicle_uid": "vehicle-B", "baseline": False}],
    }
    vehicles = build_vehicles({"CAM-01": r1, "CAM-02": r2}, slots, {"backend": "osnet"})
    assert len(vehicles) == 1
    assert vehicles[0]["state"] == "IDENTITY_CONFLICT"
    assert vehicles[0]["slot_id"] is None
    assert len(vehicles[0]["slot_assignments"]) == 2
