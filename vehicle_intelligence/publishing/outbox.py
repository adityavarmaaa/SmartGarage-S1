
from http.client import HTTPException
import json
import logging
import os
import sqlite3
from vehicle_intelligence.common.errors import Diagnostic, ProcessingError
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag

from vehicle_intelligence.security import encrypt_text, decrypt_text


log = logging.getLogger("outbox")


def _s2_headers():
    """Build Service 2 request headers."""
    headers = {"Content-Type": "application/json"}

    key = os.getenv("S2_API_KEY")
    if key:
        header_name = os.getenv("S2_API_KEY_HEADER", "X-API-Key")
        headers[header_name] = key

    return headers


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_event_endpoint(url):
    endpoint = urlsplit(url)
    if endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.username is not None:
        raise ValueError("Configure an HTTP(S) Service 2 endpoint without embedded credentials.")
    return urllib.request.build_opener(_NoRedirect())


def post_event(url, payload, timeout, opener):
    """Return a persistence acknowledgement or a sanitized failure and retry policy."""
    request = urllib.request.Request(
        url, data=payload.encode("utf-8"), headers=_s2_headers(), method="POST",
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                return None, f"HTTP_{response.status}", True
            ack = json.loads(response.read(65537))
    except urllib.error.HTTPError as exc:
        # A conflict can be out of order, but it never acknowledges persistence.
        return None, f"HTTP_{exc.code}", exc.code not in {400, 405, 415, 422}
    except (OSError, UnicodeError, ValueError, HTTPException):
        return None, "INVALID_RESPONSE_OR_TRANSPORT_FAILURE", True

    event = json.loads(payload)
    if (not isinstance(ack, dict) or ack.get("status") not in {"accepted", "duplicate"}
            or type(ack.get("event_id")) is not int or ack["event_id"] <= 0
            or ack.get("event_type") != event.get("event_type")
            or ("sender_event_id" in ack and ack["sender_event_id"] != event.get("event_id"))):
        return None, "MISSING_PERSISTENCE_ACK", True
    return {"status": ack["status"], "event_id": ack["event_id"]}, None, False



class Outbox:
    """Durable encrypted event queue and pending parking event storage."""

    def __init__(self, db_path, url, batch=20, timeout=3.0, max_attempts=8):
        self._http = open_event_endpoint(url)
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

        self.url = url
        self.batch = batch
        self.timeout = timeout
        self.max_attempts = max_attempts

        self.db = sqlite3.connect(
            db_path,
            check_same_thread=False,
        )

        self.db.execute("PRAGMA journal_mode=WAL")

        self.db.execute("""
            CREATE TABLE IF NOT EXISTS outbox(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE,
                payload TEXT,
                sent INTEGER DEFAULT 0,
                attempts INTEGER DEFAULT 0,
                next_try REAL DEFAULT 0
            )
        """)

        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(outbox)")}
            for name, field_type in (
                ("acknowledgement", "TEXT"), ("failed_at", "REAL"), ("last_error", "TEXT"),
            ):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE outbox ADD COLUMN {name} {field_type}")

        self.db.execute("""
            CREATE TABLE IF NOT EXISTS pending_parking_events(
                event_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
        """)

        self.db.commit()

        self.lock = threading.Lock()
        self._stop = threading.Event()

        # Migrate existing plaintext records before starting the sender.
        try:
            self._migrate_existing_payloads()
        except Exception:
            self.db.close()
            raise

        self._thread = threading.Thread(
            target=self._send_until_stopped,
            daemon=True,
            name="outbox",
        )
        self._thread.start()

    def _migrate_existing_payloads(self):
        """
        Encrypt existing plaintext records in place.

        Existing event IDs, retry counts, timestamps and sent status
        are preserved. The migration is transactional.
        """
        with self.lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")

                rows = self.db.execute("""
                    SELECT id, payload
                    FROM outbox
                    WHERE payload IS NOT NULL
                """).fetchall()

                for row_id, payload in rows:
                    if not payload.startswith("enc:v1:"):
                        protected = encrypt_text(payload)

                        self.db.execute(
                            "UPDATE outbox SET payload=? WHERE id=?",
                            (protected, row_id),
                        )

                rows = self.db.execute("""
                    SELECT event_key, payload
                    FROM pending_parking_events
                """).fetchall()

                for event_key, payload in rows:
                    if not payload.startswith("enc:v1:"):
                        protected = encrypt_text(payload)

                        self.db.execute("""
                            UPDATE pending_parking_events
                            SET payload=?
                            WHERE event_key=?
                        """, (protected, event_key))

                self.db.commit()

                log.info(
                    "Outbox encryption migration completed"
                )

            except Exception:
                self.db.rollback()
                log.exception(
                    "Outbox encryption migration failed"
                )
                raise

    def publish(self, event):
        """Encrypt and persist an outgoing event."""
        try:
            serialized = json.dumps(event, ensure_ascii=False)
            protected = encrypt_text(serialized)
            with self.lock, self.db:
                self.db.execute("""
                    INSERT OR IGNORE INTO outbox(event_id, payload)
                    VALUES (?, ?)
                """, (event["event_id"], protected))
            log.info("OUTBOX_QUEUED event_id=%s", event["event_id"])
        except (sqlite3.Error, TypeError, ValueError, KeyError, RuntimeError) as exc:
            raise ProcessingError(Diagnostic(
                "OUTBOX_PERSIST_FAILED", "PUBLISHING", "outbox", "persist",
                "persistence", "The outgoing event could not be committed.",
                True, "failed", "The caller must retain and retry the event.",
                context={"event_id": event.get("event_id")} if isinstance(event, dict) else {},
            )) from exc

    def pending(self):
        """Count unsent outgoing events."""
        with self.lock:
            return self.db.execute(
                "SELECT COUNT(*) FROM outbox WHERE sent=0 AND failed_at IS NULL"
            ).fetchone()[0]

    def save_pending_event(self, event_key, payload):
        """Encrypt and save an event awaiting plate finalization."""
        serialized = json.dumps(payload, ensure_ascii=False)
        protected = encrypt_text(serialized)

        with self.lock:
            self.db.execute("""
                INSERT INTO pending_parking_events(
                    event_key,
                    payload,
                    updated_at
                )
                VALUES (?, ?, ?)
                ON CONFLICT(event_key) DO UPDATE SET
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
            """, (
                event_key,
                protected,
                time.time(),
            ))

            self.db.commit()

    def load_pending_events(self):
        """Load and decrypt pending events."""
        with self.lock:
            rows = self.db.execute("""
                SELECT event_key, payload
                FROM pending_parking_events
                ORDER BY updated_at, event_key
            """).fetchall()

        events = []

        for event_key, serialized in rows:
            try:
                plaintext = decrypt_text(serialized)
                payload = json.loads(plaintext)

                if isinstance(payload, dict):
                    events.append((event_key, payload))
                else:
                    log.error(
                        "Ignoring pending event: payload is not an object"
                    )

            except Exception:
                log.exception(
                    "Unable to decrypt or read a pending event"
                )

        return events

    def delete_pending_event(self, event_key):
        """Delete a pending event after its outgoing event is persisted."""
        with self.lock:
            self.db.execute(
                "DELETE FROM pending_parking_events WHERE event_key=?",
                (event_key,),
            )
            self.db.commit()

    def pending_event_count(self):
        """Count events waiting for plate finalization."""
        with self.lock:
            return self.db.execute(
                "SELECT COUNT(*) FROM pending_parking_events"
            ).fetchone()[0]

    def _post(self, payload):
        return post_event(self.url, payload, self.timeout, self._http)

    def _send_until_stopped(self):
        try:
            self._run()
        finally:
            with self.lock:
                self.db.close()

    def _run(self):
        """Background worker for outgoing events."""
        while not self._stop.is_set():

            with self.lock:
                rows = self.db.execute("""
                    SELECT id, event_id, payload, attempts
                    FROM outbox
                    WHERE sent=0 AND failed_at IS NULL AND next_try<=?
                    ORDER BY id
                    LIMIT ?
                """, (
                    time.time(),
                    self.batch,
                )).fetchall()

            if not rows:
                self._stop.wait(0.2)
                continue

            for row_id, event_id, encrypted_payload, attempts in rows:
                if self._stop.is_set():
                    break

                log.info("OUTBOX_ATTEMPT event_id=%s attempt=%d", event_id, attempts + 1)
                try:
                    payload = decrypt_text(encrypted_payload)
                    ack, error, retryable = self._post(payload)
                except (InvalidTag, RuntimeError, TypeError, ValueError):
                    ack, error, retryable = None, "UNREADABLE_OUTBOX_PAYLOAD", False

                with self.lock, self.db:
                    if ack is not None:
                        self.db.execute(
                            "UPDATE outbox SET sent=1, acknowledgement=?, last_error=NULL WHERE id=?",
                            (json.dumps(ack), row_id),
                        )
                        log.info("OUTBOX_ACK event_id=%s status=%s stored_event_id=%s",
                                 event_id, ack["status"], ack["event_id"])
                    elif retryable and attempts + 1 < self.max_attempts:
                        delay = min(60.0, 2.0 ** min(attempts + 1, 6))
                        self.db.execute(
                            "UPDATE outbox SET attempts=attempts+1, next_try=?, last_error=? WHERE id=?",
                            (time.time() + delay, error, row_id),
                        )
                        log.warning("OUTBOX_RETRY event_id=%s reason=%s delay_seconds=%s",
                                    event_id, error, delay)
                    else:
                        self.db.execute(
                            "UPDATE outbox SET attempts=attempts+1, failed_at=?, last_error=? WHERE id=?",
                            (time.time(), error, row_id),
                        )
                        log.error("OUTBOX_DEAD_LETTER event_id=%s reason=%s", event_id, error)

    def stop(self, wait=True, timeout=5.0):
        """Request worker shutdown and optionally wait."""
        self._stop.set()

        if wait and threading.current_thread() is not self._thread:
            self._thread.join(timeout=timeout)

        stopped = not self._thread.is_alive()

        if not stopped:
            log.warning(
                "Outbox worker did not stop within %.1f seconds",
                timeout,
            )

        return stopped
