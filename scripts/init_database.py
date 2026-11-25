"""Initialize or check the PostgreSQL schema used by Parking & Bay Management (S2)."""
import argparse
import hashlib
import os
import shutil
import sys
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

MIGRATIONS = Path(__file__).resolve().parents[1] / "parking_management" / "migrations"
TABLES = ("parking_areas", "event_types", "vehicle_types", "vehicle_colors", "cameras",
          "parking_slots", "vehicles", "parking_slot_state", "parking_events", "parking_sessions")


def verify(db, migrations):
    applied = dict(db.execute("SELECT version, sha256 FROM smartgarage_schema_migrations"))
    if applied != {name: checksum for name, _, checksum in migrations}:
        raise ValueError("Migration ledger differs from the checked-in migrations.")
    keys = dict(db.execute("""
        SELECT t.relname, count(*) FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'public' AND c.contype = 'p' AND t.relname = ANY(%s)
        GROUP BY t.relname
    """, (list(TABLES),)).fetchall())
    if keys != dict.fromkeys(TABLES, 1):
        raise ValueError("One or more managed tables or primary keys are missing.")
    foreign_keys = dict(db.execute("""
        SELECT t.relname, count(*) FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'public' AND c.contype = 'f' AND c.convalidated
        AND t.relname = ANY(%s) GROUP BY t.relname
    """, (list(TABLES),)).fetchall())
    if foreign_keys != {"cameras": 2, "parking_slots": 1, "vehicles": 2,
                        "parking_slot_state": 3, "parking_events": 6, "parking_sessions": 4}:
        raise ValueError("Managed foreign keys differ from the baseline.")
    active = db.execute("""
        SELECT indexdef FROM pg_indexes WHERE schemaname = 'public'
            AND tablename = 'parking_sessions' AND indexname = 'parking_sessions_active_uid'
    """).fetchone()
    if not active or not all(part in active[0] for part in (
        "CREATE UNIQUE INDEX", "(parking_area_id, camera_id, vehicle_uid)",
        "vehicle_uid IS NOT NULL", "status = 'parked'"
    )):
        raise ValueError("The active parking session uniqueness index is missing or incompatible.")
    indexes = {name for (name,) in db.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")}
    required = {"parking_sessions_vehicle_history", "parking_events_vehicle_history",
                "parking_events_type_time", "parking_events_retry_lookup", "parking_slot_state_vehicle",
                "parking_events_sender_event_id", "parking_sessions_active_slot"}
    if not required <= indexes:
        raise ValueError("One or more managed lookup indexes are missing.")
    unique_keys = {row[0] for row in db.execute("""
        SELECT table_name FROM information_schema.table_constraints
        WHERE table_schema = 'public' AND constraint_type = 'UNIQUE'
    """)}
    if not {"parking_areas", "event_types", "vehicle_types", "vehicle_colors", "vehicles"} <= unique_keys:
        raise ValueError("One or more managed reference or plate uniqueness constraints are missing.")
    codes = {row[0] for row in db.execute("SELECT code FROM event_types WHERE code IN ('ENTRY', 'EXIT')")}
    if codes != {"ENTRY", "EXIT"}:
        raise ValueError("Required ENTRY and EXIT reference rows are missing.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Read-only schema verification")
    parser.add_argument("--create-database", action="store_true", help="Create the target if missing via postgres")
    parser.add_argument("--area", action="append", default=[], help="Register a configured parking area code")
    parser.add_argument("--vehicle-type", action="append", default=[], help="Register an allowed detector label")
    parser.add_argument("--color", action="append", default=[], help="Register an allowed color")
    parser.add_argument("--config", type=Path, help="Register area codes and detector labels from a local S1 YAML")
    args = parser.parse_args()
    if args.check and (args.create_database or args.area or args.vehicle_type or args.color or args.config):
        parser.error("--check cannot be combined with write options")
    try:
        try:
            url = make_url(os.environ.get("DATABASE_URL", ""))
        except ArgumentError:
            raise ValueError("DATABASE_URL is missing or malformed.") from None
        cameras = []
        if args.config:
            sys.path.insert(0, str(MIGRATIONS.parents[1]))
            from vehicle_intelligence.common.config import camera_configs, load_config, validate_geometry_for_camera
            from vehicle_intelligence.common.errors import ConfigurationError
            from vehicle_intelligence.geometry import Geometry

            try:
                config = load_config(args.config)
                for camera in camera_configs(config):
                    area = camera["parking_area_id"]
                    if not isinstance(area, str) or not area.strip():
                        raise ValueError("Configure a parking area code for every camera.")
                    geometry = Geometry.load(camera["geometry_file"])
                    validate_geometry_for_camera(camera, geometry)
                    cameras.append((camera["camera_id"], area, geometry.slots))
                    args.area.append(area)
                    args.vehicle_type.extend(camera["detector"]["classes"].values())
            except (OSError, KeyError, AttributeError, TypeError, ConfigurationError) as exc:
                raise ValueError("Could not validate the S1 configuration or its geometry.") from exc
        if url.drivername != "postgresql+psycopg" or not url.database:
            raise ValueError("Set DATABASE_URL to postgresql+psycopg://... with an explicit database.")
        params = url.translate_connect_args(username="user", database="dbname")
        if set(params) & set(url.query):
            raise ValueError("Do not repeat connection fields in DATABASE_URL query parameters.")
        params.update(url.query)
        params.setdefault("connect_timeout", 5)
        if args.create_database:
            if url.database in {"postgres", "template0", "template1"} or "dbname" in url.query:
                raise ValueError("Select a distinct application database.")
            with psycopg.connect(**{**params, "dbname": "postgres"}, autocommit=True) as admin:
                if not admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (url.database,)).fetchone():
                    try:
                        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(url.database)))
                    except psycopg.errors.DuplicateDatabase:
                        pass
        migrations = []
        for path in sorted(MIGRATIONS.glob("*.sql")):
            content = path.read_bytes()
            migrations.append((path.name, content.decode("utf-8"), hashlib.sha256(content).hexdigest()))
        if not migrations:
            raise ValueError("No SQL migrations were found.")
        with psycopg.connect(**params) as db:
            if args.check:
                db.execute("SET TRANSACTION READ ONLY")
            if db.execute("SHOW server_encoding").fetchone()[0] != "UTF8":
                raise ValueError("Use a UTF-8 PostgreSQL database.")
            db.execute("SET LOCAL search_path TO public")
            if not args.check:
                db.execute("SELECT pg_advisory_xact_lock(hashtext('smartgarage-schema'))")
                if not db.execute("SELECT to_regclass('public.smartgarage_schema_migrations')").fetchone()[0]:
                    existing = [name for name in TABLES if db.execute(
                        "SELECT to_regclass(%s)", ("public." + name,)).fetchone()[0]]
                    if existing:
                        raise ValueError("Unmanaged parking tables found. Use a fresh database or review the existing schema before adoption.")
                    db.execute("""CREATE TABLE smartgarage_schema_migrations (
                        version text PRIMARY KEY, sha256 text NOT NULL,
                        applied_at timestamptz NOT NULL DEFAULT now())""")
                applied = dict(db.execute("SELECT version, sha256 FROM smartgarage_schema_migrations"))
                expected = [(name, digest) for name, _, digest in migrations]
                if applied != dict(expected[:len(applied)]):
                    raise ValueError("Unknown, edited or out-of-order migrations in the database.")
                for name, content, checksum in migrations[len(applied):]:
                    db.execute(content)
                    db.execute("INSERT INTO smartgarage_schema_migrations VALUES (%s, %s)", (name, checksum))
            verify(db, migrations)
            for table, column, values in (
                ("parking_areas", "area_code", args.area),
                ("vehicle_types", "code", args.vehicle_type),
                ("vehicle_colors", "code", args.color),
            ):
                for value in values:
                    if not isinstance(value, str) or not value.strip() or value != value.strip():
                        raise ValueError("Reference codes must be non-empty and have no surrounding spaces.")
                    db.execute(sql.SQL("INSERT INTO {} ({}) VALUES (%s) ON CONFLICT DO NOTHING").format(
                        sql.Identifier(table), sql.Identifier(column)), (value,))
            for camera, area, slots in cameras:
                db.execute("""INSERT INTO cameras (camera_id, parking_area_id, parking_area_fk, name)
                    SELECT %s, area_code, id, %s FROM parking_areas WHERE area_code = %s
                    ON CONFLICT DO NOTHING""", (camera, camera, area))
                if db.execute("SELECT parking_area_id FROM cameras WHERE camera_id = %s", (camera,)).fetchone() != (area,):
                    raise ValueError("A configured camera already belongs to a different parking area.")
                for slot in slots:
                    sid = slot["slot_id"]
                    db.execute("""INSERT INTO parking_slots (camera_id, slot_id, polygon)
                        VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""", (camera, sid, Jsonb(slot["polygon"])))
                    db.execute("""INSERT INTO parking_slot_state (camera_id, slot_id, parking_area_id, status)
                        VALUES (%s, %s, %s, 'available') ON CONFLICT DO NOTHING""", (camera, sid, area))
        print("Parking & Bay Management database schema verified.")
    except ValueError as exc:
        parser.exit(1, f"Database setup failed. {exc}\n")
    except psycopg.Error as exc:
        message = str(exc).lower()
        if exc.sqlstate in {"28P01", "28000"} or "password authentication failed" in message:
            action = "PostgreSQL rejected the login. Check the role, password and host authentication."
        elif exc.sqlstate == "42501":
            action = "Grant the development login database/schema privileges or have an administrator provision it."
        elif exc.sqlstate == "3D000" or ("database" in message and "does not exist" in message):
            action = "The target database is missing. Provision it or rerun with --create-database."
        elif isinstance(exc, psycopg.OperationalError):
            tools = "found on PATH" if shutil.which("psql") or shutil.which("pg_ctl") else "not found on PATH"
            action = (f"PostgreSQL connection failed at the configured endpoint. Client/server tools are {tools}. "
                      "Check the selected service, configured host and port, and network access. "
                      "A failed connection cannot establish whether a server is installed elsewhere.")
        else:
            action = "Check schema privileges, migration prerequisites and the migration ledger."
        parser.exit(1, f"Database setup failed ({type(exc).__name__}, SQLSTATE "
                    f"{exc.sqlstate or 'unavailable'}). {action}\n")


if __name__ == "__main__":
    main()
