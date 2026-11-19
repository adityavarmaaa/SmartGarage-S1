# Smart Parking — End-to-End Technical Guide

## System flow

1. **Input** — one or more camera videos / RTSP streams are opened by the capture layer.
2. **Detection** — YOLO vehicle detections are produced using configured weights and thresholds.
3. **Tracking** — per-camera tracks preserve vehicle continuity while visible.
4. **Attributes and ANPR** — vehicle attributes and asynchronous number-plate evidence are collected.
5. **Parking logic** — vehicle footpoint/overlap and dwell logic associate a tracked vehicle with calibrated slot polygons.
6. **Lifecycle** — ENTRY, PARK_START, PARK_END and EXIT are evaluated from tripwire, occupancy, dwell and track evidence.
7. **Identity / handoff** — global identity matching attempts to relate observations across cameras. The report must identify whether OSNet or histogram fallback was used.
8. **Service 2** — events are mapped and sent through the outbox/publisher when publishing is enabled. Default Kaggle runner behavior is preview-only.
9. **Evidence** — JSON reports, lifecycle timeline, plate evidence, logs and annotated MP4 outputs are written to configured paths.

## Important operating constraints

- Current production geometry is calibrated JSON. Automatic geometry discovery remains experimental and is not connected to ENTRY/EXIT.
- Current validated setup has two camera areas and six configured parking slots (three per area); code must continue to read slot definitions from geometry rather than hardcoding a count.
- A histogram ReID fallback is weaker than OSNet and does not prove cross-camera visual ReID.
- For prerecorded videos, event-processing timestamps in IST must not be described as original real-world capture/arrival timestamps unless the source provides that metadata.
- Publishing must remain disabled for test runs unless an operator explicitly requests it.

## Main entry points

- Kaggle: `python run_kaggle.py`
- Local: `python run_local.py --videos videos`
- API: `uvicorn s1.api:app --host 127.0.0.1 --port 8000`
- Preflight: `python preflight.py config/config.local.yaml`
- Regression: `python -m pytest -q --ignore=s1/test_yolo11n_detector_v1.py -p no:cacheprovider`
- Synthetic smoke: `python tools/synth_smoke_test.py` and `python tools/synth_smoke_test.py --transfer`

## Evidence to retain for each release

- Input video inventory and checksums
- Config and geometry JSON files
- Complete JSON report for ENTRY and EXIT
- Annotated multicamera MP4
- `logs/s1.log`
- Test output and test environment versions
- ReID backend and plate engine status
- Validation flags, including any `NOT_DEMONSTRATED` items
- Package checksum manifest
