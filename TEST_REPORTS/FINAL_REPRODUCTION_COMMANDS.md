# Final reproduction commands

From the project root, provision the CPU environment using the [installation guide](../docs/INSTALLATION_GUIDE.md). Run the portable preflight and CLI from that guide to reproduce the synthetic report.

```bash
RUN="$(mktemp -d)"
PY="$(command -v python)"
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
export SMART_PARKING_UPLOAD_DIR="$RUN/uploads"
export SMART_PARKING_API_PUBLISH=0
export YOLO_CONFIG_DIR="$RUN/yolo"
S1_HANDOVER_TRACE_PATH="$RUN/lifecycle.json" "$PY" -m pytest tests/production -vv -p no:cacheprovider
"$PY" -m coverage run --data-file="$RUN/.coverage" --source=s1 -m pytest tests -q -p no:cacheprovider \
  --junitxml="$RUN/s1.xml"
"$PY" -m coverage report --data-file="$RUN/.coverage"
"$PY" -m pytest tests/integration tests/production -q -p no:cacheprovider
(cd s2 && DATABASE_URL="sqlite:///$RUN/s2.sqlite" "$PY" -m pytest tests -q -p no:cacheprovider \
  --junitxml="$RUN/s2.xml")
"$PY" -m tests.fault_injection.http_evidence --output "$RUN/http"
```

Install coverage, Playwright, Chromium, FFmpeg and bundled S2 test dependencies as described in the [initial evidence reproduction](final_verification/REPRODUCTION_COMMANDS.md). The HTTP harness starts a loopback Uvicorn server, tests upload/cancellation/streaming, and saves Swagger and ReDoc screenshots when Chromium and the public assets are available.

For the historical audits, set `ASSETS` to an authorized read-only extraction of the original supplied candidate and `AUDIT_SLOT` to the slot under review.

```bash
PYTHONPATH="$PWD" "$PY" scripts/production_video_validation.py --assets "$ASSETS" --output "$RUN/video"
"$PY" scripts/production_plate_audit.py --assets "$ASSETS" --slot "$AUDIT_SLOT" \
  --output "$RUN/plate-association.json"
```

The audit contains no deployable plate secrets. Verify the delivered archive before extracting it with `sha256sum -c SMART_PARKING_S1_PRODUCTION_HANDOVER_FINAL.zip.sha256` and `unzip -t SMART_PARKING_S1_PRODUCTION_HANDOVER_FINAL.zip`.
