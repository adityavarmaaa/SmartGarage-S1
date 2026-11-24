"""Exercise the local S1 mapper and encrypted HTTP outbox against a fresh PostgreSQL DB."""
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
from uuid import uuid4

import httpx
import psycopg
from sqlalchemy.engine import make_url
import uvicorn

ROOT = Path(__file__).resolve().parents[1]


def wait_for(check):
    for _ in range(100):
        if check():
            return
        time.sleep(0.1)
    raise RuntimeError("Timed out waiting for local service or expected occupancy.")


def main():
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit("Export a local development DATABASE_URL first.")
    url = make_url(os.environ["DATABASE_URL"])
    if url.drivername != "postgresql+psycopg" or url.host not in {"127.0.0.1", "::1", "localhost"} or url.query:
        raise SystemExit("Use a local PostgreSQL development URL without query options.")
    suffix = uuid4().hex
    area, camera, slot = ("smoke-" + suffix + "-" + kind for kind in ("area", "camera", "slot"))
    database = "smartgarage_smoke_" + suffix
    url = url.set(database=database)
    os.environ.update(
        DATABASE_URL=url.render_as_string(hide_password=False),
        S2_API_KEY=secrets.token_urlsafe(32),
        S2_API_KEY_HEADER="X-API-Key",
        SP_ACTIVE_ENCRYPTION_KEY="smoke",
        SP_ENCRYPTION_KEYS=json.dumps({"smoke": base64.b64encode(secrets.token_bytes(32)).decode()}),
        SP_HMAC_KEYS=json.dumps({"smoke": base64.b64encode(secrets.token_bytes(32)).decode()}),
        SP_ACTIVE_HMAC_KEY="smoke",
    )
    print(f"Disposable PostgreSQL database: {database}", flush=True)
    command = [sys.executable, str(ROOT / "scripts/init_database.py")]
    subprocess.run([*command, "--create-database", "--area", area], check=True)
    subprocess.run([*command, "--area", area], check=True)
    subprocess.run([*command, "--check"], check=True)

    sys.path.insert(0, str(ROOT))
    from scripts.run import verify_database
    from parking_management.app.main import app
    from vehicle_intelligence.publishing.outbox import Outbox
    from vehicle_intelligence.publishing.s2_mapper import S2Mapper

    with socket.socket() as listener, TemporaryDirectory() as directory:
        listener.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
        thread.start()
        try:
            wait_for(lambda: server.started)
            with httpx.Client(base_url=base, trust_env=False) as client:
                assert client.get("/health/database").json()["database"] == "connected"
                assert client.get("/openapi.json").is_success
                assert client.post("/internal/events/vehicle", json={}).status_code == 401
                assert client.post("/internal/events/vehicle", json={}, headers={
                    "X-API-Key": os.environ["S2_API_KEY"]
                }).status_code == 422
                outbox = Outbox(str(Path(directory) / "outbox.db"), base + "/internal/events/vehicle")
                mapper = S2Mapper(outbox, str(Path(directory) / "lifecycle.db"))
                captured = datetime.now(timezone.utc)

                def send(kind, elapsed=0):
                    timestamp = (captured + timedelta(seconds=elapsed)).isoformat()
                    mapper.publish({
                        "event_id": uuid4().hex, "event_type": kind, "vehicle_uid": suffix,
                        "camera_id": camera, "parking_area_id": area, "timestamp": timestamp,
                        "event_time": timestamp, "captured_at": timestamp,
                        "parking": {"slot_id": slot}, "vehicle": {},
                    })

                def occupancy():
                    with psycopg.connect(**url.translate_connect_args(username="user", database="dbname")) as db:
                        row = db.execute("SELECT status, vehicle_uid FROM parking_slot_state WHERE camera_id=%s AND slot_id=%s",
                                         (camera, slot)).fetchone()
                        return row

                try:
                    send("ENTRY")
                    send("PARK_START", 1)
                    wait_for(lambda: occupancy() == ("occupied", suffix))
                    send("PARK_END", 2)
                    wait_for(lambda: occupancy() == ("available", None))
                    with psycopg.connect(**url.translate_connect_args(username="user", database="dbname")) as db:
                        assert db.execute("SELECT status, duration_seconds, entry_time, exit_time FROM parking_sessions WHERE vehicle_uid=%s",
                                          (suffix,)).fetchall() == [("exited", 1, captured + timedelta(seconds=1), captured + timedelta(seconds=2))]
                        assert db.execute("SELECT event_type FROM parking_events WHERE vehicle_uid=%s ORDER BY id",
                                          (suffix,)).fetchall() == [("ENTRY",), ("EXIT",)]
                finally:
                    mapper.stop()
                verified = verify_database(url, Path(directory), base + "/internal/events/vehicle",
                                           {(camera, slot)})
                assert verified["closed_session_seconds"] == [1]
                print("Verified controlled-event PostgreSQL rows:", json.dumps(verified))
        finally:
            server.should_exit = True
            thread.join()
    print("PASS: schema, API auth, mapping, HTTP delivery, PostgreSQL session, 1-second duration and slot release.")


if __name__ == "__main__":
    main()
