# Smart Parking S1 engineering handover

## A. Error handling

Reviewed the vehicle front end, plate reader and confirmation flow, parking geometry and occupancy, lifecycle and bridge, event and S2 publishing paths, video and multicamera runtime, reporting, configuration, logging and API. Added structured diagnostics at the inference, tracking, OCR, attribute, geometry, event, outbox, capture, report and API boundaries. Diagnostics provide origin, operation, category, severity, UTC time, recoverability, outcome, action and available identifiers. Internal logs retain exception context. The ledger captures structured records for reports.

A valid empty detector result advances tracker lifecycle. A failed observation skips downstream tracking and parking updates for that frame. Invalid or missing plate evidence leaves the registration unconfirmed. Camera startup failures allow independent cameras to continue. Failed capture leaves affected occupancy unknown when the camera's calibration is available to the report. Event persistence happens before deleting a durable pending row. Report replacement is atomic.

## B. Regression testing

| Suite | Baseline | Final | Failed | Skipped | Errors | Final duration |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| S1 | 85 passed | 103 passed | 0 | 0 | 0 | 9.68 s |
| Bundled S2 | 28 passed | 28 passed | 0 | 0 | 0 | 1.02 s |
| Total | 113 passed | 131 passed | 0 | 0 | 0 | 10.70 s |

S1 ran unit, regression and integration tests in the `tests` directory. The detector diagnostic under `s1` is a manual CLI and is outside the test count. An initial baseline from the shared root had six S2 collection errors because the bundled S2 imports `app` relative to its own working directory. The corrected S2 run used its own directory and isolated SQLite. No production publishing was enabled. S1 line coverage is in [the coverage report](verification/coverage.txt). Remaining dependency warnings concern the Starlette test client adapter and S2's Pydantic settings syntax.

## C. Integration testing

- Checked the real bundled YOLO weights with a short excerpt of the supplied annotated video. First-frame inference returned one detection. The scheduler produced local tracks, a completed report with three calibrated slots and a playable 172 × 530 video at 10 fps. All ten output frames decoded. The first composite contained visible scene content and annotations.
- Tested a two-camera setup with a good local clip and invalid geometry on the other camera. The good camera kept processing, the output decoded, and the report identified the failed camera and partial run.
- Mocked empty, invalid and failed detector/OCR paths. Tests cover unconfirmed plates on ENTRY, invalid parking events, unknown occupancy and report-write recovery. The existing suites cover tripwire handoff, slot ambiguity, baseline recovery, lifecycle timelines, S2 event mapping, payload deduplication and evidence pinning.
- The short real-video excerpt demonstrated detection and tracking. Lifecycle event transitions were validated with controlled inputs and regressions. The excerpt did not produce a complete live parking cycle.

## D. API

Kept the existing routes. Upload and live start return HTTP 202, with structured validation, conflict, storage and request failures. The job status exposes sanitized diagnostics after asynchronous failure. Health reports liveness. The API defaults to no external publishing. It supports one worker and uses the first calibrated camera configuration for a single source.

## E. Swagger

A loopback Uvicorn server served Swagger HTML, ReDoc, and the OpenAPI JSON. The server accepted a real test clip, completed CPU inference and exposed its status and saved upload. The archive includes the schema generated directly from the running app. Browser execution of CDN JavaScript was outside the verification.

## F. Documentation

The README and Windows guide describe dependencies, camera configuration, startup, preview isolation, reports and test commands. API behavior and limitations are documented alongside the schema.

## G. Final project

Project root inside the ZIP is `smart_parking_s1_complete/`. The original uploads and reconstructed input were preserved. The reconstructed input was 340,299,494 bytes and all 1,471 ZIP entries passed CRC checks. Its calculated SHA-256 was `ff185cd8c38e4bfbf633449d3e5e5641e408ee03bd36b86cfb818c778835c20e`. No original digest was supplied for comparison.

Final archive is `SMART_PARKING_S1_HANDOVER.zip`. Its SHA-256 is in the accompanying checksum file. The package includes an internal SHA-256 manifest, models, calibration, source, tests, docs, and referenced restore assets. Runtime databases, unneeded prior output videos, old backups, logs, caches and credential files remain outside the clean handover. See the original archive for historical output.

```bash
python run_local.py --config config/config.local.yaml --videos videos --no-plates
python -m uvicorn s1.api:app --host 127.0.0.1 --port 8000
```

## H. Remaining issues

- The archived camera config points to raw recordings absent from the supplied project. Supply those recordings or align sources and calibration before using that config. Video verification used the archived annotated excerpt and synthetic clips.
- Real ANPR models, production OSNet, GPU execution, physical/RTSP cameras, high camera counts, Windows execution and live external publishing were outside the available isolated verification. In-process tests verify OCR fallback and plate confirmation gates.
- Graceful cancellation and live HTTP preview remain unavailable. The app has no built-in authentication. Bind it to a trusted interface.
- Some legacy boundaries still use component text logs. The structured changes target the verified failure and recovery paths.
