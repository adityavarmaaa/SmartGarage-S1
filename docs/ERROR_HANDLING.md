# Error handling and diagnostics

`vehicle_intelligence.common.errors.Diagnostic` carries code, module, component, operation, category, severity, message, recoverability, result, response action, UTC timestamp and context. Processing and configuration exceptions carry a diagnostic and preserve causal exceptions internally. The rotating CLI log includes a run ID. The error ledger records counts by logger and retains recent errors for reports.

| Boundary | Behavior |
| --- | --- |
| Detection | Empty detections are valid tracker input. Inference errors and malformed output skip the frame without telling parking or tracking that the scene was empty. |
| Tracking | Association failure stops downstream updates for the affected frame. Subsequent frames can resume. |
| ANPR | Invalid crop or reader failure yields no OCR evidence. Configuration, confidence and consensus still govern confirmation. Vehicle and parking processing can continue with an unconfirmed plate. |
| Attributes | Colour estimation failure preserves past observations or leaves the value unavailable. |
| Geometry and capture | Invalid calibration prevents that camera from starting. Early file decode failure is distinct from normal EOF. |
| Multicamera | Startup failures isolate the affected camera. Failed inference batches are dropped so the scheduler can continue. Final reports include failed camera IDs and a processing status. |
| Lifecycle and events | Existing slot arbitration, transition checks, event envelopes and deduplication remain in use. |
| Outbox | An outgoing SQLite transaction rolls back if persistence fails. The error carries the event ID. A durable pending row is deleted only after the outgoing commit succeeds. |
| Reporting and video | Reports are replaced after successful serialization. Video rendering can fail independently of parking, and logs identify missing or partial output. |
| API | HTTP errors use a typed envelope. Job failures surface in status, while tracebacks stay in internal logs. |

Inspect `logs/vehicle_intelligence.log` for CLI diagnostics and the API server logs for HTTP and job failures. Match a processing diagnostic to its camera and frame, or an API diagnostic to its job ID. Preview reports and video can contain sensitive vehicle evidence. Treat logs and runtime artifacts as restricted data.
