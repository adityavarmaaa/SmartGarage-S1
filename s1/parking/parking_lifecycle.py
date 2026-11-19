"""One readable row per park cycle: ENTRY -> PARK_START -> PARK_END -> EXIT.

Built purely from the raw Service-1 lifecycle events (already IST ISO-8601), so
it adds no new detection logic. Times are the event times the pipeline
produced; nothing is invented. A missing stage is reported as null.
"""
from datetime import datetime

_ORDER = {"ENTRY": 0, "PARK_START": 1, "PARK_END": 2, "EXIT": 3}
_FIELD = {"ENTRY": "entry_time_ist", "PARK_START": "park_start_time_ist",
          "PARK_END": "park_end_time_ist", "EXIT": "exit_time_ist"}


def _parse(ts):
    try:
        return datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None


def _new_row(camera, uid):
    return {
        "camera_id": camera, "vehicle_uid": uid, "slot_id": None,
        "plate": None, "plate_status": None, "colour": None, "vehicle_type": None,
        "entry_time_ist": None, "park_start_time_ist": None,
        "park_end_time_ist": None, "exit_time_ist": None,
        "exit_inferred": False, "parked_duration_s": None, "status": None,
    }


def _status(row):
    if row["exit_time_ist"]:
        return "EXITED"
    if row["park_end_time_ist"]:
        return "LEFT_SLOT"
    if row["park_start_time_ist"]:
        return "PARKED"
    if row["entry_time_ist"]:
        return "ENTERED"
    return "SEEN"


def build_timeline(raw_events):
    """raw_events: list of event dicts from the preview publishers."""
    groups = {}
    for ev in raw_events or []:
        etype = ev.get("event_type")
        if etype not in _ORDER:
            continue
        groups.setdefault((ev.get("camera_id"), ev.get("vehicle_uid")), []).append(ev)

    rows = []
    for (cam, uid), events in groups.items():
        events.sort(key=lambda e: (str(e.get("event_time") or e.get("timestamp") or ""),
                                   _ORDER[e["event_type"]]))
        row = None
        seen = set()
        for ev in events:
            etype = ev["event_type"]
            field = _FIELD[etype]
            when = ev.get("event_time") or ev.get("timestamp")
            slot = (ev.get("bay") or {}).get("id") or (ev.get("parking") or {}).get("slot_id")
            observation = (etype, when, slot)
            if uid and when and observation in seen:
                # Preserve a later confirmed status for the same observed transition.
                if row is not None and row[field] == when:
                    incoming = ev.get("plate_status") or (ev.get("plate") or {}).get("status")
                    if incoming == "CONFIRMED":
                        row["plate_status"] = "CONFIRMED"
                continue
            seen.add(observation)
            if row is None or row[field] is not None or (
                    etype in ("ENTRY", "PARK_START") and row["park_end_time_ist"]):
                if row is not None:
                    rows.append(row)
                row = _new_row(cam, uid)
            row[field] = when
            if etype == "EXIT":
                row["exit_inferred"] = bool(ev.get("inferred"))
            if slot and not row["slot_id"]:
                row["slot_id"] = slot
            details = ev.get("vehicle_details") or ev.get("vehicle") or {}
            # Never expose plate text; retain the strongest lifecycle evidence.
            incoming_status = (
                ev.get("plate_status")
                or (ev.get("plate") or {}).get("status")
            )
            if incoming_status == "CONFIRMED":
                row["plate_status"] = "CONFIRMED"
            elif row["plate_status"] is None:
                row["plate_status"] = incoming_status
            row["colour"] = details.get("color") or row["colour"]
            row["vehicle_type"] = details.get("type") or row["vehicle_type"]
        if row is not None:
            rows.append(row)

    for row in rows:
        a, b = _parse(row["park_start_time_ist"]), _parse(row["park_end_time_ist"])
        if a and b:
            row["parked_duration_s"] = round((b - a).total_seconds(), 3)
        row["status"] = _status(row)

    rows.sort(key=lambda r: (str(r["entry_time_ist"] or r["park_start_time_ist"]
                                 or r["park_end_time_ist"] or r["exit_time_ist"] or ""),
                             str(r["camera_id"]), str(r["vehicle_uid"])))
    return rows
