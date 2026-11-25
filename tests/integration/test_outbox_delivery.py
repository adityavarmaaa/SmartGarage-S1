import base64
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
from threading import Thread
import time

import pytest

from vehicle_intelligence.publishing.outbox import Outbox


@pytest.mark.parametrize("status,response", [(409, {"detail": "not committed"}), (200, {"status": "already_parked"})])
def test_rejection_is_never_delivered(tmp_path, monkeypatch, status, response):
    monkeypatch.setenv("SP_ACTIVE_ENCRYPTION_KEY", "test")
    monkeypatch.setenv("SP_ENCRYPTION_KEYS", json.dumps({"test": base64.b64encode(bytes(32)).decode()}))

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(status)
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

    with ThreadingHTTPServer(("127.0.0.1", 0), Receiver) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        path = tmp_path / "outbox.db"
        queue = Outbox(str(path), f"http://127.0.0.1:{server.server_port}/events", max_attempts=1)
        try:
            queue.publish({"event_id": "test-event", "event_type": "ENTRY"})
            deadline = time.monotonic() + 5
            while queue.pending() and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            assert queue.stop()
            server.shutdown()
            thread.join()
    with closing(sqlite3.connect(path)) as db:
        sent, ack, failed_at, error, ciphertext = db.execute(
            "SELECT sent, acknowledgement, failed_at, last_error, payload FROM outbox"
        ).fetchone()
    assert sent == 0 and ack is None and failed_at is not None
    assert error == ("HTTP_409" if status == 409 else "MISSING_PERSISTENCE_ACK")
    assert ciphertext.startswith("enc:v1:")


def test_pending_row_resumes_after_restart_and_records_duplicate_ack(tmp_path, monkeypatch):
    monkeypatch.setenv("SP_ACTIVE_ENCRYPTION_KEY", "test")
    monkeypatch.setenv("SP_ENCRYPTION_KEYS", json.dumps({"test": base64.b64encode(bytes(32)).decode()}))
    replies = [503, 200]

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(replies.pop(0))
            self.end_headers()
            self.wfile.write(b'{"status":"duplicate","event_id":7,"event_type":"ENTRY","sender_event_id":"test-event"}')

    with ThreadingHTTPServer(("127.0.0.1", 0), Receiver) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        path = tmp_path / "outbox.db"
        url = f"http://127.0.0.1:{server.server_port}/events"
        try:
            first = Outbox(str(path), url)
            try:
                first.publish({"event_id": "test-event", "event_type": "ENTRY"})
                with closing(sqlite3.connect(path)) as db:
                    deadline = time.monotonic() + 5
                    row = None
                    while time.monotonic() < deadline:
                        row = db.execute("SELECT sent, attempts, last_error FROM outbox").fetchone()
                        if row == (0, 1, "HTTP_503"):
                            break
                        time.sleep(0.05)
                    assert row == (0, 1, "HTTP_503")
            finally:
                assert first.stop()
            reopened = Outbox(str(path), url)
            try:
                deadline = time.monotonic() + 6
                while reopened.pending() and time.monotonic() < deadline:
                    time.sleep(0.05)
            finally:
                assert reopened.stop()
        finally:
            server.shutdown()
            thread.join()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT count(*) FROM outbox").fetchone() == (1,)
        sent, ack, attempts = db.execute("SELECT sent, acknowledgement, attempts FROM outbox").fetchone()
    assert sent == 1 and attempts == 1
    assert json.loads(ack) == {"status": "duplicate", "event_id": 7}


def test_operator_requeue_preserves_payload_and_event_id(tmp_path, capsys, monkeypatch):
    from scripts.requeue_outbox import main
    import sys

    path = tmp_path / "failed.db"
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            CREATE TABLE outbox (event_id TEXT, sent INTEGER, acknowledgement TEXT,
                attempts INTEGER, failed_at REAL, next_try REAL, last_error TEXT, payload TEXT);
            INSERT INTO outbox VALUES ('queued', 0, NULL, 8, 1, 1, 'HTTP_409', 'encrypted-payload');
        """)
    monkeypatch.setattr(sys, "argv", ["requeue_outbox.py", str(path), "--event-id", "queued"])
    main()
    assert json.loads(capsys.readouterr().out)["requeued"] is False
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT attempts, failed_at FROM outbox").fetchone() == (8, 1)
    sys.argv.append("--apply")
    main()
    assert json.loads(capsys.readouterr().out)["requeued"] is True
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT event_id, payload, attempts, failed_at, sent FROM outbox").fetchone() == (
            "queued", "encrypted-payload", 0, None, 0
        )
