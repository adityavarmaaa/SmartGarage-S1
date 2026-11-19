# Smart Parking S1 - complete package (entry -> park start -> park end -> exit, IST, deterministic)

## One-time setup (any teammate with an NVIDIA GPU)
Windows PowerShell:
```
python -m venv .venv ; .\.venv\Scripts\Activate.ps1
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
pip install -r requirements-gpu.txt
python preflight.py config/config.local.yaml
```
Linux: same, with `source .venv/bin/activate`.
Internet is needed on the first run (plate models download once). Optional: put the OSNet
`osnet_ain_x1_0_vehicle_reid.onnx` in `models/`; without it a weaker colour ReID is used (labelled in the report).

## Run
1. Copy `IMG_2138.MOV` and `IMG_2136.MOV` into `videos/` (or use `--videos <folder>`).
2. `python run_local.py`
3. Open `smart_parking_final_report.json` -> `lifecycle_timeline` (entry / park start / park end / exit in IST),
   `vehicles`, `service2_events`, `pipeline_health.logging`. Plate crops (+CLAHE): `data/plate_evidence/`. Logs: `logs/s1.log`.

## Check it is repeatable on YOUR machine
`python tools/determinism_check.py --real-config config/config.local.yaml --runs 3`
(Kaggle: use `run_kaggle.py` and `config/config.kaggle_flow.yaml` as before.)

## Tests
`pip install pytest` then `python -m pytest -q --ignore=s1/test_yolo11n_detector_v1.py`

See README_LIFECYCLE_DETERMINISM.md for exactly what changed and the known limits.
