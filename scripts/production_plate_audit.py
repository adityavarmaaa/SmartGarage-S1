"""Export redacted provenance for a selected slot in historical project artifacts."""
import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path


def collect(assets, slot):
    labels = {}
    ids = {}
    candidates, lifecycle, states, crops = [], [], [], []
    files = [p for p in assets.rglob("*") if p.is_file() and p.suffix.lower() in {".enc", ".png", ".jpg"}]
    by_name = {p.name.casefold(): p for p in files}

    def label(value, family, registry):
        if not value:
            return None
        key = family, str(value)
        return registry.setdefault(key, f"{family}-{len(registry) + 1}")

    for path in sorted((assets / "reports").rglob("*.json")):
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(obj, dict):
            continue
        source = path.relative_to(assets).as_posix()
        for group, target in (("plate_evidence", candidates), ("exit_vehicle_details", lifecycle),
                              ("lifecycle_events", lifecycle), ("lifecycle_timeline", lifecycle),
                              ("slot_details", states), ("service2_events", lifecycle)):
            for row in obj.get(group) or []:
                if not isinstance(row, dict):
                    continue
                assigned = row.get("slot_id") or (row.get("bay") or {}).get("id") or (row.get("parking") or {}).get("slot_id")
                if str(assigned).casefold() != slot.casefold():
                    continue
                current = row.get("current_vehicle") or {}
                uid = row.get("vehicle_uid") or current.get("vehicle_uid")
                if row.get("plate_text_hmac"):
                    candidate_token = label(row["plate_text_hmac"], "digest", labels)
                else:
                    candidate_token = label(row.get("plate_text"), "plaintext-group", labels)
                artifact = {
                    "source": source, "sha256": sha256(path.read_bytes()).hexdigest(),
                    "group": group, "camera": row.get("camera_id"), "slot": assigned,
                    "vehicle_alias": label(uid, "vehicle", ids), "track_id": row.get("track_id") or current.get("track_id"),
                    "status": row.get("plate_status") or current.get("plate_status"),
                    "event": row.get("event_type"), "state": row.get("state"),
                    "timestamp": row.get("event_time") or row.get("entry_time_ist") or row.get("recording_started_at_ist"),
                    "ocr_confidence": row.get("ocr_confidence"), "crop_shape": row.get("crop_px"),
                    "candidate_alias": candidate_token, "crop_references": [],
                }
                for field in ("original_png", "clahe_png"):
                    ref = row.get(field)
                    if ref:
                        base = Path(str(ref).replace(chr(92), "/")).name
                        artifact["crop_references"].append({
                            "reference": base, "present": base.casefold() in by_name,
                            "encrypted": base.casefold().endswith(".enc"),
                        })
                target.append(artifact)
    for path in files:
        if path.name.casefold().startswith(slot.casefold() + "_"):
            crops.append({"path": path.relative_to(assets).as_posix(), "bytes": path.stat().st_size,
                          "encrypted": path.suffix.casefold() == ".enc",
                          "sha256": sha256(path.read_bytes()).hexdigest()})
    logs = []
    for path in sorted((*assets.glob("*.log"), *(assets / "logs").glob("*.log*"))):
        with path.open(errors="replace") as source:
            count = sum(slot.casefold() in line.casefold() for line in source)
        if count:
            logs.append({"path": path.relative_to(assets).as_posix(), "matching_lines": count})
    return {
        "slot": slot, "candidate_reports": candidates, "slot_records": states,
        "lifecycle_records": lifecycle, "crop_inventory": crops, "log_index": logs,
        "candidate_status_counts": dict(Counter(str(r["status"]) for r in candidates)),
        "redaction": "Plate values, ciphertext and HMACs are excluded. Aliases share a family only.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--slot", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = collect(args.assets, args.slot)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: len(v) if isinstance(v, list) else v for k, v in report.items()}, indent=2))
