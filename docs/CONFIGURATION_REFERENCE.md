# Configuration reference

`vehicle_intelligence.common.config.load_config` merges YAML mappings with code defaults. Relative source, model, report and DB paths are resolved from the process working directory. Run from the project root or use absolute paths in deployment. See [field inventory](CONFIGURATION_FIELDS.csv) for actual defaults and the shipped examples. The original `config/config.local.yaml` is a historical template and needs operator inputs. Other YAMLs under `config/` are scenario variants. Some restore variants refer to historical source artifacts kept with the previous authorized release.

| Field | Type / requirement | Validation and meaning |
| --- | --- | --- |
| `cameras` | Optional list of mappings | When present it is nonempty. Each item needs a unique `camera_id`, a source and geometry path. Per-camera values override shared defaults. |
| `camera_id`, `parking_area_id` | Identifiers | Scope local tracking and lifecycle. Set a site area for outbound processing. Keep camera IDs stable and unique within the config. |
| `source` | Local path, device index, or supported URL | Files must exist and decode. API live start accepts indices and RTSP(S). |
| `geometry_file` | JSON file path | Valid tripwire and interior point. Geometry requires valid polygons and distinct slot IDs. Any specified frame dimensions must be positive. |
| `replay` | String, defaults to `realtime` | `sync` is for deterministic file replay. API live processing uses realtime. |
| `detector` | Mapping | `model_path` points at approved weights. `max_fps` is finite and positive. `device` controls provider. |
| `tracker`, `parking` | Mappings | Association thresholds/age and slot overlap/dwell/hysteresis. Calibrate per camera. |
| `plate` | Mapping | Reader models, crop screening, confidence, minimum consistent reads and deadline. `enabled: false` disables OCR. |
| `global_reid`, `handoff`, `inference` | Mappings | Identity handoff, batching, scheduling and buffer parameters. |
| `publisher`, `report`, `demo_video` | Mappings | Sender destination and local state, JSON report target, output codec/size inputs. Files and parent directories must be writable. |
| `exit_restore`, `camera_motion`, `gpu` | Optional mappings | Restore inputs, motion compensation and auxiliary device policy. Restore artifacts and separate providers require site validation. |

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `SMART_PARKING_CONFIG` | Project `config/config.local.yaml` | API configuration file. |
| `SMART_PARKING_UPLOAD_DIR`, `SMART_PARKING_OUTPUT_DIR` | Project data subdirectories | Local uploads and finalized API video. |
| `SMART_PARKING_MAX_UPLOAD_MB` | 2048 | Positive API upload size limit in MiB. |
| `SMART_PARKING_API_PUBLISH` | 0 | An explicit 1 enables external sending. |
| `S1_RTSP_OPEN_TIMEOUT_MS`, `S1_RTSP_READ_TIMEOUT_MS` | 10000 each | Capture startup/read backend timeouts. |
| `S2_API_KEY`, `S2_API_KEY_HEADER` | Key unset; header `X-API-Key` | Outbox API credentials. Inject from the deployment secret manager. |
| `SP_ENCRYPTION_KEYS`, `SP_ACTIVE_ENCRYPTION_KEY`, `SP_HMAC_KEY` | Unset | Required for applicable encrypted lifecycle/queue operations. Use approved secrets, preserve key IDs and retain key rotation history. |
| `SNAPSHOT_STORAGE`, `S3_BUCKET`, `S3_PUBLIC_BASE_URL` | Storage off; bucket/base unset | Optional S3-compatible evidence upload. |

For recorded-video verification, use the generated configuration in the [portable preview](INSTALLATION_GUIDE.md). For site calibration, verify the intended source dimensions, interior point, tripwire, and slot polygons for every camera. The checked-in `data/` geometry files are examples for their original recordings. `calibrate_entry_tripwire.py` is an interactive helper that accepts `--video` and `--geometry`; GUI calibration was outside the final API audit.
