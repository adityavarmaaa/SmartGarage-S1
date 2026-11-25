"""Final run report: one clean JSON + a readable console summary.

Everything the DB service needs is under "service2_events" (the exact ENTRY /
EXIT payloads); everything a human needs to judge the run is under "vehicles",
"slots" and "pipeline_health".
"""
import json
import os
import hmac
import tempfile
from datetime import datetime, timedelta, timezone
from vehicle_intelligence.security import decrypt_text, encrypt_text, plate_hmac

IST = timezone(timedelta(hours=5, minutes=30))

_SENSITIVE_PLATE_KEYS = {
    "plate_text", "plate_number", "number_plate",
    "license_plate", "license_plate_number",
}

def _protect_plate_data(value, in_plate=False):
    """Redact plaintext plate values and retain encrypted/HMAC representations."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")

            if normalized == "plate" and isinstance(item, dict):
                result[key] = _protect_plate_data(item, in_plate=True)
                continue

            sensitive = (
                normalized in _SENSITIVE_PLATE_KEYS
                or (in_plate and normalized in {"text", "value", "number", "number_text"})
                or (normalized == "plate" and isinstance(item, str))
            )

            if sensitive:
                result[key] = None
                if item is not None:
                    plaintext = str(item).strip()
                    if plaintext:
                        if plaintext.startswith("enc:v1:"):
                            ciphertext = plaintext
                            fingerprint = None
                        else:
                            ciphertext = encrypt_text(plaintext)
                            fingerprint = plate_hmac(plaintext)
                        result[f"{key}_ciphertext"] = ciphertext
                        if fingerprint:
                            result[f"{key}_hmac"] = fingerprint
            else:
                result[key] = _protect_plate_data(item, in_plate=in_plate)
        return result

    if isinstance(value, list):
        return [_protect_plate_data(item, in_plate=in_plate) for item in value]

    return value



def _call(obj, name):
    fn = getattr(obj, name, None)
    try:
        return fn() if callable(fn) else fn
    except Exception:
        return None


def _ist_label(epoch):
    if epoch is None:
        return None
    return datetime.fromtimestamp(float(epoch), IST).isoformat(timespec="seconds")


def _vid_label(sec):
    sec = max(0.0, float(sec))
    return f"Video +{int(sec // 3600):02d}:{int((sec % 3600) // 60):02d}:{int(sec % 60):02d}"


def slot_records_for(rt):
    """Per-camera slot table including FREE slots, with plate/colour/type resolved."""
    pipeline = rt.pipeline
    slots = getattr(pipeline, "slots", None) if pipeline else None
    states = slots.slot_states() if slots is not None and hasattr(slots, "slot_states") else []
    occupants = getattr(slots, "occupants", {}) if slots is not None else {}
    start_ts = getattr(slots, "stream_start_ts", None) if slots is not None else None
    out = []
    occupancy_unavailable = getattr(rt, "occupancy_unavailable", False)
    for state in states:
        rec = dict(state)
        if occupancy_unavailable:
            rec.update(state="UNKNOWN", track_id=None, vehicle_uid=None, occupancy_source=None)
        sid = state.get("slot_id")
        occ = None if occupancy_unavailable else occupants.get(sid)
        vehicle = getattr(occ, "vehicle", None) if occ else None
        baseline = bool(getattr(occ, "baseline", False)) if occ else False
        rec.update({
            "camera_id": rt.cfg["camera_id"],
            "plate": None, "plate_status": "UNKNOWN",
            "vehicle_type": None, "colour": None, "baseline": baseline,
        })
        parked_offset = None
        if occ and not baseline and getattr(occ, "parked_at", None) is not None and start_ts is not None:
            parked_offset = max(0.0, float(occ.parked_at) - float(start_ts))
        rec["parked_at"] = parked_offset
        rec["recording_started_at_ist"] = _ist_label(start_ts)
        rec["parked_at_epoch"] = (
            float(occ.parked_at) if occ and not baseline
            and getattr(occ, "parked_at", None) is not None else None
        )
        rec["parked_at_label"] = (
            "Before video start" if (occ and baseline)
            else "Occupied (entry unverified)"
            if rec.get("occupancy_source") == "OBSERVED_OCCUPANCY"
            else (_vid_label(parked_offset) if parked_offset is not None else None)
        )
        rec["parked_at_ist"] = (
            "Present at recording start" if (occ and baseline)
            else (_ist_label(occ.parked_at) if occ and not baseline
                  and getattr(occ, "parked_at", None) is not None else None)
        )
        if vehicle is not None:
            plate = getattr(vehicle, "emitted_plate", None)
            confirmed = False
            try:
                voter = getattr(vehicle, "voter", None)
                consensus = voter.consensus() if voter else None
                if not plate and consensus:
                    plate = getattr(consensus, "text", None)
                confirmed = bool(
                    getattr(vehicle, "plate_final", False) and consensus
                    and getattr(consensus, "valid", False) and plate
                    and str(plate).strip() == str(consensus.text).strip()
                )
            except Exception:
                pass
            rec["plate"] = str(plate).strip() if plate else None
            rec["plate_status"] = "CONFIRMED" if confirmed else ("UNCONFIRMED" if plate else "UNKNOWN")
            rec["vehicle_type"] = _call(vehicle, "vehicle_type")      # FIX: was a bound method
            rec["colour"] = _call(vehicle, "color")
            rec["vehicle_uid"] = getattr(vehicle, "uid", rec.get("vehicle_uid"))
            rec["track_id"] = getattr(occ, "track_id", rec.get("track_id"))
        out.append(rec)
    return out


def build_vehicles(runtimes, slot_table, reid_info=None):
    """Build de-duplicated physical-vehicle records from local track evidence.

    A camera-local Vehicle UID is more stable than a tracker ID: ByteTrack can
    fragment one car into multiple local tracks and the registry can rebind
    those tracks to the same Vehicle object. Group those observations first.
    Cross-camera grouping is allowed only for a multi-camera OSNet identity or
    an identical *confirmed* registration plate. Histogram fallback is not
    strong enough to merge two vehicles by appearance alone.

    Slot occupancy is taken exclusively from the authoritative per-camera slot
    table. Multiple historical track IDs therefore cannot inflate the parked
    vehicle count or make one slot appear to have several occupants.
    """
    slot_by_uid = {}
    for cam, recs in slot_table.items():
        for r in recs:
            if r.get("state") == "OCCUPIED" and r.get("vehicle_uid"):
                slot_by_uid[(str(cam), str(r["vehicle_uid"]))] = r

    local = {}
    for cam, rt in runtimes.items():
        for obs in rt.track_records.values():
            if not obs.get("track_confirmed"):
                continue
            obs_cam = str(obs.get("camera_id") or cam)
            uid = obs.get("vehicle_uid")
            track_id = obs.get("local_track_id")
            local_key = (f"UID:{obs_cam}:{uid}" if uid else
                         f"TRACK:{obs_cam}:{track_id}")
            v = local.setdefault(local_key, {
                "_local_key": local_key,
                "_global_ids": set(),
                "_confirmed_plates": set(),
                "_vehicle_uids": set(),
                "_slot_assignments": {},
                "vehicle_id": local_key,
                "global_id": None,
                "id_scope": "local",
                "vehicle_uid": str(uid) if uid else None,
                "number_plate": None,
                "plate_status": "UNKNOWN",
                "ocr_confidence": None,
                "ocr_consensus_reads": 0,
                "vehicle_type": None,
                "colour": None,
                "cameras": [],
                "camera_observations": [],
                "slot_id": None,
                "state": "SEEN",
                "baseline": False,
                "parked_at_label": None,
            })

            gid = obs.get("global_id")
            if gid:
                v["_global_ids"].add(str(gid))
            if uid:
                v["_vehicle_uids"].add((obs_cam, str(uid)))
            if obs_cam not in v["cameras"]:
                v["cameras"].append(obs_cam)
            observation = {
                "camera_id": obs_cam,
                "local_track_id": track_id,
                "first_seen_video_seconds": obs.get("first_seen_video_seconds"),
                "last_seen_video_seconds": obs.get("last_seen_video_seconds"),
                "first_seen_source_clock_seconds": obs.get("first_seen_source_clock_seconds"),
                "last_seen_source_clock_seconds": obs.get("last_seen_source_clock_seconds"),
            }
            if observation not in v["camera_observations"]:
                v["camera_observations"].append(observation)

            for field in ("vehicle_type", "colour"):
                if obs.get(field) is not None:
                    v[field] = obs[field]

            plate = str(obs.get("number_plate") or "").strip().upper()
            status = str(obs.get("plate_status") or "UNKNOWN").upper()
            confidence = obs.get("ocr_confidence")
            reads = int(obs.get("ocr_consensus_reads") or 0)
            if plate and status == "CONFIRMED":
                v["_confirmed_plates"].add(plate)
            rank = {"UNKNOWN": 0, "CANDIDATE": 1, "UNCONFIRMED": 1, "CONFIRMED": 2}.get(status, 0)
            old_rank = {"UNKNOWN": 0, "CANDIDATE": 1, "UNCONFIRMED": 1, "CONFIRMED": 2}.get(v["plate_status"], 0)
            old_conf = v.get("ocr_confidence")
            better_conf = confidence is not None and (old_conf is None or float(confidence) > float(old_conf))
            if plate and (rank > old_rank or (rank == old_rank and better_conf)):
                v["number_plate"] = plate
                v["plate_status"] = status
                v["ocr_confidence"] = confidence
                v["ocr_consensus_reads"] = reads
            elif plate and rank == old_rank and reads > v.get("ocr_consensus_reads", 0):
                v["number_plate"] = plate
                v["plate_status"] = status
                v["ocr_confidence"] = confidence
                v["ocr_consensus_reads"] = reads

            srec = slot_by_uid.get((obs_cam, str(uid))) if uid else None
            if srec:
                v["_slot_assignments"][(obs_cam, str(srec.get("slot_id")))] = srec

    # Union-find for only defensible cross-camera identity links.
    keys = list(local)
    parent = {k: k for k in keys}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    members = {key: {key} for key in keys}
    evidence = {key: set() for key in keys}
    ambiguous = set()

    def associate(group, reason):
        roots = sorted({find(key) for key in group})
        if len(roots) < 2:
            return
        candidates = set().union(*(members[root] for root in roots))
        cameras = [local[key]["cameras"][0] for key in candidates]
        plates = set().union(*(local[key]["_confirmed_plates"] for key in candidates))
        # Evaluate the whole candidate group before merging. An intermediate
        # match must never bridge conflicting plates or same-camera identities.
        if len(cameras) != len(set(cameras)) or len(plates) > 1:
            ambiguous.update(candidates)
            return
        canonical = roots[0]
        for root in roots[1:]:
            parent[root] = canonical
            members[canonical].update(members[root])
            evidence[canonical].update(evidence[root])
        evidence[canonical].add(reason)

    plate_groups = {}
    gid_groups = {}
    for key, v in local.items():
        for plate in v["_confirmed_plates"]:
            plate_groups.setdefault(plate, []).append(key)
        for gid in v["_global_ids"]:
            gid_groups.setdefault(gid, []).append(key)

    # Confirmed plate evidence takes precedence over appearance.
    for group in plate_groups.values():
        associate(group, "confirmed_plate")

    backend = str((reid_info or {}).get("backend") or "").lower()
    if "osnet" in backend:
        for group in gid_groups.values():
            associate(group, "osnet_appearance")

    merged = {}
    for key, v in local.items():
        root = find(key)
        out = merged.setdefault(root, {
            "_keys": [], "_global_ids": set(), "_confirmed_plates": set(),
            "_vehicle_uids": set(), "_slot_assignments": {},
            "vehicle_id": root, "global_id": None,
            "id_scope": "local", "vehicle_uid": None, "local_vehicle_uids": [], "number_plate": None,
            "plate_status": "UNKNOWN", "ocr_confidence": None,
            "ocr_consensus_reads": 0, "vehicle_type": None, "colour": None,
            "cameras": [], "camera_observations": [], "slot_id": None,
            "slot_assignments": [], "state": "SEEN", "baseline": False,
            "parked_at_label": None,
        })
        out["_keys"].append(key)
        out["_global_ids"].update(v["_global_ids"])
        out["_confirmed_plates"].update(v["_confirmed_plates"])
        out["_vehicle_uids"].update(v["_vehicle_uids"])
        out["_slot_assignments"].update(v["_slot_assignments"])
        if v.get("vehicle_uid") and not out["vehicle_uid"]:
            out["vehicle_uid"] = v["vehicle_uid"]
        for field in ("vehicle_type", "colour"):
            if v.get(field) is not None:
                out[field] = v[field]
        if v.get("number_plate"):
            rank = {"UNKNOWN": 0, "CANDIDATE": 1, "UNCONFIRMED": 1, "CONFIRMED": 2}
            if rank.get(v["plate_status"], 0) > rank.get(out["plate_status"], 0) or out["number_plate"] is None:
                out["number_plate"] = v["number_plate"]
                out["plate_status"] = v["plate_status"]
                out["ocr_confidence"] = v.get("ocr_confidence")
                out["ocr_consensus_reads"] = v.get("ocr_consensus_reads", 0)
        for camera in v["cameras"]:
            if camera not in out["cameras"]:
                out["cameras"].append(camera)
        out["camera_observations"].extend(v["camera_observations"])

    vehicles = []
    for v in merged.values():
        gids = sorted(v.pop("_global_ids"))
        confirmed_plates = sorted(v.pop("_confirmed_plates"))
        local_vehicle_uids = sorted(v.pop("_vehicle_uids"))
        v["local_vehicle_uids"] = [
            {"camera_id": cam, "vehicle_uid": uid}
            for cam, uid in local_vehicle_uids
        ]
        v["vehicle_uid"] = local_vehicle_uids[0][1] if len(local_vehicle_uids) == 1 else None
        local_keys = sorted(v.pop("_keys", []))
        v["identity_evidence"] = sorted(evidence[find(local_keys[0])])
        v["cross_camera_identity"] = (
            "matched" if len(v["cameras"]) > 1 else
            "ambiguous" if any(key in ambiguous for key in local_keys) else "unmatched"
        )
        slot_map = v.pop("_slot_assignments")
        assignments = list(slot_map.values())
        v["slot_assignments"] = [
            {"camera_id": r.get("camera_id"), "slot_id": r.get("slot_id"),
             "baseline": bool(r.get("baseline")), "parked_at_label": r.get("parked_at_label")}
            for r in assignments
        ]
        if len(confirmed_plates) > 1:
            v["number_plate"] = None
            v["plate_status"] = "CONFLICT"
            v["identity_conflict"] = "conflicting_confirmed_plates"
            v["state"] = "IDENTITY_CONFLICT"
        if len(assignments) == 1 and v["state"] != "IDENTITY_CONFLICT":
            slot = assignments[0]
            v.update({
                "slot_id": slot.get("slot_id"), "state": "PARKED",
                "baseline": bool(slot.get("baseline")),
                "parked_at_label": slot.get("parked_at_label"),
                "slot_camera": slot.get("camera_id"),
            })
        elif len(assignments) > 1:
            # One physical vehicle cannot occupy multiple bays at once. Keep
            # every camera's slot record intact and expose the identity conflict.
            v["state"] = "IDENTITY_CONFLICT"
            v["slot_id"] = None
            v["identity_conflict"] = "multiple_active_slots_for_one_identity"
        v["reid_ids"] = gids
        if len(gids) == 1:
            v["global_id"] = gids[0]
            v["id_scope"] = "global"
            v["vehicle_id"] = gids[0]
        elif len(gids) > 1:
            v["global_id"] = None
            v["id_scope"] = "local_merged_by_evidence"
            v["vehicle_id"] = "MERGED:" + "+".join(gids)
        elif v.get("_local_key"):
            v["vehicle_id"] = v["_local_key"]
        else:
            v["vehicle_id"] = "LOCAL_GROUP:" + "+".join(local_keys)
        # The temporary root key is not part of the public schema.
        v.pop("_local_key", None)
        vehicles.append(v)

    # A confirmed plate must not silently identify two separate physical
    # vehicle records. Mark both records as conflicted instead of counting
    # either as a trusted plate match.
    plate_to_records = {}
    for index, vehicle in enumerate(vehicles):
        plate = vehicle.get("number_plate")
        if plate and vehicle.get("plate_status") == "CONFIRMED":
            plate_to_records.setdefault(str(plate).strip().upper(), []).append(index)
    for plate, indices in plate_to_records.items():
        if len(indices) > 1:
            for index in indices:
                vehicles[index]["plate_status"] = "CONFLICT"
                vehicles[index]["identity_conflict"] = "duplicate_confirmed_plate"
    return vehicles




def _build_exit_vehicle_details(raw_events, plate_evidence=None):
    """Build complete departed-vehicle records from matching lifecycle history.

    Matching is strictly by camera + vehicle UID. Slot information is taken
    from actual lifecycle events. Missing later attributes never overwrite
    previously observed values.
    """
    from datetime import datetime

    def first_value(sources, *names):
        for source in sources:
            if not isinstance(source, dict):
                continue
            for name in names:
                value = source.get(name)
                if value is not None and value != "":
                    return value
        return None

    def event_slot(event):
        return (
            first_value([event], "slot_id", "bay_id")
            or first_value([event.get("parking") or {}], "slot_id", "id")
            or first_value([event.get("bay") or {}], "id", "slot_id")
        )

    def event_sources(event):
        return [
            event,
            event.get("vehicle") or {},
            event.get("vehicle_details") or {},
        ]

    def parse_time(value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    result = []
    seen_event_ids = set()
    events = [e for e in (raw_events or []) if isinstance(e, dict)]

    for exit_event in events:
        if str(exit_event.get("event_type") or "").upper() != "EXIT":
            continue

        exit_sources = event_sources(exit_event)
        camera = first_value(exit_sources, "camera_id", "camera")
        uid = first_value(exit_sources, "vehicle_uid", "uid")
        slot = event_slot(exit_event)

        if uid is None:
            continue

        matching = []
        for event in events:
            sources = event_sources(event)
            if str(first_value(sources, "camera_id", "camera") or "") != str(camera or ""):
                continue
            if str(first_value(sources, "vehicle_uid", "uid") or "") != str(uid):
                continue

            candidate_slot = event_slot(event)
            if slot and candidate_slot and str(candidate_slot) != str(slot):
                continue

            matching.append(event)

        event_id = first_value(exit_sources, "event_id", "id")
        if event_id is not None:
            event_id = str(event_id)
            if event_id in seen_event_ids:
                continue
            seen_event_ids.add(event_id)

        # Gather only actual observed values. Later nulls do not erase earlier data.
        def observed(*names):
            for history_event in reversed(matching):
                value = first_value(event_sources(history_event), *names)
                if value is not None and value != "":
                    return value
            return None

        vehicle_type = observed("vehicle_type", "type", "class_name")
        colour = observed("colour", "color")
        type_confidence = observed("type_confidence")
        colour_confidence = observed("colour_confidence")

        plate_confidences = []
        for history_event in matching:
            for src in event_sources(history_event):
                for key in ("plate_confidence", "ocr_confidence"):
                    value = src.get(key)
                    if isinstance(value, (int, float)):
                        plate_confidences.append(float(value))
                plate_obj = src.get("plate")
                if isinstance(plate_obj, dict):
                    value = plate_obj.get("confidence")
                    if isinstance(value, (int, float)):
                        plate_confidences.append(float(value))

        evidence_matches = [
            item for item in (plate_evidence or [])
            if isinstance(item, dict)
            and str(item.get("camera_id") or "") == str(camera or "")
            and str(item.get("vehicle_uid") or "") == str(uid)
            and item.get("plate_text_ciphertext")
            and item.get("plate_text_hmac")
        ]

        evidence = evidence_matches[0] if len(evidence_matches) == 1 else None

        history = []
        track_ids = []
        for history_event in matching:
            sources = event_sources(history_event)
            track = first_value(sources, "track_id", "local_track_id")
            if track is not None and str(track) not in {str(x) for x in track_ids}:
                track_ids.append(track)

            snapshot = history_event.get("snapshot")
            if not isinstance(snapshot, dict):
                snapshot = {}

            history.append({
                "event_type": str(history_event.get("event_type") or "").upper(),
                "event_id": first_value(sources, "event_id", "id"),
                "track_id": track,
                "timestamp": first_value(
                    sources, "event_time", "timestamp", "captured_at"
                ),
                "detected_at": first_value(sources, "detected_at"),
                "direction": first_value(sources, "direction"),
                "inferred": bool(first_value(sources, "inferred") or False),
                "snapshot": {
                    "vehicle": snapshot.get("vehicle"),
                    "plate_available": bool(snapshot.get("plate")),
                },
            })

        def event_time_for(kind):
            for history_event in matching:
                if str(history_event.get("event_type") or "").upper() == kind:
                    return first_value(
                        event_sources(history_event),
                        "event_time", "timestamp", "captured_at"
                    )
            return None

        park_start = event_time_for("PARK_START")
        park_end = event_time_for("PARK_END")
        entry_time = event_time_for("ENTRY")
        exit_time = first_value(
            exit_sources, "event_time", "timestamp", "captured_at"
        )

        duration_seconds = None
        start_dt = parse_time(park_start)
        end_dt = parse_time(park_end)
        if start_dt and end_dt:
            duration_seconds = max(0.0, (end_dt - start_dt).total_seconds())

        plate_status = str(
            (evidence or {}).get("plate_status")
            or first_value(exit_sources, "plate_status", "status")
            or "UNKNOWN"
        ).upper()

        record = {
            "event_type": "EXIT",
            "event_id": event_id,
            "camera_id": camera,
            "parking_area_id": first_value(exit_sources, "parking_area_id"),
            "vehicle_uid": uid,
            "track_id": first_value(exit_sources, "track_id", "local_track_id"),
            "track_ids_observed": track_ids,
            "vehicle_type": vehicle_type,
            "type_confidence": type_confidence,
            "colour": colour,
            "colour_confidence": colour_confidence,
            "number_plate": None,
            "number_plate_ciphertext": (
                evidence.get("plate_text_ciphertext") if evidence else None
            ),
            "number_plate_hmac": (
                evidence.get("plate_text_hmac") if evidence else None
            ),
            "plate_status": plate_status,
            "plate_confidence": max(plate_confidences) if plate_confidences else None,
            "event_time": exit_time,
            "captured_at": first_value(exit_sources, "captured_at"),
            "detected_at": first_value(exit_sources, "detected_at"),
            "direction": first_value(exit_sources, "direction"),
            "inferred": bool(first_value(exit_sources, "inferred") or False),
            "slot_id": slot or event_slot(exit_event),
            "entry_time": entry_time,
            "park_start_time": park_start,
            "park_end_time": park_end,
            "parking_duration_seconds": duration_seconds,
            "lifecycle_history": history,
        }

        if uid is not None or record["track_id"] is not None:
            result.append(record)

    return result


def build_final_report(runtimes, handoff, global_reid, reid_info, cfg,
                       no_publisher, batch_stats, plate_engine_error=False,
                       determinism=None, plate_evidence=None):
    # The asynchronous ReID worker may resolve a crop after the last video
    # frame was processed. Reconcile its final local->global assignments before
    # building the vehicle table, so EOF timing cannot silently erase IDs.
    gid_lookup = getattr(global_reid, "gid", None)
    if callable(gid_lookup):
        for cam, rt in runtimes.items():
            for obs in rt.track_records.values():
                if not obs.get("global_id"):
                    resolved = gid_lookup(obs.get("camera_id", cam), obs.get("local_track_id"))
                    if resolved:
                        obs["global_id"] = resolved

    slot_table = {cam: slot_records_for(rt) for cam, rt in runtimes.items()}
    vehicles = build_vehicles(runtimes, slot_table, reid_info=reid_info)
    identities = global_reid.summary()
    multi = [i for i in identities if len(i.get("cameras", [])) > 1]

    s2_events, raw_events = [], []
    for rt in runtimes.values():
        pub = rt.publisher
        s2_events += list(getattr(pub, "s2_events", []) or [])
        raw_events += list(getattr(pub, "raw_events", []) or [])
    _rank = {"ENTRY": 0, "PARK_START": 1, "PARK_END": 2, "EXIT": 3}
    s2_events.sort(key=lambda e: (str(e.get("captured_at") or e.get("timestamp") or ""),
                                  str(e.get("camera_id") or ""), _rank.get(e.get("event_type"), 9)))
    raw_events.sort(key=lambda e: (str(e.get("event_time") or e.get("timestamp") or ""),
                                   str(e.get("camera_id") or ""), _rank.get(e.get("event_type"), 9)))

    # Count actual PARK_END lifecycle records, deduplicating repeated event IDs.
    park_end_event_ids = set()
    park_end_event_count = 0
    for event in raw_events:
        if str(event.get("event_type") or "").upper() != "PARK_END":
            continue
        event_id = event.get("event_id")
        if event_id is not None:
            event_id = str(event_id)
            if event_id in park_end_event_ids:
                continue
            park_end_event_ids.add(event_id)
        park_end_event_count += 1
    from vehicle_intelligence.lifecycle_timeline import build_timeline
    timeline = build_timeline(raw_events)
    try:
        from vehicle_intelligence.obs import get_ledger
        ledger = get_ledger()
        error_health = ledger.snapshot() if ledger is not None else {"note": "logging ledger not active"}
    except Exception:
        error_health = {"note": "error ledger unavailable"}

    cams = {}
    for cam, rt in runtimes.items():
        recs = slot_table[cam]
        cams[cam] = {
            "frames_seen": rt.frames_seen,
            "processed_frames": rt.processed_frames,
            "dropped_frames": rt.dropped_frames + int(getattr(rt.source, "dropped_frames", 0)),
            "skipped_idle_frames": rt.skipped_idle,
            "motion_roi": rt.motion_roi_info,
            "slots_total": len(recs),
            "occupied_slots": sum(1 for r in recs if r.get("state") == "OCCUPIED"),
            "free_slots": sum(1 for r in recs if r.get("state") == "FREE"),
            "unknown_slots": sum(1 for r in recs if r.get("state") == "UNKNOWN"),
            "slots": recs,
            "event_counts": dict(rt.event_counts),
            "recent_events": list(rt.event_history[-20:]),
        }

    types = {e.get("event_type") for e in s2_events}
    occupied_total = sum(c["occupied_slots"] for c in cams.values())
    free_total = sum(c["free_slots"] for c in cams.values())
    parked_total = sum(1 for v in vehicles if v["state"] == "PARKED")
    identity_conflicts = sum(1 for v in vehicles if v["state"] == "IDENTITY_CONFLICT")
    slot_integrity_ok = all(
        len({r.get("slot_id") for r in recs}) == len(recs)
        and all(r.get("state") in {"OCCUPIED", "FREE", "UNKNOWN"} for r in recs)
        for recs in slot_table.values()
    )
    plate_conflicts = sum(1 for v in vehicles if v.get("plate_status") == "CONFLICT")
    camera_count = len(runtimes)
    configured_batch = max(1, int((cfg.get("inference", {}) or {}).get("batch_size", 1)))
    max_possible_batch = min(configured_batch, camera_count) if camera_count else 0
    batch_calls = int(batch_stats.get("calls", 0))
    batch_frames = int(batch_stats.get("frames", 0))
    average_batch = batch_frames / batch_calls if batch_calls else 0.0
    batch_health = dict(batch_stats)
    batch_health.update({
        "configured_batch_size": configured_batch,
        "max_possible_batch_size_for_this_run": max_possible_batch,
        "average_batch_size": round(average_batch, 3),
        "batch_fill_ratio": round(average_batch / max_possible_batch, 3) if max_possible_batch else 0.0,
    })
    confirmed_plate_links = any(
        len(v.get("cameras", [])) > 1 and v.get("plate_status") == "CONFIRMED"
        for v in vehicles
    )
    health = {
        "reid_backend": reid_info.get("backend"),
        "reid_threshold": reid_info.get("threshold"),
        "plate_engine": ("FAILED_TO_START" if plate_engine_error else
                         ("ON" if any(rt.plate_worker for rt in runtimes.values()) else "OFF")),
        "yolo_batches": batch_health,
        "motion_roi": {c: r.motion_roi_info for c, r in runtimes.items()},
        "logging": error_health,
    }
    report = {
        "report_version": 3,
        "generated_at": datetime.now(IST).isoformat(),
        "run_status": "COMPLETED",
        "processing_mode": "MULTI_CAMERA_SHARED_GPU_BATCHING",
        "service2_publishing": bool(not no_publisher),
        "timestamp_note": (
            "All times are IST. Live cameras: laptop/server clock at frame decode. "
            "File replay: laptop clock captured once at run start + video offset "
            "(determinism.timestamp_source=laptop), so relative timings are identical "
            "on every run of the same video; only the absolute start differs."
            if (determinism or {}).get("timestamp_source") == "laptop" else
            "Replay times are video-relative / replay_start_time based; they are not "
            "original CCTV wall-clock times."),
        "run_info": {"determinism": determinism or {"enabled": False, "reason": "not_provided"}},
        "pipeline_health": health,
        "summary": {
            "cameras": len(runtimes),
            "vehicles_seen": len(vehicles),
            "vehicles_parked": sum(1 for v in vehicles if v["state"] == "PARKED"),
            "vehicle_identity_conflicts": identity_conflicts,
            "plates_confirmed": sum(1 for v in vehicles if v["plate_status"] == "CONFIRMED"),
            "plate_conflicts": plate_conflicts,
            "slots_occupied": occupied_total,
            "slots_free": free_total,
            "slots_unknown": sum(c["unknown_slots"] for c in cams.values()),
            "service2_entry_events": sum(1 for e in s2_events if e.get("event_type") == "ENTRY"),
            "service2_exit_events": sum(1 for e in s2_events if e.get("event_type") == "EXIT"),
            "park_end_events": park_end_event_count,
            "exit_tripwire_vehicle_records": len(_build_exit_vehicle_details(raw_events, plate_evidence)),
            "camera_transfers": len(handoff.transfers) if handoff else 0,
            "parked_vehicle_slot_difference": parked_total - occupied_total,
        },
        "vehicles": vehicles,
        "service2_events": s2_events,
        "lifecycle_events": raw_events,
        "generated_lifecycle_counts": {
            kind: sum(event.get("event_type") == kind and event.get("observation_origin") != "restored"
                      for event in raw_events)
            for kind in ("ENTRY", "PARK_START", "PARK_END", "EXIT")
        },
        "restored_lifecycle_counts": {
            kind: sum(event.get("event_type") == kind and event.get("observation_origin") == "restored"
                      for event in raw_events)
            for kind in ("ENTRY", "PARK_START", "PARK_END", "EXIT")
        },
        "exit_vehicle_details": _build_exit_vehicle_details(raw_events, plate_evidence),
        "lifecycle_timeline": timeline,
        "plate_evidence": plate_evidence or [],
        "camera_transfers": list(handoff.transfers) if handoff else [],
        "global_reid": {
            "backend": reid_info.get("backend"), "model": reid_info.get("model"),
            "similarity_threshold": reid_info.get("threshold"), "identities": identities,
        },
        "cameras": cams,
        "track_observations": [o for rt in runtimes.values() for o in rt.track_records.values()],
        "validation": {
            "local_tracking": "TRACKS_OBSERVED" if any(rt.track_records for rt in runtimes.values()) else "NO_TRACKS",
            "vehicle_records": "BUILT" if vehicles else "NO_VEHICLES",
            "cross_camera_reidentification": (
                "OSNET_IDENTITY_OBSERVED" if (str(reid_info.get("backend", "")).lower() == "osnet" and multi)
                else ("CONFIRMED_PLATE_LINK_OBSERVED" if confirmed_plate_links else "NOT_DEMONSTRATED")
            ),
            "plate_detection_ocr": (
                "CONFIRMED_READS" if any(v.get("plate_status") == "CONFIRMED" for v in vehicles)
                else ("CANDIDATES_ONLY" if any(v.get("number_plate") for v in vehicles) else "NO_PLATE_READS")
            ),
            "vehicle_attributes": (
                "COMPLETE_FOR_PARKED_VEHICLES" if parked_total and all(
                    v.get("vehicle_type") and v.get("colour")
                    for v in vehicles if v.get("state") == "PARKED"
                ) else ("PARTIAL" if any(v.get("vehicle_type") or v.get("colour") for v in vehicles) else "NO_ATTRIBUTES")
            ),
            "multi_camera_processing": (
                f"OBSERVED_{camera_count}_CAMERAS" if camera_count >= 2 and all(x.processed_frames > 0 for x in runtimes.values())
                else "PARTIAL"
            ),
            "camera_scale_validation": "NOT_TESTED_10_20_30_CAMERA_LOAD",
            "slot_integrity": "PASS" if slot_integrity_ok else "FAIL",
            "vehicle_slot_consistency": "PASS" if parked_total == occupied_total and identity_conflicts == 0 else "MISMATCH",
            "confirmed_plate_integrity": "PASS" if plate_conflicts == 0 else "CONFLICT",
            "service2_entry_emitted": "EMITTED" if "ENTRY" in types else "NONE",
            "service2_exit_emitted": "EMITTED" if "EXIT" in types else "NOT_DEMONSTRATED",
            "lifecycle_park_start": "OBSERVED" if any(r["park_start_time_ist"] for r in timeline) else "NOT_DEMONSTRATED",
            "lifecycle_park_end": "OBSERVED" if any(r["park_end_time_ist"] for r in timeline) else "NOT_DEMONSTRATED",
            "lifecycle_exit": (
                "OBSERVED_TRIPWIRE" if any(r["exit_time_ist"] and not r["exit_inferred"] for r in timeline)
                else ("OBSERVED_INFERRED_ONLY" if any(r["exit_time_ist"] for r in timeline) else "NOT_DEMONSTRATED")),
        },
    }
    # Protect report-facing plate data after all internal matching is complete.
    # Service 2 payloads are already handled by S2Mapper and remain unchanged.
    # Protect every exported report section, including nested Service 2
    # payloads, evidence metadata, camera transfers and track observations.
    report = _protect_plate_data(report)

    # Add masked display fields only after plaintext plate protection.
    # Ciphertext and HMAC remain the authoritative protected values.
    for vehicle in report.get("vehicles", []):
        ciphertext = vehicle.get("number_plate_ciphertext")
        status = vehicle.get("plate_status")
        if ciphertext and str(status).upper() in {
            "CONFIRMED", "CANDIDATE", "UNCONFIRMED"
        }:
            vehicle["number_plate_display"] = _display_masked_plate(
                ciphertext, status
            )

    for exit_vehicle in report.get("exit_vehicle_details", []):
        ciphertext = exit_vehicle.get("number_plate_ciphertext")
        status = exit_vehicle.get("plate_status")
        exit_vehicle["number_plate_display"] = _display_masked_plate(
            ciphertext,
            status,
            expected_hmac=exit_vehicle.get("number_plate_hmac"),
            require_hmac=True,
        )

    for camera in (report.get("cameras") or {}).values():
        for slot in camera.get("slots") or []:
            ciphertext = slot.get("plate_ciphertext")
            status = slot.get("plate_status")
            if ciphertext and str(status).upper() in {
                "CONFIRMED", "CANDIDATE", "UNCONFIRMED"
            }:
                slot["plate_display"] = _display_masked_plate(
                    ciphertext, status
                )

    # Build one complete, data-driven record for every configured slot.
    # Departed vehicles remain visible in history even when the slot is FREE.
    slot_details = []

    def _report_event_slot(event):
        parking = event.get("parking") or {}
        bay = event.get("bay") or {}
        return (
            event.get("slot_id")
            or event.get("bay_id")
            or parking.get("slot_id")
            or parking.get("id")
            or bay.get("id")
            or bay.get("slot_id")
        )

    for camera_id, camera_data in (report.get("cameras") or {}).items():
        for slot in camera_data.get("slots") or []:
            slot_id = slot.get("slot_id") or slot.get("bay_id")
            if not slot_id:
                continue

            matching_lifecycle = [
                event for event in (report.get("lifecycle_events") or [])
                if str(event.get("camera_id") or "") == str(camera_id)
                and str(_report_event_slot(event) or "") == str(slot_id)
            ]

            matching_exits = [
                item for item in (report.get("exit_vehicle_details") or [])
                if str(item.get("camera_id") or "") == str(camera_id)
                and str(item.get("slot_id") or "") == str(slot_id)
            ]

            current_vehicle = None
            if str(slot.get("state") or "").upper() == "OCCUPIED":
                current_vehicle = {
                    "vehicle_uid": slot.get("vehicle_uid"),
                    "track_id": slot.get("track_id"),
                    "vehicle_type": slot.get("vehicle_type"),
                    "colour": slot.get("colour"),
                    "plate_display": slot.get("plate_display"),
                    "plate_status": slot.get("plate_status"),
                    "occupancy_source": slot.get("occupancy_source"),
                    "baseline": slot.get("baseline"),
                    "parked_at": slot.get("parked_at"),
                    "parked_at_epoch": slot.get("parked_at_epoch"),
                    "parked_at_ist": slot.get("parked_at_ist"),
                }

            slot_details.append({
                "camera_id": camera_id,
                "slot_id": slot_id,
                "level": slot.get("level"),
                "state": slot.get("state"),
                "recording_started_at_ist": slot.get("recording_started_at_ist"),
                "current_vehicle": current_vehicle,
                "vehicle_history": matching_exits,
                "lifecycle_history": matching_lifecycle,
            })

    report["slot_details"] = slot_details
    report["slot_details_summary"] = {
        "total_slots": len(slot_details),
        "occupied_slots": sum(
            1 for item in slot_details
            if str(item.get("state") or "").upper() == "OCCUPIED"
        ),
        "free_slots": sum(
            1 for item in slot_details
            if str(item.get("state") or "").upper() == "FREE"
        ),
        "unknown_slots": sum(1 for item in slot_details if item.get("state") == "UNKNOWN"),
        "slots_with_departed_vehicle_history": sum(
            1 for item in slot_details if item.get("vehicle_history")
        ),
    }

    return report


def write_report(report, path):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=d, delete=False) as stream:
            temp_path = stream.name
            json.dump(report, stream, indent=2, ensure_ascii=False, default=str)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
    return path



def _display_masked_plate(ciphertext, status, expected_hmac=None, require_hmac=False):
    """Display a masked plate candidate without exposing its full value."""
    status = str(status).upper()
    if status not in {"CONFIRMED", "CANDIDATE", "UNCONFIRMED"} or not ciphertext:
        return "NO-PLATE"
    try:
        plate = str(decrypt_text(ciphertext)).strip().upper().replace(" ", "")
        if require_hmac and not expected_hmac:
            return "UNVERIFIED"
        if expected_hmac:
            actual_hmac = plate_hmac(plate)
            if not hmac.compare_digest(
                str(actual_hmac), str(expected_hmac)
            ):
                return "UNVERIFIED"
        if len(plate) <= 4:
            return "*" * len(plate)
        if status != "CONFIRMED":
            return plate[:2] + "*" * (len(plate) - 4) + plate[-2:]
        if len(plate) <= 6:
            return plate[:1] + "*" * (len(plate) - 2) + plate[-1:]
        return plate[:4] + "*" * (len(plate) - 6) + plate[-2:]
    except Exception:
        return "ENCRYPTED"


def print_summary(report):
    line = "=" * 78
    print("\n" + line)
    print("SMART PARKING - FINAL MULTI-CAMERA REPORT")
    print(line)
    h = report["pipeline_health"]
    print(f"ReID: {h['reid_backend']} | Plate engine: {h['plate_engine']} | "
          f"YOLO batches: {h['yolo_batches'].get('calls', 0)} "
          f"(max size {h['yolo_batches'].get('max_size', 0)})")
    for cam, c in report["cameras"].items():
        roi = (c.get("motion_roi") or {}).get("area_frac")
        print(f"\n{cam}: seen={c['frames_seen']} processed={c['processed_frames']} "
              f"dropped={c['dropped_frames']} idle_skipped={c['skipped_idle_frames']} "
              f"motion_roi={'%.0f%% of frame' % (roi * 100) if roi else 'whole frame'}")
        print(f"  slots: {c['occupied_slots']} occupied / {c['free_slots']} free")
        for r in c["slots"]:
            slot_id = r.get("slot_id")
            detail = next(
                (
                    item for item in (report.get("slot_details") or [])
                    if item.get("camera_id") == cam
                    and item.get("slot_id") == slot_id
                ),
                {},
            )

            current = detail.get("current_vehicle") or {}
            history = detail.get("vehicle_history") or []

            if current:
                display_plate = current.get("plate_display") or "NO-PLATE"
                plate_status = current.get("plate_status") or "UNKNOWN"
                colour = current.get("colour") or "-"
                label = (
                    current.get("parked_at_ist")
                    or current.get("parked_at")
                    or "Before video start"
                )
            elif history:
                previous = history[-1]
                display_plate = previous.get("number_plate_display") or "NO-PLATE"
                plate_status = previous.get("plate_status") or "UNKNOWN"
                colour = previous.get("colour") or "-"
                exit_time = previous.get("event_time") or "-"
                label = f"Exited: {exit_time}"
            else:
                display_plate = "NO-PLATE"
                plate_status = "UNKNOWN"
                colour = "-"
                label = "FREE"

            print(
                f"   {slot_id:>6} | {display_plate:<12} "
                f"({plate_status}) | {colour:<8} | {label}"
            )
    print("\nVehicles:")
    if not report["vehicles"]:
        print("  none confirmed")
    for v in report["vehicles"]:
        display_plate = _display_masked_plate(
            v.get("number_plate_ciphertext"), v.get("plate_status")
        )
        print(f"  {v['vehicle_id']:<18} plate={display_plate:<12} ({v['plate_status']}) "
              f"colour={v['colour'] or '-'} type={v['vehicle_type'] or '-'} "
              f"cams={','.join(v['cameras'])} slot={v['slot_id'] or '-'} {v['state']}")
    print("\\nEXIT VEHICLE DETAILS (actual EXIT lifecycle events):")
    exit_details = report.get("exit_vehicle_details", [])
    if not exit_details:
        print("  No EXIT vehicle records with a track ID or vehicle UID.")
    for item in exit_details:
        display_plate = item.get("number_plate_display") or "NO-PLATE"
        print(
            f"  camera={item.get('camera_id') or '-'} "
            f"UID={item.get('vehicle_uid') or '-'} "
            f"track={item.get('track_id') or '-'} "
            f"type={item.get('vehicle_type') or '-'} "
            f"colour={item.get('colour') or '-'} "
            f"plate={display_plate} ({item.get('plate_status') or 'UNKNOWN'}) "
            f"time={item.get('event_time') or item.get('captured_at') or '-'} "
            f"slot={item.get('slot_id') or '-'} "
            f"inferred={item.get('inferred', False)}"
        )

    # Print every configured slot and its current/departed vehicle history.
    print("\\nALL SLOT DETAILS (current occupancy + vehicle history):")
    slot_details = report.get("slot_details") or []
    slot_summary = report.get("slot_details_summary") or {}
    print(
        f"  Total={slot_summary.get('total_slots', len(slot_details))} | "
        f"Occupied={slot_summary.get('occupied_slots', 0)} | "
        f"Free={slot_summary.get('free_slots', 0)} | "
        f"With departed history={slot_summary.get('slots_with_departed_vehicle_history', 0)}"
    )

    for slot in slot_details:
        state = str(slot.get("state") or "UNKNOWN").upper()
        current = slot.get("current_vehicle") or {}
        print(
            f"\\n  {slot.get('slot_id') or '-'} | "
            f"{slot.get('camera_id') or '-'} | "
            f"{state} | Level={slot.get('level') or '-'}"
        )

        if current:
            print(
                f"    CURRENT: UID={current.get('vehicle_uid') or '-'} "
                f"Track={current.get('track_id') or '-'} "
                f"Type={current.get('vehicle_type') or '-'} "
                f"Colour={current.get('colour') or '-'}"
            )
            print(
                f"    PLATE: {current.get('plate_display') or 'NO-PLATE'} "
                f"({current.get('plate_status') or 'UNKNOWN'}) | "
                f"Source={current.get('occupancy_source') or '-'} | "
                f"Baseline={current.get('baseline')}"
            )
            print(
                f"    PARKED: {current.get('parked_at_ist') or current.get('parked_at') or 'Not recorded'}"
            )
        else:
            print("    CURRENT: Occupancy unavailable" if state == "UNKNOWN" else "    CURRENT: No vehicle")

        printed_events = set()

        for vehicle in slot.get("vehicle_history") or []:
            print(
                f"    PREVIOUS VEHICLE: UID={vehicle.get('vehicle_uid') or '-'} "
                f"Track={vehicle.get('track_id') or '-'} "
                f"Type={vehicle.get('vehicle_type') or '-'} "
                f"Colour={vehicle.get('colour') or '-'}"
            )
            print(
                f"    PLATE: {vehicle.get('number_plate_display') or 'NO-PLATE'} "
                f"({vehicle.get('plate_status') or 'UNKNOWN'})"
            )
            print(
                f"    PARKING: ENTRY={vehicle.get('entry_time') or '-'} | "
                f"PARK_START={vehicle.get('park_start_time') or '-'} | "
                f"PARK_END={vehicle.get('park_end_time') or '-'} | "
                f"EXIT={vehicle.get('event_time') or '-'}"
            )
            duration = vehicle.get("parking_duration_seconds")
            if duration is not None:
                print(f"    PARKING DURATION: {duration} seconds")

            for event in vehicle.get("lifecycle_history") or []:
                event_id = event.get("event_id")
                if event_id and event_id in printed_events:
                    continue
                if event_id:
                    printed_events.add(event_id)
                print(
                    f"      {event.get('event_type') or 'EVENT'}: "
                    f"{event.get('timestamp') or '-'} "
                    f"| track={event.get('track_id') or '-'} "
                    f"| inferred={event.get('inferred', False)}"
                )

        for event in slot.get("lifecycle_history") or []:
            event_id = event.get("event_id")
            if event_id and event_id in printed_events:
                continue
            if event_id:
                printed_events.add(event_id)
            print(
                f"    LIFECYCLE {event.get('event_type') or 'EVENT'}: "
                f"{event.get('timestamp') or event.get('event_time') or '-'} "
                f"| UID={event.get('vehicle_uid') or '-'} "
                f"| track={event.get('track_id') or '-'} "
                f"| inferred={event.get('inferred', False)}"
            )

        if not current and not slot.get("vehicle_history"):
            print("    HISTORY: No departed vehicle history recorded.")

    s = report["summary"]
    print(f"\nService 2 payloads: {s['service2_entry_events']} ENTRY, {s['service2_exit_events']} EXIT "
          f"| PARK_END: {s['park_end_events']} "
          f"| camera transfers: {s['camera_transfers']}")
    print("Validation: " + ", ".join(f"{k}={v}" for k, v in report["validation"].items()))
    print(line + "\n")
