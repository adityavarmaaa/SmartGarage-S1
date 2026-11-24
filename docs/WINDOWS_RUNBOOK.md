# SmartGarage on Windows

Run these commands from the clone root in VS Code PowerShell. Install Python 3.12 and FFmpeg first. Use an authorized recording and its matching camera geometry for parking validation. The bundled clip checks the software and output path. Its observations are not evidence of a complete parking session.

## Quick preview

Create a fresh run directory for each execution. Preview uses local files and SQLite preview state. It does not connect to PostgreSQL or send events to Parking Management. The generated configuration uses CPU inference and disables OCR for the bundled smoke clip. For a calibrated site configuration, retain its OCR settings and provision the configured models. If preview uses plate encryption or restores plate-linked sessions, export the same encryption and HMAC settings documented in the [developer guide](DEVELOPER_SETUP.md).

```powershell
$run = Join-Path 'runs' ([guid]::NewGuid().ToString('N'))
$config = Join-Path $run 'preview.yaml'
python scripts/setup.py --mode preview --cpu-torch --config $config --preview-source TEST_REPORTS/raw/http/cancelled-output.mp4
if ($LASTEXITCODE -ne 0) { throw 'Preview setup failed' }

$py = '.\.venv\Scripts\python.exe'
& $py -m vehicle_intelligence.main --config $config --no-publisher
if ($LASTEXITCODE -ne 0) { throw 'Preview processing failed' }
Get-Item (Join-Path $run 'annotated.mp4'), (Join-Path $run 'report.json')
```

The JSON reports processing status, camera metrics, configured slots, observations, and any lifecycle events. CLI logs are written under the current directory's `logs/`. A previous generated YAML may have `demo_video.enabled: false`. The fresh run command above leaves existing YAML and outputs intact.

## Full integration prerequisites

An absent TCP listener prevents PostgreSQL connections regardless of project setup. If PostgreSQL is not installed, use the [official Windows download](https://www.postgresql.org/download/windows/). During installation, note the configured port and create an administrative password. Then check the selected instance.

```powershell
Get-Command psql -ErrorAction SilentlyContinue
Get-CimInstance Win32_Service | Where-Object {
    $_.Name -match 'postgres' -or $_.PathName -match 'postgres'
} | Select-Object Name, State, PathName
Test-NetConnection 127.0.0.1 -Port 5432
```

If the selected service is stopped, run `Start-Service -Name 'ACTUAL_SERVICE_NAME'` in elevated PowerShell. Use the actual port if the installation uses a different one. When `psql` is off PATH, use its executable under the selected installation’s `bin` directory. For a disposable local database, an administrator can create a dedicated development login with PostgreSQL's `createuser.exe --host 127.0.0.1 --port 5432 --username postgres --pwprompt --createdb smartgarage_dev`. Run it only when that role does not already exist. The prompt supplies the role password. An administrator can instead provision a database owned by the development login and omit `--create-database` from setup.

Export `DATABASE_URL` as a `postgresql+psycopg://` URL with an explicit database and URL-encoded credentials. Export `S2_API_KEY`, `SP_ACTIVE_ENCRYPTION_KEY`, `SP_ENCRYPTION_KEYS`, and `SP_HMAC_KEY` using the [developer guide](DEVELOPER_SETUP.md#prerequisites-and-environment). Use matching values in each service terminal. Setup, the initializer, and publishing credentials use the process environment. The Parking Management database settings also support a working-directory `.env`, with process variables taking precedence. Setup leaves `.env.example` and any existing `.env` untouched.

## Full system

Use an authorized development YAML with a matching recording and calibration. Set `publisher.s2_url` to `http://127.0.0.1:8001/internal/events/vehicle`. Use fresh local outbox, report and MP4 paths. Enable `demo_video` and use footage that shows the same vehicle establishing and closing its parking session. Restored EXIT sessions are local-preview only. A generated smoke preview YAML has an isolated publisher URL and is unsuitable for this run.

```powershell
$config = Read-Host 'Path to the calibrated development YAML'
python scripts/setup.py --mode full --cpu-torch --config $config --create-database
if ($LASTEXITCODE -ne 0) { throw 'Full setup failed' }
$py = '.\.venv\Scripts\python.exe'
& $py scripts/init_database.py --check
if ($LASTEXITCODE -ne 0) { throw "Database schema check failed" }
& $py scripts/smoke_database.py
if ($LASTEXITCODE -ne 0) { throw "PostgreSQL smoke failed" }
& $py -m uvicorn parking_management.app.main:app --host 127.0.0.1 --port 8001
```

In another terminal with the same keys, check database health and run Vehicle Intelligence in the foreground. The CLI starts the vehicle processing service for this batch. Swagger for Parking Management is at `http://127.0.0.1:8001/docs`.

```powershell
$health = Invoke-RestMethod 'http://127.0.0.1:8001/health/database'
if ($health.database -ne 'connected') { throw 'Parking database is not connected' }
$config = Read-Host 'Path to the same calibrated development YAML'
$py = '.\.venv\Scripts\python.exe'
& $py -m vehicle_intelligence.main --config $config
if ($LASTEXITCODE -ne 0) { throw 'Video processing failed' }
Invoke-RestMethod 'http://127.0.0.1:8001/api/occupancy'
```

With the PostgreSQL client connected to the same development database, run these read-only checks and match the rows to the time, camera and slots in your report.

```sql
SELECT camera_id, slot_id, event_type, timestamp, captured_at
FROM parking_events ORDER BY id DESC LIMIT 20;
SELECT camera_id, slot_id, status, entry_time, exit_time, duration_seconds
FROM parking_sessions ORDER BY id DESC LIMIT 20;
SELECT camera_id, slot_id, status FROM parking_slot_state ORDER BY camera_id, slot_id;
```

The smoke test sends controlled lifecycle events into a disposable PostgreSQL database. A passing smoke test verifies duration for its controlled timestamps. Use the [automated full runner](DEVELOPER_SETUP.md#full-system) to verify duration and persisted evidence for your recording. Legacy producers that omit the explicit departure timestamp retain their previous behavior. See [developer setup](DEVELOPER_SETUP.md) for the Vehicle Intelligence API and regression commands.
