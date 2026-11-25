
import copy
import logging
import os
import sqlite3
from datetime import datetime
from threading import RLock

from vehicle_intelligence.security import encrypt_text, plate_hmac

log = logging.getLogger("s2_mapper")


class S2Mapper:
    """Maps S1 lifecycle events to the S2 contract.

    Plate data is stored locally as AES-GCM ciphertext.
    HMAC fingerprints are used for matching without plaintext storage.
    Vehicle UID remains the fallback when OCR has not identified a plate.
    """

    def __init__(self, inner, db_path="data/lifecycle.db"):
        self._inner = inner
        self._db_path = db_path
        self.raw_events = []
        self._publish_lock = RLock()

        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)

        self._db = sqlite3.connect(
            self._db_path,
            timeout=10,
            check_same_thread=False,
        )

        self._db.execute("""
            CREATE TABLE IF NOT EXISTS parking_sessions (
                plate_hmac TEXT NOT NULL,
                plate_ciphertext TEXT NOT NULL,
                camera_id TEXT NOT NULL,
                parking_area_key TEXT NOT NULL,
                parking_area_id TEXT,
                slot_id TEXT,
                first_seen TEXT NOT NULL,
                parked_at TEXT,
                status TEXT NOT NULL DEFAULT 'OPEN',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (
                    plate_hmac,
                    camera_id,
                    parking_area_key
                )
            )
        """)

        self._db.execute("""
            CREATE TABLE IF NOT EXISTS parking_sessions_by_uid (
                vehicle_uid TEXT NOT NULL,
                camera_id TEXT NOT NULL,
                parking_area_key TEXT NOT NULL,
                parking_area_id TEXT,
                slot_id TEXT,
                first_seen TEXT NOT NULL,
                parked_at TEXT,
                status TEXT NOT NULL DEFAULT 'OPEN',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (
                    vehicle_uid,
                    camera_id,
                    parking_area_key
                )
            )
        """)

        # Fail safely rather than silently using an older plaintext schema.
        columns = {
            row[1]
            for row in self._db.execute(
                "PRAGMA table_info(parking_sessions)"
            ).fetchall()
        }

        required = {
            "plate_hmac",
            "plate_ciphertext",
            "camera_id",
            "parking_area_key",
            "first_seen",
            "status",
        }

        if not required.issubset(columns):
            self._db.close()
            raise RuntimeError(
                "Old plaintext lifecycle database schema detected. "
                "Back up and migrate it before starting S1."
            )

        self._db.execute("""
            CREATE TABLE IF NOT EXISTS park_start_delivery (
                camera_id TEXT NOT NULL,
                area_key TEXT NOT NULL,
                vehicle_uid TEXT NOT NULL,
                slot_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                event_id TEXT NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (camera_id, area_key, vehicle_uid, slot_id, observed_at)
            )
        """)
        self._db.commit()

    @staticmethod
    def _safe_report_event(event):
        """Copy an S1 lifecycle event without retaining plaintext plate values."""
        sensitive_keys = {
            "plate_text", "plate_number", "number_plate",
            "license_plate", "license_plate_number", "plate_candidate",
        }

        def clean(value, in_plate=False):
            if isinstance(value, dict):
                result = {}
                for key, item in value.items():
                    normalized = str(key).strip().lower().replace("-", "_")
                    if normalized in sensitive_keys:
                        result[key] = None
                    elif normalized == "plate":
                        result[key] = clean(item, True) if isinstance(item, dict) else None
                    elif in_plate and normalized in {
                        "text", "value", "number", "number_text"
                    }:
                        result[key] = None
                    else:
                        result[key] = clean(item, in_plate)
                return result
            if isinstance(value, list):
                return [clean(item, in_plate) for item in value]
            return copy.deepcopy(value)

        return clean(event)

    @staticmethod
    def _plate(event):
        status = event.get("plate_status") or (event.get("plate") or {}).get("status")
        if status is not None and str(status).upper() != "CONFIRMED":
            return None
        vehicle = event.get("vehicle") or {}
        plate = vehicle.get("plate")

        if not plate:
            plate_obj = event.get("plate") or {}
            plate = plate_obj.get("text")

        return str(plate).strip().upper() if plate else None

    @staticmethod
    def _slot(event):
        return (event.get("parking") or {}).get("slot_id")

    @staticmethod
    def _camera(event):
        return event.get("camera_id") or ""

    @staticmethod
    def _area(event):
        return event.get("parking_area_id")

    @staticmethod
    def _area_key(event):
        return str(event.get("parking_area_id") or "")

    @staticmethod
    def _uid(event):
        return event.get("vehicle_uid")

    @staticmethod
    def _plate_fingerprint(plate):
        return plate_hmac(plate) if plate else None

    @staticmethod
    def _encrypted_plate(plate):
        return encrypt_text(plate) if plate else None

    def _uid_session(self, event, status=None):
        uid = self._uid(event)
        if not uid:
            return None

        sql = """
            SELECT first_seen, parked_at, slot_id, status
            FROM parking_sessions_by_uid
            WHERE vehicle_uid=?
              AND camera_id=?
              AND parking_area_key=?
        """

        args = [
            str(uid),
            self._camera(event),
            self._area_key(event),
        ]

        if status:
            sql += " AND status=?"
            args.append(status)

        return self._db.execute(sql, args).fetchone()

    def _uid_session_for_exit(self, event, status=None):
        uid = self._uid(event)
        if not uid:
            return None

        rows = self._db.execute("""
            SELECT first_seen, parked_at, slot_id, status,
                   parking_area_key
            FROM parking_sessions_by_uid
            WHERE vehicle_uid=? AND camera_id=? AND status=?
        """, (
            str(uid), self._camera(event), status or "PARKED"
        )).fetchall()

        area = self._area_key(event)
        exact = [r for r in rows if r[4] == area]
        if len(exact) == 1:
            return exact[0]
        if exact:
            return None

        slot = self._slot(event)
        matches = [
            r for r in rows
            if slot and r[2] is not None and str(r[2]) == str(slot)
        ]
        if len(matches) == 1:
            log.info(
                "EXIT matched UID session using unique camera/slot "
                "vehicle_uid=%s slot=%s original_area=%s exit_area=%s",
                uid, slot, matches[0][4], area,
            )
            return matches[0]
        if len(matches) > 1:
            log.warning(
                "Ambiguous cross-area EXIT session; refusing match "
                "vehicle_uid=%s slot=%s", uid, slot
            )
        return None

    def _upsert_uid_session(
        self,
        event,
        first_seen,
        parked_at=None,
        status="OPEN",
        slot_id=None,
    ):
        uid = self._uid(event)

        if not uid or not first_seen:
            return False

        area = self._area(event)

        self._db.execute("""
            INSERT INTO parking_sessions_by_uid (
                vehicle_uid,
                camera_id,
                parking_area_key,
                parking_area_id,
                slot_id,
                first_seen,
                parked_at,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (
                vehicle_uid,
                camera_id,
                parking_area_key
            )
            DO UPDATE SET
                parking_area_id=excluded.parking_area_id,
                slot_id=COALESCE(
                    excluded.slot_id,
                    parking_sessions_by_uid.slot_id
                ),
                first_seen=excluded.first_seen,
                parked_at=excluded.parked_at,
                status=excluded.status
        """, (
            str(uid),
            self._camera(event),
            self._area_key(event),
            area,
            slot_id or self._slot(event),
            first_seen,
            parked_at,
            status,
        ))

        self._db.commit()
        return True

    def _upsert_plate_session(
        self,
        event,
        plate,
        first_seen,
        parked_at=None,
        status="OPEN",
        slot_id=None,
    ):
        if not plate or not first_seen:
            return False

        fingerprint = self._plate_fingerprint(plate)
        ciphertext = self._encrypted_plate(plate)

        self._db.execute("""
            INSERT INTO parking_sessions (
                plate_hmac,
                plate_ciphertext,
                camera_id,
                parking_area_key,
                parking_area_id,
                slot_id,
                first_seen,
                parked_at,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (
                plate_hmac,
                camera_id,
                parking_area_key
            )
            DO UPDATE SET
                plate_ciphertext=excluded.plate_ciphertext,
                parking_area_id=excluded.parking_area_id,
                slot_id=COALESCE(
                    excluded.slot_id,
                    parking_sessions.slot_id
                ),
                first_seen=excluded.first_seen,
                parked_at=excluded.parked_at,
                status=excluded.status
        """, (
            fingerprint,
            ciphertext,
            self._camera(event),
            self._area_key(event),
            self._area(event),
            slot_id or self._slot(event),
            first_seen,
            parked_at,
            status,
        ))

        self._db.commit()
        return True

    @staticmethod
    def _before(left, right):
        if not left or not right:
            return False
        try:
            return datetime.fromisoformat(left) < datetime.fromisoformat(right)
        except (TypeError, ValueError):
            return False

    def _remember_entry(self, event):
        plate = self._plate(event)
        first_seen = event.get("event_time") or event.get("timestamp")

        if not first_seen:
            log.warning(
                "Cannot remember ENTRY without timestamp event_id=%s",
                event.get("event_id"),
            )
            return

        existing = self._uid_session(event)
        if existing and (existing[0] == first_seen or self._before(first_seen, existing[0])):
            log.info("LIFECYCLE_ENTRY_REPLAY vehicle_uid=%s", self._uid(event))
            return

        # Vehicle UID preserves lifecycle tracking without OCR.
        self._upsert_uid_session(
            event,
            first_seen,
            status="OPEN",
        )

        if not plate:
            log.info(
                "ENTRY remembered without plate vehicle_uid=%s timestamp=%s",
                self._uid(event),
                first_seen,
            )
            return

        self._upsert_plate_session(
            event,
            plate,
            first_seen,
            status="OPEN",
            slot_id=self._slot(event),
        )

        log.info(
            "Lifecycle ENTRY remembered using encrypted plate storage "
            "first_seen=%s slot=%s",
            first_seen,
            self._slot(event),
        )

    def _remember_park_start(self, event):
        plate = self._plate(event)

        if not plate:
            log.warning(
                "Cannot remember PARK_START without plate event_id=%s",
                event.get("event_id"),
            )
            return None

        parked_at = event.get("event_time") or event.get("timestamp")
        fingerprint = self._plate_fingerprint(plate)

        row = self._db.execute("""
            SELECT first_seen
            FROM parking_sessions
            WHERE plate_hmac=?
              AND camera_id=?
              AND parking_area_key=?
              AND status='OPEN'
        """, (
            fingerprint,
            self._camera(event),
            self._area_key(event),
        )).fetchone()

        uid_row = self._uid_session(event)

        if row:
            first_seen = row[0]
        elif uid_row:
            first_seen = uid_row[0]
        else:
            first_seen = parked_at

        self._upsert_plate_session(
            event,
            plate,
            first_seen,
            parked_at=parked_at,
            status="PARKED",
            slot_id=self._slot(event),
        )

        self._upsert_uid_session(
            event,
            first_seen,
            parked_at=parked_at,
            status="PARKED",
            slot_id=self._slot(event),
        )

        log.info(
            "Lifecycle PARK_START remembered first_seen=%s "
            "captured_at=%s slot=%s",
            first_seen,
            parked_at,
            self._slot(event),
        )

        return first_seen, parked_at

    def _build_entry(self, event, first_seen, parked_at):
        out = dict(event)
        out["source_event_time"] = event.get("event_time") or first_seen
        out["event_type"] = "ENTRY"
        out["timestamp"] = first_seen
        out["captured_at"] = parked_at
        out["received_at"] = None
        return out

    def _build_exit(self, event, first_seen, parked_at):
        out = dict(event)
        leave_time = event.get("event_time") or event.get("timestamp")
        out["event_type"] = "EXIT"
        out["timestamp"] = first_seen
        out["captured_at"] = parked_at
        out["received_at"] = leave_time
        out["departure_at"] = leave_time
        return out

    def _publish_entry(self, event):
        uid = self._uid(event)
        event_id = event.get("event_id")
        observed_at = event.get("event_time") or event.get("timestamp")
        delivery_key = None
        if uid and event_id and observed_at:
            delivery_key = (self._camera(event), self._area_key(event), str(uid),
                            str(self._slot(event)), observed_at)
            with self._db:
                self._db.execute("""
                    INSERT OR IGNORE INTO park_start_delivery
                        (camera_id, area_key, vehicle_uid, slot_id, observed_at, event_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (*delivery_key, event_id))
                canonical_id, delivered = self._db.execute("""
                    SELECT event_id, delivered FROM park_start_delivery
                    WHERE camera_id=? AND area_key=? AND vehicle_uid=? AND slot_id=? AND observed_at=?
                """, delivery_key).fetchone()
            if delivered:
                log.info("LIFECYCLE_PARK_START_REPLAY event_id=%s", canonical_id)
                return
            event = {**event, "event_id": canonical_id}

        plate = self._plate(event)

        if plate:
            result = self._remember_park_start(event)
            if not result:
                return

            first_seen, parked_at = result

        else:
            parked_at = event.get("event_time") or event.get("timestamp")
            uid_row = self._uid_session(event)
            first_seen = uid_row[0] if uid_row else parked_at

            if not first_seen or not parked_at:
                log.warning(
                    "Cannot publish PARK_START without timestamp "
                    "event_id=%s",
                    event.get("event_id"),
                )
                return

            self._upsert_uid_session(
                event,
                first_seen,
                parked_at=parked_at,
                status="PARKED",
                slot_id=self._slot(event),
            )

        out = self._build_entry(event, first_seen, parked_at)
        self._inner.publish(out)
        if delivery_key is not None:
            with self._db:
                self._db.execute("""
                    UPDATE park_start_delivery SET delivered=1
                    WHERE camera_id=? AND area_key=? AND vehicle_uid=? AND slot_id=? AND observed_at=?
                """, delivery_key)


        log.info(
            "S2 ENTRY published vehicle_uid=%s timestamp=%s captured_at=%s",
            self._uid(out),
            out.get("timestamp"),
            out.get("captured_at"),
        )

    def _publish_exit(self, event):
        plate = self._plate(event)
        camera = self._camera(event)
        area_key = self._area_key(event)
        slot = self._slot(event)

        row = None
        fingerprint = self._plate_fingerprint(plate) if plate else None

        if fingerprint:
            row = self._db.execute("""
                SELECT first_seen, parked_at, slot_id
                FROM parking_sessions
                WHERE plate_hmac=?
                  AND camera_id=?
                  AND parking_area_key=?
                  AND status='PARKED'
            """, (
                fingerprint,
                camera,
                area_key,
            )).fetchone()

        uid_row = self._uid_session_for_exit(event, status="PARKED")
        uid_area_key = area_key

        if (row and uid_row and row != uid_row[:3]) or any(
            str(candidate[2]) != str(slot)
            for candidate in (row, uid_row) if candidate and candidate[2] and slot
        ):
            log.warning("LIFECYCLE_EXIT_ASSOCIATION_CONFLICT event_id=%s vehicle_uid=%s",
                        event.get("event_id"), self._uid(event))
            return

        if row:
            first_seen, parked_at, stored_slot = row
        elif uid_row:
            (
                first_seen, parked_at, stored_slot,
                _status, uid_area_key
            ) = uid_row
        else:
            log.warning(
                "EXIT has no stored PARKED session "
                "vehicle_uid=%s slot=%s event_id=%s",
                self._uid(event),
                slot,
                event.get("event_id"),
            )
            return

        try:
            departure = datetime.fromisoformat(event.get("event_time") or event.get("timestamp"))
            parking_start = datetime.fromisoformat(parked_at)
            if departure.utcoffset() is None or parking_start.utcoffset() is None:
                raise ValueError("parking times require timezone offsets")
        except (TypeError, ValueError):
            log.warning("LIFECYCLE_INVALID_EXIT_TIME event_id=%s", event.get("event_id"))
            return

        if departure < parking_start:
            log.warning("LIFECYCLE_EXIT_BEFORE_PARK_START event_id=%s vehicle_uid=%s",
                        event.get("event_id"), self._uid(event))
            return

        received_at = event.get("detected_at") or event.get("timestamp")

        if parked_at and received_at and received_at < parked_at:
            log.warning(
                "EXIT received_at is earlier than captured_at "
                "captured_at=%s received_at=%s",
                parked_at,
                received_at,
            )

        out = self._build_exit(event, first_seen, parked_at)
        self._inner.publish(out)

        if fingerprint:
            self._db.execute("""
                UPDATE parking_sessions
                SET status='CLOSED'
                WHERE plate_hmac=?
                  AND camera_id=?
                  AND parking_area_key=?
                  AND status='PARKED'
            """, (
                fingerprint,
                camera,
                area_key,
            ))

        if self._uid(event):
            self._db.execute("""
                UPDATE parking_sessions_by_uid
                SET status='CLOSED'
                WHERE vehicle_uid=?
                  AND camera_id=?
                  AND parking_area_key=?
                  AND status='PARKED'
            """, (
                str(self._uid(event)),
                camera,
                uid_area_key,
            ))

        self._db.commit()

        log.info(
            "S2 EXIT published vehicle_uid=%s timestamp=%s "
            "captured_at=%s received_at=%s slot=%s",
            self._uid(event),
            out.get("timestamp"),
            out.get("captured_at"),
            out.get("received_at"),
            stored_slot,
        )

    def publish(self, event):
        with self._publish_lock:
            self._publish(event)

    def _publish(self, event):
        status = event.get("plate_status") or (event.get("plate") or {}).get("status")
        if status is not None and str(status).upper() != "CONFIRMED":
            event = self._safe_report_event(event)
        etype = event.get("event_type")

        if etype in {"ENTRY", "PARK_START", "PARK_END", "EXIT"}:
            self.raw_events.append(self._safe_report_event(event))
            log.info("LIFECYCLE_GENERATED event_id=%s type=%s camera=%s",
                     event.get("event_id"), etype, event.get("camera_id"))

        if etype == "ENTRY":
            self._remember_entry(event)
            return

        if etype == "PARK_START":
            if not self._slot(event):
                log.warning(
                    "PARK_START missing parking.slot_id event_id=%s",
                    event.get("event_id"),
                )
                return

            self._publish_entry(event)
            return

        if etype == "VEHICLE_UPDATE":
            plate = self._plate(event)
            uid_row = self._uid_session(event)

            if plate and uid_row:
                first_seen, parked_at, slot_id, status = uid_row

                self._upsert_plate_session(
                    event,
                    plate,
                    first_seen,
                    parked_at=parked_at,
                    status=(
                        status if status in ("OPEN", "PARKED")
                        else "CLOSED"
                    ),
                    slot_id=slot_id,
                )

            log.info(
                "Late plate update associated locally "
                "vehicle_uid=%s; original timestamps retained",
                self._uid(event),
            )
            return

        if etype == "PARK_END":
            if not self._slot(event):
                log.warning(
                    "PARK_END missing parking.slot_id event_id=%s",
                    event.get("event_id"),
                )
                return

            self._publish_exit(event)
            return

        log.info(
            "Internal event not sent to Service 2: %s event_id=%s",
            etype,
            event.get("event_id"),
        )

    def stop(self, wait=True, timeout=5.0):
        self._db.close()
        return self._inner.stop(wait=wait, timeout=timeout)

    def __getattr__(self, name):
        return getattr(self._inner, name)
