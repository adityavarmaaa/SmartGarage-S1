import uuid
from datetime import datetime

from vehicle_intelligence.common import iso

SCHEMA_VERSION = 1


def build_event(
    etype,
    vehicle,
    camera_id,
    event_time,
    detected_at,
    direction=None,
    slot=None,
    snapshot_uri=None,
    inferred=False,
    captured_at=None,
    received_at=None,
    parking_area_id=None,
    plate_snapshot_uri=None,
):
    c = vehicle.voter.consensus() if getattr(vehicle, "plate_final", False) else None

    plate_text = None
    plate_confidence = None

    if c:
        plate_text = c.text
        plate_confidence = round(c.conf, 3)

    event_time_iso = iso(event_time)
    detected_at_iso = iso(detected_at)
    captured_at_iso = iso(captured_at if captured_at is not None else event_time)
    received_at_iso = iso(received_at) if received_at is not None else None

    parking = None
    bay = None

    if slot:
        slot_id = slot.get("slot_id")
        level = slot.get("level")

        parking = {
            "slot_id": slot_id,
            "slot_confidence": None,
        }
        bay = {
            "id": slot_id,
            "level": level,
        }

    snapshot = None
    if snapshot_uri or plate_snapshot_uri:
        snapshot = {
            "vehicle": snapshot_uri,
            "plate": plate_snapshot_uri,
        }

    vehicle_type = vehicle.vehicle_type()
    vehicle_color = vehicle.color()

    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": uuid.uuid4().hex,
        "event_type": etype,
        "camera_id": camera_id,
        "parking_area_id": parking_area_id,
        "vehicle_uid": vehicle.uid,
        "track_id": vehicle.track_id,
        "timestamp": event_time_iso,
        "event_time": event_time_iso,
        "detected_at": detected_at_iso,
        "received_at": received_at_iso,
        "captured_at": captured_at_iso,
        "direction": direction,
        "inferred": bool(inferred),
        "vehicle": {
            "type": vehicle_type,
            "type_confidence": None,
            "color": vehicle_color,
            "color_confidence": None,
            "plate": plate_text,
            "plate_confidence": plate_confidence,
        },
        "vehicle_details": {
            "type": vehicle_type,
            "type_confidence": None,
            "color": vehicle_color,
            "color_confidence": None,
            "plate": plate_text,
            "plate_confidence": plate_confidence,
        },
        "plate": {
            "text": plate_text,
            "confidence": plate_confidence,
        },
        "bay": bay,
        "parking": parking,
        "snapshot": snapshot,
    }


def validate_event(event):
    """Check the core S1 envelope before publication, preserving optional fields."""
    for field in ("event_id", "event_type", "camera_id", "vehicle_uid", "event_time"):
        if not isinstance(event.get(field), str) or not event[field].strip():
            raise ValueError(f"{field} must be a non-empty string")
    if datetime.fromisoformat(event["event_time"]).tzinfo is None:
        raise ValueError("event_time must include a timezone")
    if event["event_type"] in {"PARK_START", "PARK_END"}:
        parking = event.get("parking")
        if not isinstance(parking, dict) or not parking.get("slot_id"):
            raise ValueError("parking.slot_id is required for parking transitions")
