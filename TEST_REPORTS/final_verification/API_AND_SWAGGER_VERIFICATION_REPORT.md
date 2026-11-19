# API and Swagger verification

## Result

**44 HTTP checks passed** on a loopback Uvicorn server. **10 documentation checks passed**, including rendering both documentation UIs in Chromium. The [API matrix](API_TEST_RESULTS.csv) points to response headers and bodies by test ID. [Generated OpenAPI](raw/http-final/openapi.json) is semantically identical to the checked-in schema.

## Endpoint inventory

| Method and path | Input and validation | Response and side effects | Evidence |
| --- | --- | --- | --- |
| GET `/` | No required input | 200 service links. No state change. | H01 |
| GET `/health` | No required input | 200 liveness and service name. | H02, H44 |
| GET `/api/status` | No required input | 200 state, current job key, source label, diagnostic and output URLs where available. | H03, H27, H29, H36 |
| POST `/api/upload` | Required multipart `file`; optional `replay` and `no_plates`. Extension, empty file and configured size checks apply. | 202 queues a job; 400, 409, 413, 415, 422 or 500 for relevant request/storage faults. An accepted job can fail asynchronously. | H19–H23, H28–H30 |
| POST `/api/live/start` | Required string `source`; optional `replay` and `no_plates`. Accepts a numeric camera device or RTSP(S) URL. | 202 accepts a job; 400, 409, 422 or 500 when applicable. | H13–H16, H26, H31 |
| POST `/api/live/stop` | No required input | 200 returns job state. Signals cooperative cancellation for an active job. | H12, H34, H36 |
| GET `/api/video/uploaded` | No required input | 200 inventory with file sizes; structured 500 on the injected internal exception. | H08, H24, H25, H43 |
| GET `/api/frames/latest` | No required input | Structured 501. Standalone still-image route remains unavailable. | H09 |
| GET, HEAD `/api/output/stream` | Optional Boolean `live`; optional HTTP byte range for finalized video. | 200 MP4 or live MJPEG; 206 byte range; 400 malformed range; 404 unavailable; 409 pending; 416 outside file; 422 invalid query. HEAD returns headers. | H10, H11, H32, H33, H35, H37–H42 |
| GET `/openapi.json`, `/docs`, `/redoc`, `/docs/oauth2-redirect` | No required input | 200 framework documentation endpoints. | H04–H07 |

S1 has no built-in authentication. The job ID is a status correlation key; no route accepts it as input. The framework OAuth redirect page does not represent an S1 authentication policy. Bundled S2 authentication is covered by its separate test suite.

## Live verification findings

The API returned structured validation errors for missing fields, invalid types, malformed JSON and invalid queries. The file upload endpoint rejected empty, oversized and unsupported files, and recovered after the controlled storage obstruction. Invalid configuration and corrupt media reached terminal error states with diagnostics. Unsupported methods returned 405 and unknown paths returned 404. The second start request was rejected while the first job was active. Cancellation occurred while a streaming request was open. The MJPEG closing boundary, JPEG decode, final report and partial MP4 are preserved.

Starlette returns protocol-level range errors directly. A malformed range returns a text response and an unsatisfiable range includes its range header. The schema distinguishes these from application error envelopes.

The OpenAPI check compared the live route/method inventory and request and response declarations, including upload multipart requirements, camera request examples, health and streaming media types. [Schema checks](raw/http-final/swagger-checks.json) and [browser checks](raw/http-final/browser-result.json) are separate. The initial unmanaged Chromium attempt timed out. The second headless run fetched public documentation assets through the workspace HTTP proxy and verified visible operations in [Swagger](raw/http-final/swagger.png) and [ReDoc](raw/http-final/redoc.png). The server log records both startup and completed application shutdown after SIGTERM.

## Scope limits

Physical device and RTSP connectivity were outside this controlled environment. Native cancellation cannot force a backend call to return. Job-level auth and hard request deadlines are unavailable on the S1 routes tested here.
