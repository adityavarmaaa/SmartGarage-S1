# Smart Parking S1 post-handover validation

## Preservation and delivery

The previous handover ZIP, checksum, and engineering report were preserved. The work used an independent extraction of the validated handover. The original ZIP checksum still matches its recorded SHA-256. This release ships in `SMART_PARKING_S1_RELEASE_VALIDATION.zip`, beside its own SHA-256 checksum. The ZIP retains the original project layout and includes an internal file digest manifest.

## Cancellation and video

- `POST /api/live/stop` requests cancellation for the active upload or camera job. Repeated stop requests are safe. The worker slot stays occupied until cleanup ends. Single- and multi-camera processing finish in-flight work, release capture, drain queued evidence and flush observed lifecycle work. Cancellation is recorded in the job status and in reports generated during finalization.
- Multicamera reports still use atomic replacement. An injected report-write failure preserved the prior valid report and surfaced a processing error. An interrupted H.264 conversion falls back to a verified playable MP4 when possible. Incomplete temporary video files are removed and an existing final file survives failed finalization.
- The original stream route now supports byte-range playback of finalized MP4. Add `?live=true` for MJPEG while the configured demo recorder is active. Each job retains one current preview frame in memory. Existing preview connections close on terminal job state.
- Verified a real Uvicorn server using the bundled CPU YOLO weights and a short excerpt from supplied annotated footage. A normal upload produced a playable MP4. On a second job, a live JPEG decoded successfully, the stop endpoint acknowledged cancellation, the multipart stream terminated, the report recorded cancellation, and the partial MP4 decoded and served HTTP 206. HEAD and full GET were checked. Health, Swagger HTML, ReDoc and OpenAPI served successfully. CDN JavaScript rendering was outside this check.

## Regression and coverage

| Suite | Handover | Final | Failed | Skipped | Errors | Final duration |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| S1 | 103 | 112 | 0 | 0 | 0 | 13.93 s with coverage |
| Bundled S2 | 28 | 28 | 0 | 0 | 0 | 2.39 s |
| Total | 131 | 140 | 0 | 0 | 0 | |

S1 line coverage rose from **51% to 58%**. The [coverage report](../TEST_REPORTS/raw/prior_verification/release/coverage.txt) has file-level metrics. The added tests cover resource teardown, cancellation before the first frame and during inference, HTTP ranges and malformed requests, preservation of a report on write failure, invalid video finalization, bounded MJPEG buffers, and recovery from shared inference failure through real slot assignment. Existing regression tests remain intact. Test output included the Starlette test-client adapter warning and S2's Pydantic settings deprecation warning.

The outbox test used generated credentials, temporary SQLite and a loopback HTTP receiver. It verified the authenticated request, decrypted outbound JSON, the durable encrypted row and the acknowledgement. No production publishing occurred.

## Operating limits and unverified targets

- Windows validation was unavailable in the Linux workspace. The Windows guide documents commands and their Linux verification status.
- CUDA was unavailable in the CPU PyTorch environment. The real detector check used CPU.
- No provisioned ANPR or OSNet weights with corresponding validation footage were available. Physical camera and RTSP operation were outside the validation.
- Live MJPEG and finalized MP4 are available when the multicamera demo recorder is enabled. The standalone latest-frame endpoint retains its existing HTTP 501 response.
- Cancellation waits for in-flight native model or capture calls to return. A cancelled MP4 can use the MP4V fallback when H.264 conversion is interrupted. Player support varies.
- Live preview and job state are process-local. Output files are retained under the configured API output directory. Production deployment and the real publishing destination were outside this validation.
