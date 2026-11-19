# Installation and portable preview

The reference verification ran on Linux x86_64, Python 3.12.14, CPU PyTorch, OpenCV and FFmpeg. The pinned observations are in [the reference environment](../TEST_REPORTS/final_verification/raw/environment.json). The test environment already had its CPU packages provisioned. A wheel build and runtime preflight validate the delivered source and model. Dependency installation on another host is environment-specific. Windows, CUDA/GPU and live ANPR installation were outside this verification.

From the extracted project root:

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
python -m pip install -r requirements-dev.txt
python main.py --help
```

The local config selects bundled `yolo11n.pt`. Set the detector model path explicitly if you use a separately approved model. Install FFmpeg for metadata and H.264 export. Configure Linux device IDs or authorized source URLs and matching geometry before using site cameras. ANPR requires provisioned detector and OCR models. OSNet requires its configured ONNX model. Verify providers before using GPU, and avoid installing conflicting CPU and GPU ONNX runtimes. See [Windows notes](../INSTALL_WINDOWS.md) and the [configuration reference](CONFIGURATION_REFERENCE.md).

Try the portable preview on the synthetic MP4 from the HTTP verification. The generator uses a selected calibration and disables OCR, ReID and external publishing for this preview.

```bash
TMPDIR_S1="$(mktemp -d)"
python scripts/make_handover_config.py \
  --source TEST_REPORTS/raw/http/cancelled-output.mp4 \
  --output "$TMPDIR_S1/preview.yaml"
python preflight.py "$TMPDIR_S1/preview.yaml"
python main.py --config "$TMPDIR_S1/preview.yaml" --no-publisher --no-plates
```

Inspect the report next to `preview.yaml`. For API preview, choose a writable work directory and start Uvicorn from the project root.

```bash
SMART_PARKING_CONFIG="$TMPDIR_S1/preview.yaml" SMART_PARKING_API_PUBLISH=0 \
  python -m uvicorn s1.api:app --host 127.0.0.1 --port 8000
```

See [API reference](API_SWAGGER.md) and [reproduction commands](../TEST_REPORTS/FINAL_REPRODUCTION_COMMANDS.md) for HTTP, coverage and bundled S2 tests.
