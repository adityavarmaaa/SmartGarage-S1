
import json
import hashlib
from pathlib import Path
from datetime import datetime, timezone


def _sha256(path):
    p = Path(path)
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _normal_plate(value):
    if value is None:
        return None
    value = "".join(ch for ch in str(value).upper() if ch.isalnum())
    return value or None


def build_entry_manifest(report_path, manifest_path):
    """
    Build EXIT restoration data exclusively from the latest ENTRY report.

    Slot occupancy is authoritative from report['cameras'][camera]['slots'].
    Candidate plates remain candidates. No OCR votes are fabricated.
    """
    report_path = Path(report_path).resolve()
    manifest_path = Path(manifest_path).resolve()

    if not report_path.is_file():
        raise FileNotFoundError(f"ENTRY report missing: {report_path}")

    report = json.loads(report_path.read_text(encoding="utf-8"))

    if report.get("run_status") != "COMPLETED":
        raise RuntimeError(
            f"ENTRY report is not COMPLETED: {report.get('run_status')}"
        )

    cameras = report.get("cameras")
    if not isinstance(cameras, dict) or not cameras:
        raise RuntimeError("ENTRY report has no camera slot records.")

    lifecycle = report.get("lifecycle_events") or []
    vehicles = report.get("vehicles") or []
    vehicle_by_uid = {
        str(v["vehicle_uid"]): v
        for v in vehicles
        if isinstance(v, dict) and v.get("vehicle_uid")
    }

    # Map only evidence explicitly associated with the same camera and slot.
    evidence_by_camera_slot = {}
    for ev in report.get("plate_evidence") or []:
        if not isinstance(ev, dict):
            continue
        key = (str(ev.get("camera_id")), str(ev.get("slot_id")))
        evidence_by_camera_slot.setdefault(key, []).append(ev)

    records = []
    seen_slots = set()
    seen_uids = set()
    confirmed_plate_owners = {}

    for camera_id, camera_data in cameras.items():
        camera_id = str(camera_id)
        slots = camera_data.get("slots") or []

        if not isinstance(slots, list):
            raise RuntimeError(f"{camera_id}: slots must be a list.")

        for slot in slots:
            if str(slot.get("state", "")).upper() != "OCCUPIED":
                continue

            slot_id = str(slot.get("slot_id") or "").strip()
            uid = str(slot.get("vehicle_uid") or "").strip()

            if not slot_id:
                raise RuntimeError(f"{camera_id}: occupied slot has no slot_id.")
            if not uid or uid.lower() == "none":
                raise RuntimeError(
                    f"{camera_id}/{slot_id}: occupied slot has no vehicle_uid."
                )

            slot_key = (camera_id, slot_id)
            if slot_key in seen_slots:
                raise RuntimeError(f"Duplicate occupied slot: {slot_key}")
            seen_slots.add(slot_key)

            if uid in seen_uids:
                raise RuntimeError(
                    f"One vehicle UID occupies multiple slots in ENTRY: {uid}"
                )
            seen_uids.add(uid)

            plate_status = str(slot.get("plate_status") or "UNKNOWN").upper()
            plate = _normal_plate(slot.get("plate"))

            # Preserve only a plate explicitly marked CONFIRMED.
            # A candidate/unconfirmed read is stored separately.
            confirmed_plate = plate if plate_status == "CONFIRMED" else None
            candidate_plate = (
                plate if plate_status in {"CANDIDATE", "UNCONFIRMED"} else None
            )

            if confirmed_plate:
                owner = confirmed_plate_owners.get(confirmed_plate)
                if owner and owner != uid:
                    raise RuntimeError(
                        "Confirmed plate collision in ENTRY report: "
                        f"{confirmed_plate} belongs to {owner} and {uid}. "
                        "Resolve evidence before EXIT; refusing to merge."
                    )
                confirmed_plate_owners[confirmed_plate] = uid

            vehicle = vehicle_by_uid.get(uid, {})
            matching_evidence = evidence_by_camera_slot.get(
                (camera_id, slot_id), []
            )

            # Evidence is historical metadata only. It is never an OCR vote.
            selected_evidence = None
            for item in matching_evidence:
                item_plate = _normal_plate(
                    item.get("plate") or item.get("plate_text")
                )
                if confirmed_plate and item_plate == confirmed_plate:
                    selected_evidence = item
                    break

            historical_evidence = {
                "source": "ENTRY_REPORT",
                "historically_confirmed": bool(confirmed_plate),
                "plate_text": confirmed_plate,
                "ocr_confidence": (
                    selected_evidence.get("ocr_confidence")
                    if selected_evidence else None
                ),
                "original_image": (
                    selected_evidence.get("original_image")
                    if selected_evidence else None
                ),
                "clahe_image": (
                    selected_evidence.get("clahe_image")
                    if selected_evidence else None
                ),
            }

            # ENTRY/PARK_START events are selected by exact camera + UID.
            uid_events = [
                e for e in lifecycle
                if isinstance(e, dict)
                and str(e.get("camera_id")) == camera_id
                and str(e.get("vehicle_uid")) == uid
            ]
            entry_events = [
                e for e in uid_events
                if str(e.get("event_type", "")).upper() == "ENTRY"
            ]
            park_events = [
                e for e in uid_events
                if str(e.get("event_type", "")).upper() == "PARK_START"
            ]

            baseline = bool(slot.get("baseline"))
            if not baseline and len(park_events) != 1:
                raise RuntimeError(
                    f"{camera_id}/{slot_id}/{uid}: expected exactly one "
                    f"PARK_START for live entry; found {len(park_events)}."
                )
            if len(entry_events) > 1:
                raise RuntimeError(
                    f"{camera_id}/{slot_id}/{uid}: duplicate ENTRY events."
                )

            # Validate event IDs before allowing lifecycle replay.
            for event in entry_events + park_events:
                if not event.get("event_id"):
                    raise RuntimeError(
                        f"{camera_id}/{slot_id}/{uid}: lifecycle event missing event_id."
                    )

            records.append({
                "camera_id": camera_id,
                "parking_area_id": slot.get("parking_area_id"),
                "slot_id": slot_id,
                "level": slot.get("level"),
                "vehicle_uid": uid,
                "global_id": vehicle.get("global_id"),
                "previous_track_id": slot.get("track_id"),
                "occupancy_source": slot.get("occupancy_source"),
                "baseline": baseline,
                "plate": confirmed_plate,
                "plate_status": (
                    "CONFIRMED" if confirmed_plate
                    else ("CANDIDATE" if candidate_plate else "UNKNOWN")
                ),
                "plate_candidate": candidate_plate,
                "vehicle_type": slot.get("vehicle_type") or vehicle.get("vehicle_type"),
                "colour": slot.get("colour") or vehicle.get("colour"),
                "parked_at_epoch": slot.get("parked_at_epoch"),
                "parked_at_ist": slot.get("parked_at_ist"),
                "recording_started_at_ist": slot.get("recording_started_at_ist"),
                "entry_event": entry_events[0] if entry_events else None,
                "park_start_event": park_events[0] if park_events else None,
                "restoration_status": "READY_FOR_EXIT_RESTORE",
                "historical_plate_evidence": historical_evidence,
            })

    if not records:
        raise RuntimeError("ENTRY report produced no occupied slots to restore.")

    # A single camera/slot cannot appear more than once. Every UID is unique.
    if len({(r["camera_id"], r["slot_id"]) for r in records}) != len(records):
        raise RuntimeError("Manifest contains duplicate camera/slot records.")
    if len({r["vehicle_uid"] for r in records}) != len(records):
        raise RuntimeError("Manifest contains duplicate vehicle UIDs.")

    manifest = {
        "schema_version": 2,
        "purpose": "FRESH_ENTRY_TO_EXIT_IDENTITY_RESTORATION",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_report": str(report_path),
        "source_report_sha256": _sha256(report_path),
        "source_report_generated_at": report.get("generated_at"),
        "source_summary": report.get("summary", {}),
        "records": records,
        "validation": {
            "record_count": len(records),
            "camera_count": len({r["camera_id"] for r in records}),
            "unique_uid_count": len({r["vehicle_uid"] for r in records}),
            "confirmed_plate_count": sum(
                r["plate_status"] == "CONFIRMED" for r in records
            ),
            "candidate_plate_count": sum(
                r["plate_status"] == "CANDIDATE" for r in records
            ),
            "baseline_count": sum(bool(r["baseline"]) for r in records),
            "live_entry_count": sum(not bool(r["baseline"]) for r in records),
            "historical_evidence_is_not_fresh_ocr": True,
        },
    }

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temp_path.replace(manifest_path)

    # Read-after-write validation.
    check = json.loads(manifest_path.read_text(encoding="utf-8"))
    if len(check.get("records", [])) != len(records):
        raise RuntimeError("Manifest read-after-write validation failed.")

    print("FRESH ENTRY MANIFEST:", manifest_path)
    print(json.dumps(manifest["validation"], indent=2))
    return manifest


def validate_lifecycle(report_path):
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    events = report.get("lifecycle_events") or []
    errors = []
    grouped = {}

    for event in events:
        if not isinstance(event, dict):
            continue
        uid = event.get("vehicle_uid")
        camera = event.get("camera_id")
        kind = str(event.get("event_type", "")).upper()
        if uid and camera:
            grouped.setdefault((str(camera), str(uid)), []).append(event)

    for (camera, uid), rows in grouped.items():
        rows.sort(key=lambda x: (
            x.get("event_time") or x.get("timestamp") or ""
        ))
        kinds = [str(x.get("event_type", "")).upper() for x in rows]
        if kinds.count("PARK_END") != kinds.count("EXIT"):
            errors.append(
                f"{camera}/{uid}: PARK_END={kinds.count('PARK_END')} "
                f"EXIT={kinds.count('EXIT')}"
            )
        for row in rows:
            if str(row.get("event_type", "")).upper() in {
                "ENTRY", "PARK_START", "PARK_END", "EXIT"
            } and not row.get("event_id"):
                errors.append(f"{camera}/{uid}: missing event_id")

    return {"events": len(events), "identity_groups": len(grouped), "errors": errors}
