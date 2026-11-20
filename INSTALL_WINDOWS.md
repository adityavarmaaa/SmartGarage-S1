# Windows installation

Use Windows 10 or 11, Python 3.10–3.12, and PowerShell. Run these commands from the extracted project directory. The commands call the virtual environment directly, so activation and PowerShell execution policy changes are unnecessary.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
.\.venv\Scripts\python.exe main.py --help
```

Edit the source and geometry paths in your copy of `config/config.local.yaml` to match your recordings or devices. The archived configuration references the original workstation's recordings. Reuse a geometry file only for the camera and calibration it describes. Set the detector device to `cpu` for a CPU deployment. Check input paths and inference dependencies before running.

```powershell
.\.venv\Scripts\python.exe preflight.py config/config.local.yaml
.\.venv\Scripts\python.exe run_local.py --config config/config.local.yaml --videos videos --no-plates
```

Remove `--no-plates` after supplying or provisioning the detector/OCR models used by the configuration. `run_local.py` uses preview mode unless `--publish` is supplied.

## API

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-api.txt
$env:SMART_PARKING_CONFIG = "config/config.local.yaml"
.\.venv\Scripts\python.exe -m uvicorn s1.api:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/docs`. Check liveness at `/health` and the job outcome at `/api/status`. API publishing defaults to preview. Use `POST /api/live/stop` for cancellation and the output URLs in job status for live or finalized video when configured. Refer to [API behavior](docs/API_SWAGGER.md) for the opt-in and limitations.

## GPU and video

Install a compatible CUDA build of PyTorch first, then use `requirements-gpu.txt`. Keep exactly one ONNX Runtime provider installed. FFmpeg on PATH enables H.264 conversion and metadata probing. Without FFmpeg, demo output uses the OpenCV MP4 writer. The headless OpenCV runtime is intended for CLI and API operation. OpenCV GUI preview requires a GUI-capable OpenCV build.

These PowerShell commands are reference instructions and were not executed on Windows. Linux CPU verification is recorded in the [final audit](TEST_REPORTS/API_DOCUMENTATION_FINALIZATION_REPORT.md).
