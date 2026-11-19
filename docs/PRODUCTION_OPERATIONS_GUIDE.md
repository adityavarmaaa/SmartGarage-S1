# Production operations guide

## Startup and acceptance

Run from a deployment-owned project root. Start with a copy of `config/config.local.yaml` and configure authorized camera sources, sensor-specific geometry, writable output and outbox paths, and model providers. The stock config refers to archived workstation paths. Inspect every camera line in `python preflight.py <config>`; a single ready camera can make a multicamera preflight return success. Use an isolated setup and an authorized calibration clip to validate each camera before enabling publishing.

Run preview with `python main.py --config <config> --no-publisher`. The one-job API defaults to preview when `SMART_PARKING_API_PUBLISH` is unset. Bind the API to a controlled interface. Production access requires upstream authentication, TLS, upload size controls, and approved media access. Validate the destination, API credentials, storage permissions, queue and network boundaries in staging before enabling external publishing.

## Health and monitoring

`GET /health` reports process liveness. `GET /api/status` shows the active or latest job, diagnostics, worker state and available video URLs. Job state is held per process. Monitor CLI rotation under `logs/`, Uvicorn logs, storage usage, queue errors and per-camera frame/diagnostic counters. Review the JSON report's `processing_status`, failed camera list and occupancy. UNKNOWN and incomplete results should stay visible to operators. Output files accumulate in the configured output directory; apply site retention outside the worker.

## Stop, restart and recover

For API jobs, send `POST /api/live/stop` and wait until the state is terminal and `worker_alive` is false. Allow cleanup for decoder handles, OCR, writer and publisher resources. Send the server's normal shutdown signal after the job drains. CLI preview supports interrupt-driven shutdown. A native backend stall can delay cooperative cancellation; record the current state and follow the site's bounded restart procedure. Keep the last valid report and media until the new run is verified.

Before upgrade, back up the configured SQLite outbox and related per-camera lifecycle databases consistently. Preserve key IDs and key material in deployment secret storage. Keep the previous release/config snapshot and the DB set together. Start one writer for a given output/database state. The park-start receipt table is additive. A receipt means that the mapper handed the event to the sender/outbox. Remote acknowledgement is tracked separately by the outbox.

If an exit is withheld because identity signals conflict, keep the session for manual reconciliation. Use the camera, source time and slot with authorized footage. Avoid replacing a candidate plate with a guessed value. Restore DB snapshots and the matching prior release when rollback is needed; account for any externally accepted events before replay.

See [troubleshooting](TROUBLESHOOTING.md), [configuration](CONFIGURATION_REFERENCE.md), and the [readiness assessment](../TEST_REPORTS/PRODUCTION_HANDOVER_READINESS_REPORT.md).
