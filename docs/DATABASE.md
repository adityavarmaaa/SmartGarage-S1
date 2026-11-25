# PostgreSQL setup and schema

Use a local development instance and an explicit `postgresql+psycopg://` URL in `DATABASE_URL`. PostgreSQL 17 is the verified reference instance. Keep credentials in the process environment. See [developer setup](DEVELOPER_SETUP.md#prerequisites-and-environment) and the [Windows runbook](WINDOWS_RUNBOOK.md#full-integration-prerequisites) for server and role setup. Preview uses no PostgreSQL. Full mode requires PostgreSQL and verified service acknowledgements.

From a prepared virtual environment and the repository root, start a local PostgreSQL server using its native platform service manager. Provision a dedicated development login with `CREATEDB` if the application database is missing, or have an administrator create the database for that role. With that database selected in `DATABASE_URL`:

```text
python scripts/init_database.py --config <development-yaml> --create-database
python scripts/init_database.py --check
python scripts/smoke_database.py
```

Omit `--create-database` with an already provisioned database if the role cannot access the maintenance database. The initializer applies checked-in SQL and records checksums transactionally. It preserves existing data and stops at an unmanaged schema or incompatible migration. The smoke creates its own disposable database on the same local instance and verifies accepted and rejected transactions with read-back. It therefore needs `CREATEDB` even when the application's own database already exists. No automatic database cleanup is performed.

| Tables | Purpose and key relationships |
|---|---|
| `parking_areas`, `cameras`, `parking_slots` | Configuration hierarchy. Slots use a composite camera/slot key. |
| `vehicles`, `vehicle_types`, `vehicle_colors` | Plate-backed identities and reference attributes. Plate lookup uses HMAC, with encryption for protected values. |
| `parking_slot_state` | Current occupancy for each configured camera-slot. |
| `parking_sessions` | Historical sessions and current active claims. Partial unique indexes guard active camera-slot and camera/area/UID claims. |
| `parking_events`, `event_types` | Mapped event history. A unique non-null sender ID supports idempotency, and acknowledgements reference the stored row ID. |
| `smartgarage_schema_migrations` | Migration filenames, checksums and application times. |

The camera/slot, vehicle and reference relationships use foreign keys. Compatibility columns and observed snapshots intentionally repeat some text values. This is not strict third normal form throughout. Raw gate observations and cross-camera groupings are report data. See the [lifecycle contract](END_TO_END.md#observations-identity-and-persisted-events) and [upgrade and recovery notes](DEVELOPER_SETUP.md#delivery-integrity-and-schema-upgrade).
