# Lifecycle + deterministic replay patch

Unchanged on purpose: slot geometry, slot assignment (`logic.py`), tracker, plate OCR/reader
logic, ReID matching, Service-2 mapping (PARK_START -> S2 ENTRY, PARK_END -> S2 EXIT), config files.

## What changed
| File | Change |
|---|---|
| `s1/determinism.py` (new) | Decides deterministic mode (`auto` = only when EVERY source is a file), seeds RNG/cuDNN |
| `s1/multicam.py` | In deterministic mode: every frame read in order, scheduler + motion gate + 5 fps cap on the VIDEO clock, fixed batch order, laptop-clock IST base shared by all cameras |
| `s1/plates.py` | `PlateWorker(deterministic=True)`: no dropped OCR jobs, `wait_idle()` so results land on the same frame every run |
| `s1/flow.py` | `AsyncGlobalReID(deterministic=True)`: same idea for ReID |
| `s1/pipeline.py` | Plate-wait deadlines on the video clock; **inferred EXIT** (see below) |
| `s1/lifecycle_timeline.py` (new) | One row per park cycle: entry / park_start / park_end / exit (IST) |
| `s1/plate_evidence.py` (new) | Best plate crop per car + CLAHE copy, PNG |
| `s1/report.py` | Adds `lifecycle_timeline`, `plate_evidence`, `run_info`, error ledger, lifecycle validation flags |
| `s1/obs.py` (new), `run_kaggle.py`, `s1/main.py` | Logging with run id, rotating `logs/s1.log` (10 MB x 5), error/warning counts in the report |
| `tools/determinism_check.py`, `s1/test_lifecycle_determinism.py` | Proof tools (below) |

Originals of every edited file: `backup_before_lifecycle_determinism/`.

## Lifecycle events (report -> `lifecycle_timeline`)
`ENTRY` (tripwire) -> `PARK_START` (settled in a slot) -> `PARK_END` (left the slot) -> `EXIT`.
EXIT comes from the tripwire when it fires. **New:** if a car left its slot (PARK_END) and its track
then disappears without a tripwire crossing, an EXIT is emitted at the last time it was seen with
`inferred: true`. Turn off: `lifecycle: {infer_exit_on_track_loss: false}`.
Cars first seen in the first `parking.baseline_window_s` (15 s) of a clip are treated as already
parked (existing behaviour): they get slot/plate/colour but no PARK_START.

## Timestamps (IST)
Live camera: laptop/server clock at decode. File replay: laptop clock captured ONCE at run start +
video offset. Relative timings are identical every run; only the absolute start differs.
`determinism.timestamp_source: video_metadata` restores the old "video creation time" behaviour.

## Config knobs (all optional, defaults shown)
```yaml
deterministic: auto            # auto | true | false
determinism: {seed: 1234, timestamp_source: laptop}
lifecycle: {infer_exit_on_track_loss: true}
report: {plate_evidence_dir: data/plate_evidence}
```

## Prove it
```
python -m pytest tests -q -p no:cacheprovider  # S1 suite
python tools/determinism_check.py                                  # synthetic cameras, 3 runs x 3 scenarios
python tools/determinism_check.py --real-config config/config.kaggle_flow.yaml --runs 3   # YOUR videos, GPU
```
The last command is the one that matters: it runs your real YOLO + plates + ReID N times and prints
IDENTICAL or the first differing lines.

## Known trade-offs
* Deterministic replay is not real-time paced; it runs as fast as the machine allows.
* File replay has no rolling "evidence clip" buffer (that lives in the live grabber).
* GPU kernels can still differ across different GPU models/driver versions; within one machine and
  one library stack the pins above remove the known sources.
