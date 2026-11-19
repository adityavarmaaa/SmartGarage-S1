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
