# Troubleshooting

| Signal | What to check |
| --- | --- |
| `VIDEO_SOURCE_NOT_FOUND`, `VIDEO_SOURCE_OPEN_FAILED` | Confirm the path, source permission and decoder. Run preflight with the active config. A workstation path in the template needs replacement. |
| `VIDEO_FRAME_INVALID`, `VIDEO_FRAME_READ_FAILED`, `CAMERA_STARTUP_TIMEOUT` | Inspect decoder and timestamps, then confirm the configured sensor and dimensions. Review other cameras in the same run for isolation. |
| `YOLO_MODEL_LOAD_FAILED`, `YOLO_INFERENCE_FAILED`, `DETECTION_OUTPUT_INVALID` | Check the configured model, provider, frame pixels and structured camera/frame IDs. |
| `TRACKER_UPDATE_FAILED`, `TRACKER_OUTPUT_INVALID` | Check for invalid track output after inference. Match diagnostic camera and frame to the source. |
| `ALPR_MODEL_LOAD_FAILED`, `ALPR_INFERENCE_FAILED`, `PLATE_CROP_INVALID` | Check optional weights and crop suitability. Preserve an unconfirmed plate while reviewing occupancy. |
| `SLOT_GEOMETRY_INVALID`, UNKNOWN occupancy | Validate calibrated polygons and dimensions. Check frame progress; a missing observation does not establish FREE. |
| `LIFECYCLE_EXIT_ASSOCIATION_CONFLICT`, `LIFECYCLE_EXIT_BEFORE_PARK_START` | Compare UID, slot, camera, session times and OCR evidence before manual reconciliation. |
| `LIFECYCLE_ENTRY_REPLAY`, `LIFECYCLE_PARK_START_REPLAY` | Repeated source events were withheld. Compare observation key and sender queue state. |
| `REPORT_GENERATION_FAILED` | Check permissions, free space and parent directory. The previous valid report may still be present. |
| API 409 and streaming unavailable | Poll status. One worker processes a job at a time. Active MJPEG and final MP4 depend on the enabled recorder. |
| API cancellation still pending | Poll terminal state. Capture and model calls must return before cooperative teardown. Review runtime logs. |
| `OUTBOX_PERSIST_FAILED` or outgoing HTTP errors | Check SQLite and storage, configured URL, key/permissions and network. Keep queue and lifecycle DB aligned. Reconcile accepted destination events before retrying. |

## Automated setup and recorded runs

Read the [developer guide](DEVELOPER_SETUP.md#automated-execution) first. A PostgreSQL connection error at the configured endpoint calls for checking the actual host, port and service. If the server rejected credentials or privileges, resolve that before rerunning migrations. Use database-free preview while the development server is being provisioned.

Each run prints its output directory. Read `execution-summary.json`, then the stage-specific log named in the summary or the pipeline log. Full mode with zero matched lifecycle rows means the recording did not establish and close a verifiable session. A readable MP4 can still be available. Review `database-verification.json` and confirm the footage and calibration before trying again.
