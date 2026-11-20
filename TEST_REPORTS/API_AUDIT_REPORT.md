# Final S1 API audit

The audit started from GitHub commit `3513ed0fc5d3bd5ead2deaac8299dc8b1fa00f85`. The baseline passed 44 loopback HTTP checks and all 10 schema and browser checks. The cancellation test and HTTP harness confirmed that a cancelled upload can produce a decodable partial MP4 with a finalized cancellation report.

## Findings and changes

- The generated OpenAPI contract declared response bodies on HEAD. S1 sends the selected representation's headers and an empty response body. The schema now documents the header-only response and distinguishes MP4 headers from live MJPEG headers.
- The 416 declaration now documents an empty response and `Content-Range: bytes */<size>`. The existing runtime compatibility fix remains in place. The loopback harness now asserts the exact range header against the delivered MP4's byte length.
- Two pytest cases cover the GET/HEAD schema contract and lifespan shutdown signalling and joining an active worker. The existing cancellation integration test covers worker exit, report and video finalization, streaming, range requests, and output cleanup. Processing, camera selection, rendering, and publishing code were unchanged.

## Endpoint coverage

| Endpoint | Scenarios exercised |
| --- | --- |
| GET `/`, GET `/health` | Service discovery and liveness before and after failed jobs and cancellation |
| GET `/api/status` | Idle, active, error, and cancelled snapshots; output URLs and released worker |
| POST `/api/upload` | Multipart acceptance, missing file, unsupported extension, empty and oversized uploads, blocked storage, corrupt video failure, and valid CPU processing |
| POST `/api/live/start` | Missing/wrong-type body fields, malformed JSON, unsupported scheme, accepted request followed by failed config, and active-job conflict |
| POST `/api/live/stop` | Idle behavior, active cancellation, repeated stops in integration, and waiting for cleanup |
| GET `/api/video/uploaded` | Empty and nonempty inventory, directory fault, and recovery |
| GET `/api/frames/latest` | Explicit 501 feature-unavailable response |
| GET, HEAD `/api/output/stream` | Missing output, pending output, live preview and termination, invalid live query, bodyless HEAD, valid and invalid byte ranges, playable cancelled MP4 |
| GET `/openapi.json`, `/docs`, `/redoc`, `/docs/oauth2-redirect` | HTTP accessibility, OpenAPI 3.1 schema validation, application operation parity, and Chromium rendering of both UIs |
| Unknown route and unsupported method | Structured 404 and 405 |

Every case and observed HTTP status is in the [request matrix](raw/api-finalization/http-checks.csv). The [results record](raw/api-finalization/results.json) contains counts, observed range headers and shutdown status. Chromium captures show [Swagger](raw/api-finalization/swagger.png) and [ReDoc](raw/api-finalization/redoc.png). The checked-in [OpenAPI 3.1 schema](../docs/openapi.json) validated and matched the runtime schema for all 10 application operations.

## Final results

| Run | Passed | Failed | Errors | Skipped | Blocked |
| --- | ---: | ---: | ---: | ---: | ---: |
| Targeted API, cancellation, failure recovery and fault injection | 80 | 0 | 0 | 0 | 0 |
| Complete S1 | 174 | 0 | 0 | 0 | 0 |
| Complete bundled S2 | 28 | 0 | 0 | 0 | 0 |
| Loopback HTTP assertions | 44 | 0 | 0 | 0 | 0 |
| OpenAPI, Swagger and ReDoc assertions | 10 | 0 | 0 | 0 | 0 |

The targeted tests are included in the S1 count. The Linux CPU runs used Python 3.12.14, FastAPI 0.141.1 and Starlette 1.6.0, with temporary storage and external publishing disabled. S2 used SQLite. The two additional S1 tests account for the change from the original 200-test baseline to 202 tests. Framework deprecation warnings remained visible.

The live camera API's validation, acceptance and conflict behavior were exercised. Device acquisition, RTSP, Windows, CUDA, live ANPR and site publishing require separate environment acceptance. A native backend stall can delay cooperative shutdown. No selected automated check was blocked during this audit.

See [commands](FINAL_REPRODUCTION_COMMANDS.md), [documentation completion](API_DOCUMENTATION_FINALIZATION_REPORT.md), and the [API reference](../docs/API_SWAGGER.md).
