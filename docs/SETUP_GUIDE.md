# Smart Parking — Setup Guide

## Python environment

Use Python 3.10–3.12 where possible for the supported ML dependency ecosystem. The original project was also exercised in Kaggle Python 3.13, but local ML wheels may differ.

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-api.txt
```

For NVIDIA GPU, install the PyTorch build matching the host CUDA runtime using the official PyTorch selector, then install `requirements-gpu.txt`. Do not install CPU and GPU ONNX Runtime packages together.

### Linux

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-api.txt
```

## Required project files

- `yolo11n.pt` at the configured detector model path
- Camera video files or reachable RTSP/RTSPS sources
- One calibrated geometry JSON per camera
- Optional OSNet ONNX weights for stronger visual ReID

## Validate and run

```bash
python preflight.py config/config.local.yaml
python -m pytest tests -q -p no:cacheprovider
python run_local.py --videos videos
```

For Kaggle, attach the video dataset and run `python run_kaggle.py`. See `README_KAGGLE.md`.

## API

```bash
uvicorn s1.api:app --host 127.0.0.1 --port 8000
```

Then open `/docs`. Set `SMART_PARKING_CONFIG` to the config YAML to use.
