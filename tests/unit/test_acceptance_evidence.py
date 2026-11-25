from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from scripts.acceptance_evidence import verify_recording_lifecycle


@pytest.fixture
def correlated():
    area, camera, slot, uid = (uuid4().hex for _ in range(4))
    identity = {"parking_area_id": area, "camera_id": camera, "vehicle_uid": uid}
    anchor = datetime.now(timezone.utc)
    events = [
        {**identity, "event_id": uuid4().hex, "event_type": kind, "inferred": False,
         "event_time": (anchor + timedelta(seconds=offset)).isoformat(), "parking": {"slot_id": slot}}
        for offset, kind in enumerate(("ENTRY", "PARK_START", "PARK_END", "EXIT"))
    ]
    report = {"lifecycle_events": events, "slot_details": [{"camera_id": camera, "slot_id": slot, "state": "FREE"}]}
    database = {
        "final_slot_state": [{"camera_id": camera, "slot_id": slot, "state": "available"}],
        "closed_sessions": [{**identity, "slot_id": slot, "database_session_id": 1,
                             "entry_time": events[1]["event_time"], "exit_time": events[2]["event_time"],
                             "duration_seconds": 1}],
        "persisted_events": [
            {**identity, "slot_id": slot, "event_type": kind, "sender_event_id": event["event_id"],
             "database_event_id": index, "event_time": event["event_time"]}
            for index, (kind, event) in enumerate(zip(("ENTRY", "EXIT"), events[1:3]), 1)
        ],
    }
    return report, database


def test_acceptance_correlates_real_observation_shape_to_stored_session(correlated):
    report, database = correlated
    result = verify_recording_lifecycle(report, database)
    assert result["status"] == "passed"
    session, = result["correlated_sessions"]
    assert session["database_entry_event_id"] == database["persisted_events"][0]["database_event_id"]
    assert session["observation_ids"]["EXIT"] == report["lifecycle_events"][-1]["event_id"]


@pytest.mark.parametrize("gap", ["missing_exit", "restored_entry", "inferred_departure", "different_identity",
                                 "wrong_sender_id", "wrong_slot", "occupancy_mismatch", "exit_before_departure"])
def test_missing_grounded_lifecycle_blocks_even_with_closed_database_session(correlated, gap):
    report, database = correlated
    events = report["lifecycle_events"]
    if gap == "missing_exit":
        events.pop()
    elif gap == "restored_entry":
        events[0]["observation_origin"] = "restored"
    elif gap == "inferred_departure":
        events[2]["inferred"] = True
    elif gap == "different_identity":
        events[-1]["vehicle_uid"] = uuid4().hex
    elif gap == "wrong_sender_id":
        database["persisted_events"][1]["sender_event_id"] = uuid4().hex
    elif gap == "wrong_slot":
        events[2]["parking"]["slot_id"] = uuid4().hex
    elif gap == "occupancy_mismatch":
        report["slot_details"][0]["state"] = "UNKNOWN"
    else:
        events[-1]["event_time"] = events[0]["event_time"]
    assert verify_recording_lifecycle(report, database)["status"] == "blocked"
