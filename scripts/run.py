"""Run and verify an isolated recorded-video preview or local PostgreSQL integration."""
import argparse
from contextlib import ExitStack, closing
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def run_command(args, log, env=None, cwd=ROOT):
    with log.open("w", encoding="utf-8") as stream:
        subprocess.run([sys.executable, *args], cwd=cwd, env=env, check=True,
                       stdout=stream, stderr=subprocess.STDOUT)


def start_api(stack, module, health, env, directory):
    import httpx

    log = directory / f"{module}.log"
    # Let the kernel allocate a port. Read that port after Uvicorn binds.
    stream = stack.enter_context(log.open("w", encoding="utf-8"))
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", module + ":app", "--host", "127.0.0.1", "--port", "0"],
        cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT,
    )

    def stop():
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    stack.callback(stop)
    deadline = time.monotonic() + 30
    with httpx.Client(trust_env=False, timeout=2) as client:
        while process.poll() is None and time.monotonic() < deadline:
            match = re.search(r"Uvicorn running on http://127\.0\.0\.1:(\d+)",
                              log.read_text(encoding="utf-8", errors="replace"))
            if match:
                url = f"http://127.0.0.1:{match.group(1)}"
                try:
                    response = client.get(url + health)
                    if (response.is_success
                            and (health != "/health/database" or response.json().get("database") == "connected")):
                        schema = client.get(url + "/openapi.json")
                        docs = client.get(url + "/docs")
                        if schema.is_success and schema.json().get("openapi") and docs.is_success and "SwaggerUIBundle" in docs.text:
                            return url
                except httpx.TransportError:
                    pass
            time.sleep(0.25)
    raise ValueError(f"{module} did not become ready. See {log.name}.")


def check_output(directory, expected_slots, publishing):
    import cv2

    report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    if report["processing_status"] != "completed" or report["processing_failures"]:
        raise ValueError("Video processing did not complete successfully.")
    if report["service2_publishing"] is not publishing:
        raise ValueError("Pipeline publishing mode did not match the requested mode.")
    slots = [(row["camera_id"], row["slot_id"]) for row in report["slot_details"]]
    if set(slots) != expected_slots or len(slots) != len(expected_slots):
        raise ValueError("The JSON report did not include each configured slot exactly once.")
    capture = cv2.VideoCapture(str(directory / "annotated.mp4"))
    try:
        expected_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        frames = 0
        while capture.read()[0]:
            frames += 1
    finally:
        capture.release()
    if frames == 0 or (expected_frames > 0 and frames != expected_frames):
        raise ValueError("Annotated MP4 did not decode through its expected frame count.")
    return {"decoded_output_frames": frames, "configured_slots": len(slots),
            "vehicles_seen": report["summary"]["vehicles_seen"]}


def verify_database(url, directory, endpoint, expected_slots, *, require_session=True):
    import psycopg
    from vehicle_intelligence.common.security import decrypt_text
    from vehicle_intelligence.publishing.outbox import Outbox

    events = []
    delivery = dict(queued=0, acknowledged=0, pending=0, failed=0, retry_failures=0)
    for path in sorted(directory.glob("outbox*.db")):
        with closing(sqlite3.connect(path)) as queue:
            outbox_table = queue.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='outbox'"
            ).fetchone()
            if not outbox_table:
                continue
            pending = queue.execute("SELECT 1 FROM outbox WHERE sent=0 AND failed_at IS NULL LIMIT 1").fetchone()
            if pending:
                outbox = Outbox(str(path), endpoint)
                try:
                    deadline = time.monotonic() + 30
                    while outbox.pending() and time.monotonic() < deadline:
                        time.sleep(0.25)
                finally:
                    outbox.stop()
            for encrypted, sent, acknowledgement, failed_at, attempts in queue.execute(
                "SELECT payload, sent, acknowledgement, failed_at, attempts FROM outbox ORDER BY id"
            ):
                delivery["queued"] += 1
                delivery["retry_failures"] += attempts
                if not sent or not acknowledgement:
                    delivery["failed" if failed_at is not None else "pending"] += 1
                    continue
                delivery["acknowledged"] += 1
                payload = json.loads(decrypt_text(encrypted))
                if payload.get("event_type") in {"ENTRY", "EXIT"}:
                    events.append((payload, json.loads(acknowledgement)["event_id"]))

    (directory / "outbox-verification.json").write_text(json.dumps(delivery, indent=2), encoding="utf-8")
    if delivery["pending"] or delivery["failed"]:
        raise ValueError("Outgoing events remained pending or failed. Inspect outbox-verification.json and the service log.")

    entries = {}
    departures = {}
    latest = {}
    persisted = []
    closed_sessions = []
    with psycopg.connect(**url.translate_connect_args(username="user", database="dbname"),
                         connect_timeout=5) as db:
        db.execute("SET TRANSACTION READ ONLY")
        stored_slots = set(db.execute("SELECT camera_id, slot_id FROM parking_slot_state"))
        if expected_slots - stored_slots:
            raise ValueError("Expected calibrated slots are missing in PostgreSQL.")
        for event, persisted_id in events:
            kind = event["event_type"]
            uid = event.get("vehicle_uid")
            slot = (event.get("parking") or {}).get("slot_id")
            camera = event["camera_id"]
            area = event["parking_area_id"]
            event_time = datetime.fromisoformat(event.get("departure_at") or
                                               event.get("captured_at") or event["timestamp"])
            if not uid or (camera, slot) not in expected_slots:
                raise ValueError("Mapped event has no verifiable identity or configured slot.")
            count = db.execute(
                """SELECT count(*) FROM parking_events WHERE event_type=%s AND parking_area_id=%s
                AND camera_id=%s AND slot_id=%s AND vehicle_uid=%s AND captured_at=%s AND id=%s""",
                (kind, area, camera, slot, uid, event_time, persisted_id),
            ).fetchone()[0]
            if count != 1:
                raise ValueError(f"Expected exactly one persisted {kind} event for a mapped observation.")
            persisted.append({"sender_event_id": event["event_id"], "database_event_id": persisted_id,
                              "event_type": kind, "camera_id": camera, "parking_area_id": area,
                              "slot_id": slot, "vehicle_uid": uid, "event_time": event_time.isoformat()})
            key = (area, camera, slot, uid)
            (entries if kind == "ENTRY" else departures).setdefault(key, []).append(event_time)
            previous = latest.get((camera, slot))
            if previous is None or event_time >= previous[0]:
                latest[camera, slot] = (event_time, kind, uid)

        durations = []
        for key, exit_times in departures.items():
            for exit_time in exit_times:
                rows = db.execute(
                    """SELECT id, entry_time, duration_seconds FROM parking_sessions
                    WHERE parking_area_id=%s AND camera_id=%s AND slot_id=%s AND vehicle_uid=%s
                    AND exit_time=%s AND status='exited'""", (*key, exit_time),
                ).fetchall()
                if len(rows) != 1:
                    raise ValueError("Expected exactly one closed session for a persisted EXIT.")
                session_id, entry_time, seconds = rows[0]
                if entry_time not in entries.get(key, []) or seconds != int((exit_time-entry_time).total_seconds()) or seconds <= 0:
                    raise ValueError("Persisted departure does not match a positive-duration session opened by this run.")
                durations.append(seconds)
                area, camera, slot, uid = key
                closed_sessions.append({
                    "database_session_id": session_id, "parking_area_id": area,
                    "camera_id": camera, "slot_id": slot, "vehicle_uid": uid,
                    "entry_time": entry_time.isoformat(), "exit_time": exit_time.isoformat(),
                    "duration_seconds": seconds,
                })

        for (camera, slot), (_, kind, uid) in latest.items():
            row = db.execute(
                "SELECT status, vehicle_uid FROM parking_slot_state WHERE camera_id=%s AND slot_id=%s",
                (camera, slot),
            ).fetchone()
            expected = ("available", None) if kind == "EXIT" else ("occupied", uid)
            if row != expected:
                raise ValueError("Final slot state disagrees with the last persisted event.")

    result = {"entry_events": sum(map(len, entries.values())),
              "exit_events": sum(map(len, departures.values())), "closed_session_seconds": durations,
              "configured_slots_verified": len(expected_slots), "persisted_events": persisted,
              "closed_sessions": closed_sessions,
              "final_slot_state": [
                  {"camera_id": camera, "slot_id": slot, "state": "available" if kind == "EXIT" else "occupied"}
                  for (camera, slot), (_, kind, _) in latest.items()
              ]}
    (directory / "database-verification.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    if require_session and not durations:
        raise ValueError("Recording did not establish and close a session. See database-verification.json.")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("preview", "full"))
    input_args = parser.add_mutually_exclusive_group(required=True)
    input_args.add_argument("--config", type=Path)
    input_args.add_argument("--source", type=Path, help="Generate a preview YAML for the default camera calibration")
    parser.add_argument("--output-root", type=Path, default=Path("runs"))
    args = parser.parse_args()
    full = args.mode == "full"
    if full and not args.config:
        parser.error("Full mode requires a calibrated --config with its own complete parking recording.")
    if sys.prefix == sys.base_prefix:
        parser.error("Run with the project's virtual environment Python after setup.")

    sys.path.insert(0, str(ROOT))
    import yaml
    from vehicle_intelligence.common.config import camera_configs, load_config, validate_geometry_for_camera
    from vehicle_intelligence.geometry import Geometry

    config_path = args.config.resolve() if args.config else None
    directory = args.output_root.resolve() / uuid4().hex
    directory.mkdir(mode=0o700, parents=True)
    print(f"Run directory: {directory}", flush=True)
    summary = {"mode": args.mode, "publishing": False, "status": "failed"}
    run_errors = (OSError, ValueError, KeyError, subprocess.CalledProcessError)
    try:
        if args.source:
            config_path = directory / "source.yaml"
            run_command(["scripts/make_handover_config.py", "--source", str(args.source.resolve()),
                         "--output", str(config_path)], directory / "config.log")
        cfg = load_config(config_path)
        if "cameras" not in cfg:
            raise ValueError("The runner requires a cameras list to use the multicamera report and MP4 pipeline.")
        cameras = camera_configs(cfg)
        if full and any(camera["publisher"].get("enabled") is False for camera in cameras):
            raise ValueError("Publishing is disabled in the configuration. Select preview or explicitly enable it.")
        expected_slots = set()
        for camera in cameras:
            if not (ROOT / str(camera["source"])).is_file():
                raise ValueError("The recorded-video runner requires a local file for every camera.")
            geometry = Geometry.load(camera["geometry_file"])
            validate_geometry_for_camera(camera, geometry)
            expected_slots.update((camera["camera_id"], slot["slot_id"]) for slot in geometry.slots)
            if full and (camera.get("exit_restore") or {}).get("enabled"):
                raise ValueError("Restored EXIT sessions require preview. Supply footage with parking and departure.")
        cfg["detector"]["model_path"] = str((ROOT / cfg["detector"]["model_path"]).resolve())
        for camera in cfg["cameras"]:
            for key in ("source", "geometry_file"):
                camera[key] = str((ROOT / camera[key]).resolve())
        for section in (cfg, *cfg["cameras"]):
            restore = section.get("exit_restore", {})
            for key in ("manifest_path", "entry_report_path"):
                if restore.get(key):
                    restore[key] = str((ROOT / restore[key]).resolve())
        cfg["report"].update(path=str(directory / "report.json"), plate_evidence_dir=str(directory / "evidence"))
        cfg.setdefault("demo_video", {}).update(enabled=True, path=str(directory / "annotated.mp4"))
        cfg.setdefault("camera_motion", {})["diagnostics_path"] = str(directory / "motion.csv")
        endpoint = None
        with ExitStack() as stack:
            env = os.environ.copy()
            env.update(SMART_PARKING_API_PUBLISH="0", SMART_PARKING_OUTPUT_DIR=str(directory / "api"),
                       SMART_PARKING_UPLOAD_DIR=str(directory / "uploads"))
            if full:
                try:
                    import httpx
                    import psycopg
                    from sqlalchemy.engine import make_url
                    from sqlalchemy.exc import ArgumentError
                except ImportError:
                    raise ValueError("Full mode requires the service dependencies. Run scripts/setup.py --mode full --install.") from None
                run_errors += (psycopg.Error, httpx.HTTPError)

                for name in ("DATABASE_URL", "S2_API_KEY", "SP_ENCRYPTION_KEYS",
                             "SP_ACTIVE_ENCRYPTION_KEY", "SP_HMAC_KEY"):
                    if not env.get(name):
                        raise ValueError(f"Set {name} before running full mode.")
                try:
                    url = make_url(env["DATABASE_URL"])
                except ArgumentError:
                    raise ValueError("DATABASE_URL is malformed.") from None
                if (url.drivername != "postgresql+psycopg" or url.host not in {"localhost", "127.0.0.1", "::1"}
                        or url.query or not url.database or url.database in {"postgres", "template0", "template1"}):
                    raise ValueError("Full mode requires a local PostgreSQL development database URL without query overrides.")
                run_command(["scripts/init_database.py", "--config", str(config_path), "--create-database"],
                            directory / "migration.log", env)
                run_command(["scripts/init_database.py", "--check"], directory / "schema.log", env)
                endpoint = start_api(stack, "parking_management.app.main", "/health/database", env, directory)
                endpoint += "/internal/events/vehicle"
            for sections in (cfg, *cfg["cameras"]):
                sections.setdefault("exit_restore", {})["preview_db_dir"] = str(directory / "preview-db")
                sections.setdefault("publisher", {}).update(
                    outbox_db=str(directory / "outbox.db"), snapshot_dir=str(directory / "snapshots"),
                    s2_url=endpoint or "http://127.0.0.1:1/preview",
                )
            generated = directory / "run.yaml"
            generated.write_text(yaml.safe_dump(cfg), encoding="utf-8")
            run_command(["preflight.py", str(generated)], directory / "preflight.log", env)
            if full:
                env["SMART_PARKING_CONFIG"] = str(generated)
                start_api(stack, "vehicle_intelligence.api", "/health", env, directory)
            command = ["-m", "vehicle_intelligence.main", "--config", str(generated),
                       "--log-dir", str(directory / "logs")]
            if not full:
                command.append("--no-publisher")
            env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(ROOT), env.get("PYTHONPATH"))))
            (directory / "data").mkdir()  # Isolate legacy local diagnostic files.
            summary["publishing"] = full
            run_command(command, directory / "pipeline.log", env, cwd=directory)
            summary.update(check_output(directory, expected_slots, full))
            if full:
                from scripts.acceptance_evidence import verify_recording_lifecycle
                summary["postgresql"] = verify_database(
                    url, directory, endpoint, expected_slots, require_session=False,
                )
                report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
                evidence = verify_recording_lifecycle(report, summary["postgresql"])
                (directory / "footage-suitability.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
                summary["status"] = evidence["status"]
                if evidence["status"] == "blocked":
                    summary["error"] = "No independently observed lifecycle matched PostgreSQL. See footage-suitability.json."
            else:
                summary["status"] = "passed"
    except run_errors as exc:
        # Subprocesses write their own diagnostics. Never echo a connection string or credential.
        summary["error"] = (f"Command failed with exit status {exc.returncode}; see run logs."
                            if isinstance(exc, subprocess.CalledProcessError) else
                            str(exc) if type(exc) is ValueError else type(exc).__name__)
    finally:
        (directory / "execution-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary))
    return {"passed": 0, "failed": 1, "blocked": 2}[summary["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
