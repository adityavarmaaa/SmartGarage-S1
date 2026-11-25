"""Correlate newly observed gate and bay events with acknowledged PostgreSQL rows."""
from datetime import datetime


def _timestamp(value):
    try:
        result = datetime.fromisoformat(value)
        return result if result.utcoffset() is not None else None
    except (TypeError, ValueError):
        return None


def verify_recording_lifecycle(report, database):
    """Require one continuous observed sequence for a single camera-local identity."""
    observations = {}
    counts = {kind: 0 for kind in ("ENTRY", "PARK_START", "PARK_END", "EXIT")}
    for event in report["lifecycle_events"]:
        kind = event.get("event_type")
        if kind not in counts or event.get("observation_origin") == "restored" or event.get("inferred") is not False:
            continue
        event_id = event.get("event_id")
        uid = event.get("vehicle_uid")
        camera = event.get("camera_id")
        area = event.get("parking_area_id")
        time = _timestamp(event.get("event_time"))
        if not (event_id and uid and camera and area and time is not None):
            continue
        slot = (event.get("parking") or {}).get("slot_id")
        observations.setdefault((area, camera, uid), []).append((time, event_id, kind, slot))
        counts[kind] += 1

    available = {(row["camera_id"], row["slot_id"]) for row in database["final_slot_state"]
                 if row["state"] == "available"}
    free = {(row["camera_id"], row["slot_id"]) for row in report["slot_details"] if row["state"] == "FREE"}
    verified = []
    for session in database["closed_sessions"]:
        key = (session["parking_area_id"], session["camera_id"], session["vehicle_uid"])
        slot = session["slot_id"]
        if (key[1], slot) not in (available & free):
            continue
        start = _timestamp(session["entry_time"])
        end = _timestamp(session["exit_time"])
        if start is None or end is None:
            continue
        records = {(event["event_type"], event["sender_event_id"]): event["database_event_id"]
                   for event in database["persisted_events"]
                   if (event["parking_area_id"], event["camera_id"], event["vehicle_uid"]) == key
                   and event["slot_id"] == slot
                   and _timestamp(event["event_time"]) == (start if event["event_type"] == "ENTRY" else end)}
        observations_for_vehicle = sorted(observations.get(key, []),
                                          key=lambda item: (item[0], tuple(counts).index(item[2]), item[1]))
        gate_entry = None
        park_start = None
        park_end = None
        for time, event_id, kind, observed_slot in observations_for_vehicle:
            if kind == "ENTRY":
                gate_entry, park_start, park_end = (time, event_id), None, None
            elif kind == "PARK_START":
                park_start = (time, event_id) if gate_entry and time == start and observed_slot == slot else None
                park_end = None
            elif kind == "PARK_END":
                park_end = (time, event_id) if park_start and time == end and observed_slot == slot else None
            elif kind == "EXIT":
                if (gate_entry and park_start and park_end and observed_slot in (None, slot)
                        and ("ENTRY", park_start[1]) in records and ("EXIT", park_end[1]) in records):
                    verified.append({
                        **session,
                        "observation_ids": {"ENTRY": gate_entry[1], "PARK_START": park_start[1],
                                            "PARK_END": park_end[1], "EXIT": event_id},
                        "gate_entry_time": gate_entry[0].isoformat(), "gate_exit_time": time.isoformat(),
                        "database_entry_event_id": records["ENTRY", park_start[1]],
                        "database_exit_event_id": records["EXIT", park_end[1]],
                    })
                    break
                gate_entry, park_start, park_end = None, None, None

    return {"status": "passed" if verified else "blocked", "observed_counts": counts,
            "restored_counts": report.get("restored_lifecycle_counts", {}),
            "correlated_sessions": verified,
            "timestamp_source": report.get("run_info", {}).get("determinism", {}).get("timestamp_source"),
            "cross_camera_verified": False}
