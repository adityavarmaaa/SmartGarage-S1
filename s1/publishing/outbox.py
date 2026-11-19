
import json
import logging
import os
import sqlite3
from s1.common.errors import Diagnostic, ProcessingError
import threading
import time
import urllib.error
import urllib.request

from s1.security import encrypt_text, decrypt_text


log = logging.getLogger("outbox")


def _s2_headers():
    """Build Service 2 request headers."""
    headers = {"Content-Type": "application/json"}

    key = os.getenv("S2_API_KEY")
    if key:
        header_name = os.getenv("S2_API_KEY_HEADER", "X-API-Key")
        headers[header_name] = key

    return headers


class Outbox:
    """Durable encrypted event queue and pending parking event storage."""

    def __init__(self, db_path, url, batch=20, timeout=3.0):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

        self.url = url
        self.batch = batch
        self.timeout = timeout

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
                "SELECT COUNT(*) FROM outbox WHERE sent=0"
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
        """Send a decrypted JSON payload to Service 2."""
        req = urllib.request.Request(
            self.url,
            data=payload.encode("utf-8"),
            headers=_s2_headers(),
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                req,
                timeout=self.timeout,
            ) as response:

                log.info(
                    "Service 2 HTTP status: %s",
                    response.status,
                )

                return 200 <= response.status < 300

        except urllib.error.HTTPError as exc:
            log.warning(
                "Service 2 returned HTTP status: %s",
                exc.code,
            )

            return exc.code == 409

        except Exception:
            log.exception("Service 2 request failed")
            return None

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
                    SELECT id, payload, attempts
                    FROM outbox
                    WHERE sent=0 AND next_try<=?
                    ORDER BY id
                    LIMIT ?
                """, (
                    time.time(),
                    self.batch,
                )).fetchall()

            if not rows:
                self._stop.wait(0.2)
                continue

            for row_id, encrypted_payload, attempts in rows:

                if self._stop.is_set():
                    break

                try:
                    plaintext_payload = decrypt_text(
                        encrypted_payload
                    )

                    result = self._post(plaintext_payload)

                except Exception:
                    log.exception(
                        "Outbox event could not be decrypted or sent"
                    )
                    result = False

                with self.lock:

                    if result:
                        self.db.execute(
                            "UPDATE outbox SET sent=1 WHERE id=?",
                            (row_id,),
                        )

                    else:
                        delay = min(
                            60.0,
                            2.0 ** min(attempts + 1, 6),
                        )

                        self.db.execute("""
                            UPDATE outbox
                            SET attempts=attempts+1,
                                next_try=?
                            WHERE id=?
                        """, (
                            time.time() + delay,
                            row_id,
                        ))

                    self.db.commit()

                if result is None:
                    self._stop.wait(1.0)
                    break

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
