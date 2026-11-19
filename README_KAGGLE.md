# Smart Parking S1 - run on Kaggle (v2)

## 0. What you need attached
| Item | Where | If missing |
|---|---|---|
| Test videos (IMG_2138.MOV, IMG_2136.MOV) | Kaggle dataset | run stops with a clear message |
| `yolo11n.pt` | already in this zip | run stops |
| OSNet `osnet_ain_x1_0_vehicle_reid.onnx` | put in `models/` | **runs anyway** with a weak colour-histogram ReID (labelled in the report) |
| Internet ON (first run only) | Kaggle settings | plates are skipped, rest still runs |
| GPU accelerator (T4) | Kaggle settings | falls back to CPU (slow) |

## 1. Install (one cell)
```
!pip uninstall -y onnxruntime
!pip install -q onnxruntime-gpu "fast-alpr[onnx]" ultralytics
```

## 2. Check, then run
```
!python preflight.py            # FAIL = cannot run, WARN = runs with lower quality
!python run_kaggle.py           # TEST MODE: previews the JSON, sends nothing
```
Outputs: `smart_parking_final_report.json`, `data/demo_multicam.mp4` (all cameras tiled).

## 3. Read the result
Open `smart_parking_final_report.json`:
- `summary` - counts at a glance
- `service2_events` - **exactly** the ENTRY / EXIT payloads the DB service receives
- `vehicles` - one record per car (plate, colour, type, cameras, slot, PARKED/SEEN)
- `cameras.<id>.slots` - every slot incl. FREE ones
- `pipeline_health` - which ReID backend ran, plate engine ON/OFF/FAILED, batch sizes, motion ROI
- `validation` - PASS / NOT_DEMONSTRATED flags (nothing is faked: if no EXIT happened it says so)

## 4. Send to the DB service
```
!python tools/send_report_to_s2.py smart_parking_final_report.json            # dry run
!python tools/send_report_to_s2.py smart_parking_final_report.json --send     # real POST
```
or run live: `!python run_kaggle.py --publish`.
Auth (only if Service 2 requires it): set `S2_API_KEY` (and `S2_API_KEY_HEADER` if the header is not `X-API-Key`).
Re-sending is safe: HTTP 409 counts as already received.

## 5. No GPU / no videos? Prove the plumbing first (~1 min)
```
!python tools/synth_smoke_test.py             # 1 camera: ENTRY, EXIT, baseline car, leaf filter
!python tools/synth_smoke_test.py --transfer  # 2 cameras: car leaves cam A, parks on cam B (one global ID)
```

## Tuning knobs (config/config.kaggle_flow.yaml)
- `inference.motion_roi.*` - parking-area motion mask; add `"motion_roi": [[x,y],...]` to a geometry JSON for a hand-drawn region
- `inference.batch_size` - max cameras per YOLO call (20)
- `global_reid.similarity_threshold` (OSNet) / `fallback_similarity_threshold` (histogram)
- `replay: sync` processes every file frame (slower, no skipping) instead of `realtime`
