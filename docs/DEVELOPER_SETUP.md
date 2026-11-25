# SmartGarage developer setup

See the [Windows runbook](WINDOWS_RUNBOOK.md) for database-free preview, PostgreSQL provisioning, and connected replay.

## Automated execution

Run from the repository root. The following commands require Python 3.12 and sufficient storage for CPU packages. On Linux, use `.venv/bin/python` in place of `.\.venv\Scripts\python.exe`.

### Quick preview

```powershell
python scripts/setup.py --mode preview --cpu-torch --config .venv/dev-preview.yaml --preview-source TEST_REPORTS/raw/http/cancelled-output.mp4
.\.venv\Scripts\python.exe scripts/run.py --mode preview --config .venv/dev-preview.yaml
```

Use your calibrated multicamera YAML for meaningful site results. The fixture checks decoding, inference startup and output generation. PostgreSQL is optional in preview. Use the configured ANPR assets and provide development encryption and HMAC settings for plate-linked evidence or restored sessions. The runner prints a unique output directory and checks every slot in the JSON and decodes the MP4.

### Full system

Start a local PostgreSQL instance and export the development credentials below. Provide a calibrated YAML with a `cameras` list and local video files. Footage must show parking and departure from the same session. Relative paths resolve from the repository root. The runner copies the effective config into the output directory and launches services on kernel-assigned loopback ports. Use a dedicated database. An existing database must already use the managed schema.

```powershell
python scripts/setup.py --mode full --cpu-torch --config dev.yaml --create-database --smoke
.\.venv\Scripts\python.exe scripts/run.py --mode full --config dev.yaml
```

The runner creates or validates the PostgreSQL database, starts both APIs, publishes through the real mapper/outbox, then reads PostgreSQL directly. A pass requires matching mapped ENTRY and EXIT rows, a closed session of positive duration, and consistent final occupancy for the affected slots. Zero matched lifecycle events is a failed integration run even if video outputs are valid. The optional setup smoke uses **controlled events** in a separate disposable database. Its pass never substitutes for the recording check.

Setup installs packages only with `--install` or `--cpu-torch`. For a read-only schema/preflight check, use `python scripts/setup.py --mode full --check --config dev.yaml`. Repeat preview setup with the existing YAML and omit `--preview-source`. Setup does not install or start the PostgreSQL server. See the [Windows runbook](WINDOWS_RUNBOOK.md#full-integration-prerequisites) for discovery, service startup and dedicated role creation. If a configured endpoint is unreachable, verify the actual instance and port. Client tools missing from PATH do not prove PostgreSQL is absent.

## Service boundaries

The **Vehicle Intelligence Service** (`vehicle_intelligence`) decodes video, detects and tracks vehicles, and evaluates camera-side occupancy and lifecycle against calibrated geometry. OCR and cross-camera ReID are optional. It produces reports and annotated video and offers a processing API. Its publishing path uses the existing SQLite outbox and S2 mapper.

The **Parking & Bay Management Service** (`parking_management`) receives authenticated HTTP messages at `POST /internal/events/vehicle`. It persists events, current occupancy, vehicle records, and parking sessions in PostgreSQL. It serves occupancy, analytics, vehicle lookup, and snapshot metadata. The API also exposes a parking WebSocket. Keep both development listeners on loopback.

## Prerequisites and environment

Use Python 3.10 or newer. Python 3.12 is the reference environment for the CPU workflow. Install PostgreSQL with UTF-8 encoding and provision a development login. Install FFmpeg for metadata and H.264 output where supported. A fresh deployment needs authorized recordings or camera access and matching calibration. The repository provides the default detector weight. Configure the actual detector path if that weight is unavailable. ANPR requires its detector and OCR assets, and OSNet requires the configured ReID model. For GPU provisioning, use the platform-specific instructions in the [installation guide](INSTALLATION_GUIDE.md).

Export the variables in [the template](../.env.example) into the process environment in each service terminal. Use `export NAME='value'` in Bash or `$env:NAME = 'value'` in PowerShell. The setup script reads process variables. Keep credentials outside Git and share the same development encryption and HMAC settings between both services.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL SQLAlchemy URL of the form `postgresql+psycopg://USER@HOST:PORT/DATABASE`. Use a password or platform authentication as required. URL-encode special characters in credentials. |
| `S2_API_KEY` | Local shared API credential. The outbox sends it as `X-API-Key`. |
| `SP_ENCRYPTION_KEYS`, `SP_ACTIVE_ENCRYPTION_KEY` | JSON map of key IDs to base64-encoded 32-byte keys and the ID of the active key. |
| `SP_HMAC_KEY` | Base64-encoded 32-byte fingerprinting key. S1 requires this variable. S2 also supports its own rotating `SP_HMAC_KEYS` and `SP_ACTIVE_HMAC_KEY`. |

For a disposable local environment, generate credentials once and reuse the same values in both terminals. For example, run the following in Bash, then start each service from that environment. Supply your actual development database URL separately.

```bash
export S2_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export SP_ACTIVE_ENCRYPTION_KEY=dev
export SP_ENCRYPTION_KEYS="$(python -c 'import base64,json,secrets; print(json.dumps(dict(dev=base64.b64encode(secrets.token_bytes(32)).decode())))')"
export SP_HMAC_KEY="$(python -c 'import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())')"
```

On PowerShell, assign each command's output with `$env:NAME = python -c '...'`. Keep the keys stable when restarting an environment with existing encrypted records.

## Install and initialize

From the clone root, use a fresh development database and a new output directory. For a first run, this command installs dependencies before generating a separate preview YAML from the bundled short clip:

```bash
python scripts/setup.py --cpu-torch --config .venv/dev-preview.yaml --preview-source TEST_REPORTS/raw/http/cancelled-output.mp4 --create-database
```

Run the same command from PowerShell. For an existing authorized, calibrated development YAML, omit `--preview-source` and pass its path to `--config`. On repeat setup, reuse the generated YAML without `--preview-source`. The checked-in site config contains workstation-specific sources and a remote publisher destination. The script creates or reuses `.venv`, installs both services, runs the S1 resource preflight and initializes PostgreSQL. `--cpu-torch` selects CPU wheels on Linux or Windows. On macOS, or with a supported GPU environment, omit that option and provision the compatible PyTorch build. Existing virtual environments can have their dependencies updated by this command.

If installation reports `No space left on device`, check both the clone volume and the operating system’s temporary directory. CPU PyTorch must fit alongside its unpacked files and the rest of the dependencies. On Linux, use `df -h . "${TMPDIR:-/tmp}"`. On PowerShell, use `Get-PSDrive -PSProvider FileSystem` and check the drive containing `$env:TEMP`. Move to a larger writable development volume or set `TMPDIR` on Linux or `TEMP` and `TMP` on Windows to an existing writable directory on a larger volume. Retry in a fresh clone if the failed install left a partial environment. Setup reports the failed stage and available space when installation or environment creation fails.

`--create-database` uses the PostgreSQL `postgres` maintenance database and needs `CREATEDB` if the target is absent. If permissions prevent creation, have the administrator create a fresh UTF-8 database owned by the development login, then omit the flag. The target needs permission to create objects in `public`. The application and initializer should use the normal `public` schema search path.

Activate the environment with `source .venv/bin/activate` on Bash or `.\.venv\Scripts\Activate.ps1` on PowerShell. Run setup commands from the repository root so relative model and geometry paths resolve consistently.

```bash
python scripts/init_database.py --config /path/to/dev.yaml
python scripts/init_database.py --check
```

The initializer applies versioned SQL transactionally, records migration checksums, and verifies the managed tables, primary and foreign keys, active-session uniqueness index and event types. Supplying a YAML provisions its area codes, detector labels, cameras and slots using the existing configuration and geometry validators. Initial slots are marked available pending observations. Existing rows, geometry and occupancy are preserved. Prefer the authenticated geometry API for updating existing polygons. The CLI also accepts `--area`, `--vehicle-type`, and `--color` for explicit reference codes.

### Schema and compatibility

The baseline declares area, vehicle type, color and event type reference tables, camera and slot definitions, vehicles, slot state, sessions and events. Slot identity uses camera plus slot ID. Vehicles may be unresolved for unconfirmed plates, so nullable vehicle foreign keys coexist with the event UID. Legacy text and observed snapshot fields retain the values the current API reads or writes. The occupancy table is a mutable projection, while events and sessions describe history. This avoids a cross-service contract rewrite to remove compatibility fields.

Existing databases with managed-table names and no migration ledger are refused. No adoption or in-place conversion is implied. Back up the database and review its actual schema before designing a migration for it. On failure, inspect the database name, encoding, reachability, permissions and migration ledger. Applied SQL is checksum-protected, so add a new migration and extend verification for any future schema change.

## Start and connect the services

Run the commands from the clone root. Update external launch configurations to use the package paths below. The S1 and S2 labels, environment variables, HTTP routes and event payloads keep their existing meanings.

Start Parking & Bay Management Service first from the root of the clone:

```bash
python -m uvicorn parking_management.app.main:app --host 127.0.0.1 --port 8001
```

Check `http://127.0.0.1:8001/health/database` for `database: connected`. Check the JSON body because the existing endpoint also returns HTTP 200 when it reports `database: disconnected`. Swagger is at `/docs` and the schema at `/openapi.json`.

For Vehicle Intelligence HTTP preview, set `SMART_PARKING_CONFIG` to the development YAML path and `SMART_PARKING_API_PUBLISH=0`, then run:

```bash
python -m uvicorn vehicle_intelligence.api:app --host 127.0.0.1 --port 8000
```

Check `/health` and `/docs` on port 8000. See the [S1 HTTP reference](API_SWAGGER.md). For a terminal video run with local previews:

```bash
python -m vehicle_intelligence.main --config /path/to/dev.yaml --no-publisher
```

For a connected site replay, explicitly set `publisher.s2_url` in the **development YAML** to `http://127.0.0.1:8001/internal/events/vehicle`, with fresh local outbox, snapshot and report paths. Confirm the destination is the local development instance before running `python main.py --config /path/to/dev.yaml` with publishing enabled. The mapper uses `PARK_START` for S2 ENTRY and `PARK_END` for S2 EXIT. Detection alone does not imply either event. The restored EXIT-only workflow requires local preview mode. Use a recording that establishes its own parking session for a publishing-enabled replay.

Stop both servers with Ctrl+C. Inspect S1's `logs/` and the configured report and video paths. After failure, verify the configuration and database with preflight and `--check`; preserve the outbox and database when diagnosing startup.

## Verify on isolated resources

Use the [quick-test command](TESTING.md) for API, Swagger, controlled PostgreSQL delivery, sample preview and regressions. See [database setup](DATABASE.md) for schema relationships and role requirements.

```bash
python scripts/smoke_database.py
python -m pytest tests -q
DATABASE_URL=sqlite:///isolated-test.db python -m pytest parking_management/tests -q
```

On PowerShell, use a separate test terminal at the clone root. Set `$env:DATABASE_URL = "sqlite:///isolated-test.db"` before running `python -m pytest parking_management/tests -q`. The bundled S2 suite uses mocked persistence. The PostgreSQL smoke separately starts a real loopback Parking & Bay Management listener and exercises S1 mapping, encrypted outbox delivery, API auth, database events, session closure and slot release. It uses generated credentials and its own uniquely named disposable database on the localhost PostgreSQL server. Use a local development login with `CREATEDB`. The database is retained for inspection. The smoke injects lifecycle input and does not exercise camera inference.

Mapped EXIT now includes optional, timezone-aware `departure_at` from the original PARK_END observation. Existing `timestamp`, `captured_at`, and `received_at` fields are preserved for consumers. Parking Management uses departure time for newly mapped EXIT events. Older producers without `departure_at` retain the original fallback. Existing history is left intact. For video replay, timestamps depend on the source clock and replay anchor. Use a calibrated continuous recording and avoid interpreting a restored preview session as a measured connected dwell time. See [end-to-end processing](END_TO_END.md) and the [API reference](API_SWAGGER.md).


## Future commits

Use descriptive `type(scope): subject` messages, such as `test(integration): verify PostgreSQL slot release`. Preserve existing history and review the exact diff before committing. Keep credentials, recordings, models, databases and generated reports out of new commits.

### Delivery integrity and schema upgrade

Run `python scripts/init_database.py` before starting the upgraded Parking Management service. Migration `002_delivery_integrity.sql` adds a nullable sender event ID, a unique index on populated sender IDs, and a unique index for active camera-slot sessions. Migration `001_baseline.sql` remains byte-identical. If historical data already violates the new active-slot constraint, migration stops and rolls back. Review the conflicting rows before proceeding.

A sender ID and PostgreSQL event ID serve separate purposes. An exact replay of a known sender ID returns `duplicate` with the original stored ID. Reusing the sender ID for a conflicting event returns 409. Legacy requests without sender IDs retain semantic duplicate detection.

The outbox accepts only an `accepted` or `duplicate` acknowledgement with a stored event ID and matching event type. It persists that receipt. HTTP 409 is a retryable conflict. Requests use bounded exponential backoff with up to eight failed attempts by default. Invalid 400/405/415/422 requests and unreadable encrypted rows go straight to retained dead-letter state. Failed rows keep their ciphertext, attempt count, reason and failure time. Reconcile the underlying error and destination records before recovery. Keep the SQLite artifact and encryption keys together.

Full-mode output includes `outbox-verification.json` for queued, acknowledged, pending and permanently failed counts. `database-verification.json` reads back acknowledged event IDs, lifecycle sessions and slot state directly from PostgreSQL. Preview does not send events.

Run the real HTTP/PostgreSQL controlled smoke test with:

```bash
python scripts/smoke_database.py
```

It checks authentication, persisted acknowledgements, duplicates, an out-of-order EXIT rollback, competing claims, duration and slot release using test data. It does not replace the real recorded-video acceptance run.
