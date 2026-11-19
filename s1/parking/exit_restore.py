
import json
from pathlib import Path
from s1.vehicles import Vehicle
from .logic import Occupant


def restore_camera_occupants(pipeline, camera_id, first_frame_ts, manifest_path, entry_report_path=None):
    """
    Restore only this camera's six-slot session records.
    No historical timestamp is invented.
    """
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    records = [
        r for r in manifest["records"]
        if r.get("camera_id") == camera_id
    ]

    # Recover historical candidate plate text from ENTRY report.
    # This does not create OCR votes or promote candidates to confirmed.
    entry_vehicles = {}
    if entry_report_path:
        report_file = Path(entry_report_path)
        if not report_file.is_absolute():
            report_file = Path.cwd() / report_file
        if report_file.is_file():
            with report_file.open("r", encoding="utf-8") as handle:
                entry_report = json.load(handle)
            entry_vehicles = {
                str(item.get("vehicle_uid")): item
                for item in entry_report.get("vehicles", [])
                if item.get("vehicle_uid")
            }


    expected = {
        str(slot_id): slot
        for slot_id, slot in pipeline.slots.slots.items()
    }

    restored = []
    errors = []

    for record in records:
        sid = str(record["slot_id"])

        if sid not in expected:
            errors.append(f"{sid}: slot not present in camera geometry")
            continue

        if sid in pipeline.slots.occupants:
            errors.append(f"{sid}: slot already occupied")
            continue

        uid = str(record["vehicle_uid"])
        if uid in pipeline.registry.by_uid:
            errors.append(f"{sid}: UID already registered")
            continue

        vehicle = Vehicle(
            uid=uid,
            track_id=-1,
            first_ts=float(first_frame_ts),
        )

        vehicle.slot_id = sid
        vehicle.last_parked_slot = sid
        vehicle.has_parked = True

        # Restore descriptive attributes without manufacturing OCR votes.
        vehicle.restored_vehicle_type = record.get("vehicle_type")
        vehicle.restored_colour = record.get("colour")
        vehicle.historical_plate_evidence = record.get(
            "historical_plate_evidence"
        )
        entry_vehicle = entry_vehicles.get(uid, {})
        if (
            record.get("plate_status") == "CANDIDATE"
            or entry_vehicle.get("plate_status") == "CANDIDATE"
        ):
            vehicle.historical_plate_candidate = (
                record.get("plate_candidate")
                or record.get("plate")
                or entry_vehicle.get("plate")
                or entry_vehicle.get("number_plate")
            )
        else:
            vehicle.historical_plate_candidate = None

        vehicle.historical_plate = (
            record.get("plate")
            if record.get("plate_status") == "CONFIRMED"
            and record.get("historical_plate_evidence", {}).get(
                "historically_confirmed"
            )
            else None
        )

        # Keep the original UID addressable before tracker matching.
        pipeline.registry.by_uid[uid] = vehicle

        # EXIT stream timestamp is only the recovery grace anchor.
        ts = float(first_frame_ts)
        occupant = Occupant(
            vehicle=vehicle,
            track_id=None,
            parked_at=ts,
            last_seen=ts,
            lost_since=ts,
            baseline=bool(record.get("baseline")),
            occupancy_source=record.get(
                "occupancy_source", "RESTORED"
            ),
            restoration_pending=True,
        )

        pipeline.slots.occupants[sid] = occupant
        restored.append({
            "slot_id": sid,
            "vehicle_uid": uid,
            "historical_plate": vehicle.historical_plate,
            "timestamp_source": (
                "UNKNOWN"
                if record.get("parked_at_epoch") is None
                else "ENTRY_MANIFEST"
            ),
        })

    return {
        "camera_id": camera_id,
        "restored_count": len(restored),
        "restored": restored,
        "errors": errors,
    }
