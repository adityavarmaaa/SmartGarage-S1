# Reproduction commands

Use Linux with Python 3.12, FFmpeg and Chromium on PATH. Run from the ZIP's project root. The exact reference packages are recorded in [environment.json](raw/environment.json). To provision a new CPU environment:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
python -m pip install -r requirements-dev.txt coverage playwright sqlalchemy pydantic-settings
```

The verification dedicated a temporary output directory and disabled external publishing. Unset deployment credentials before running the commands below. The bundled S2 suite uses a local SQLite connection rather than a deployment database.

```bash
RUN="$(mktemp -d)"
PY="$(command -v python)"
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
export SMART_PARKING_UPLOAD_DIR="$RUN/uploads"
export SMART_PARKING_API_PUBLISH=0
export YOLO_CONFIG_DIR="$RUN/yolo"
mkdir -p "$RUN/http-final"
"$PY" -m pytest tests/fault_injection -vv --tb=short -p no:cacheprovider \
  --log-cli-level=WARNING --junitxml="$RUN/fault-final.xml" > "$RUN/fault-final.log" 2>&1
"$PY" -m coverage run --data-file="$RUN/.coverage" --source=s1 -m pytest tests -q \
  -p no:cacheprovider --junitxml="$RUN/final-s1.xml" > "$RUN/final-s1.log" 2>&1
"$PY" -m coverage report --data-file="$RUN/.coverage" > "$RUN/coverage.txt"
(
  cd s2
  DATABASE_URL="sqlite:///$RUN/s2.sqlite" "$PY" -m pytest tests -q \
    -p no:cacheprovider --junitxml="$RUN/final-s2.xml"
) > "$RUN/final-s2.log" 2>&1
"$PY" -m tests.fault_injection.http_evidence --output "$RUN/http-final" > "$RUN/http-final-harness.log" 2>&1
```

The loopback harness uses the bundled weights, calibration discovered from the configuration and synthetic footage. Public CDN assets used by Swagger and ReDoc must be accessible through the workspace HTTP proxy for the browser checks. Their HTTP endpoint checks run independently. The harness inherits a bound socket into Uvicorn on Linux; its child process is terminated at the end.

For the baseline, extract and verify `SMART_PARKING_S1_RELEASE_VALIDATION.zip` and run the same S1 and S2 pytest commands there without the coverage wrapper. The original 140-test release contains no `tests/fault_injection` suite. To regenerate the CSV matrices and summary metrics from the checked-in raw evidence, run `python tests/fault_injection/generate_evidence_reports.py`.

```bash
sha256sum -c SMART_PARKING_S1_FINAL_EVIDENCE.zip.sha256
unzip -t SMART_PARKING_S1_FINAL_EVIDENCE.zip
```
