# AI agent setup

1. Read the [developer guide](DEVELOPER_SETUP.md), [architecture](ARCHITECTURE_HANDOVER.md), and [end-to-end data flow](END_TO_END.md). Check Git status and preserve the working tree.
2. Confirm Python, FFmpeg, writable storage and the requested mode. For full mode, confirm the local PostgreSQL endpoint and a dedicated development login. If it is absent, follow the [Windows runbook](WINDOWS_RUNBOOK.md#full-integration-prerequisites) or the platform's PostgreSQL installation procedure. Check the actual service and port. Ask for unavailable credentials, calibration, recordings or model assets. Keep secrets in process variables. Leave existing environment files intact.
3. Follow [Quick preview](DEVELOPER_SETUP.md#quick-preview) for a no-publisher replay or [Full system](DEVELOPER_SETUP.md#full-system) for setup, migrations, API startup, authenticated delivery and direct database checks. Use a config with a `cameras` list. The bundled clip is for software verification. Full mode needs calibrated footage with both parking and departure.
4. Check `execution-summary.json`, `report.json`, `annotated.mp4` and pipeline logs under the printed run directory. In full mode, also read `database-verification.json`. Do not report a full pass if the runner returned an error or the only events came from the controlled database smoke test.
5. Run the [test commands](DEVELOPER_SETUP.md#verify-on-isolated-resources) and report the exact commands, results and missing prerequisites. Distinguish a preview, controlled-event persistence, and real footage-to-PostgreSQL validation.

For an existing database, apply and verify the [forward-only delivery migration](DEVELOPER_SETUP.md#delivery-integrity-and-schema-upgrade) before starting the updated API. In full mode read `outbox-verification.json` as well as the PostgreSQL read-back. Keep preview observations, controlled-event tests and verified recorded-video delivery distinct.

Do not modify existing application code until you have inspected the repository, understood the architecture, checked the working tree, and run the baseline tests.

From a fresh clone, first run `git status --short`, read the guides linked above, and install the documented CPU environment. Activate `.venv` and run the [quick test](TESTING.md). Follow [database setup](DATABASE.md) before requesting full integration. The Vehicle Intelligence Service lives in `vehicle_intelligence/` and exports `vehicle_intelligence.api:app`. The Parking & Bay Management Service lives in `parking_management/` and exports `parking_management.app.main:app`. The manual Uvicorn commands and Swagger links are in the [developer guide](DEVELOPER_SETUP.md#start-and-connect-the-services).

Report the commit, commands, exact test counts and artifact paths, and distinguish setup, mocked regression, controlled HTTP/PostgreSQL integration, real preview and real footage-to-database acceptance.
