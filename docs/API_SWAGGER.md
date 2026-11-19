# Smart Parking S1 HTTP API

Run from the project root using `python -m uvicorn s1.api:app --host 127.0.0.1 --port 8000`. Swagger UI is at `/docs`, ReDoc at `/redoc` and the live contract at `/openapi.json`. The version 1.2.0 [schema snapshot](openapi.json) includes the new job states and streaming responses.

| Method | Path | Result |
| --- | --- | --- |
| GET | `/` | Service links |
| GET | `/health` | Process liveness |
| GET | `/api/status` | Current or most recent job and available output URLs |
| POST | `/api/upload` | Multipart video upload and HTTP 202 |
| POST | `/api/live/start` | Camera index or RTSP/RTSPS URL and HTTP 202 |
| POST | `/api/live/stop` | Request graceful cancellation of the current upload or camera job |
| GET | `/api/video/uploaded` | Saved upload references and sizes |
| GET | `/api/frames/latest` | HTTP 501 |
| GET, HEAD | `/api/output/stream` | Finalized MP4. Add `?live=true` for the active job’s MJPEG preview. |

## Start and stop jobs

```bash
curl -F 'file=@test-clip.mp4' 'http://127.0.0.1:8000/api/upload?replay=sync&no_plates=true'
curl -H 'Content-Type: application/json' -d '{"source":"0","no_plates":true}' http://127.0.0.1:8000/api/live/start
curl http://127.0.0.1:8000/api/status
curl -X POST http://127.0.0.1:8000/api/live/stop
```

The worker processes one source per job. With a `cameras:` configuration it uses the first configured camera's calibration. HTTP 202 acknowledges a new job. Poll status for `starting`, `running`, `cancelling`, `cancelled`, `completed` or `error`. The idle state reports that no job has run.

A stop request returns HTTP 200 and `cancelling` while the worker finishes an in-flight frame or inference batch, releases capture, drains queued evidence work, flushes pending lifecycle events and finalizes outputs. Poll until the status is terminal and `worker_alive` is false. Repeated stop requests are safe. When no job is active, the response reflects its current state. Startup cancellation can finish before any new report or video is produced. Cancellation timing depends on the capture and model backend. A cancelled transcode uses the playable MP4 writer output when available.

## Stream video

Video is available for a `cameras:` configuration with `demo_video.enabled: true`. Other configurations still support processing and cancellation. Use `live_output_url` and `output_url` from job status.

```bash
curl --no-buffer 'http://127.0.0.1:8000/api/output/stream?live=true' -o preview.mjpeg
curl -I 'http://127.0.0.1:8000/api/output/stream'
curl -H 'Range: bytes=0-1023' 'http://127.0.0.1:8000/api/output/stream' -o first-kib.bin
```

With `live=true`, GET serves `multipart/x-mixed-replace` with JPEG frames, one latest frame per job, and a closing multipart boundary at job termination. The stream can skip intermediate frames for slow viewers. Use `<img>` for an MJPEG preview and a video player for finalized MP4. HEAD returns preview headers. Live preview returns 409 during initialization and 404 when unavailable. Opening an MJPEG preview has no effect on the processing job's lifetime.

The default GET streams the latest job's immutable finalized file as `video/mp4`. HEAD returns content headers without a body. A supported Range request returns 206. A malformed Range returns 400, and an unsatisfiable range returns 416 with `Content-Range`. The finalized endpoint returns 409 while processing or cancelling and 404 if the latest job has no finalized video. The service can serve a playable partial clip after cancellation. H.264 export uses FFmpeg when available. MP4V fallback requires a compatible player.

Output URLs describe the current process's latest job. Each job writes to a unique filename in the API output directory, and files remain there for operator-managed retention. Keep files in place while serving clients. OpenAPI and Swagger describe the endpoint and media types. Use a streaming client for live preview.

## Error and configuration details

Application errors include a human-readable `detail` and a structured `error` object. Internal stack traces stay in service logs. The file response uses Starlette's protocol-level range errors directly. Status sanitizes source references and includes a diagnostic for asynchronous failures.

| Environment variable | Default |
| --- | --- |
| `SMART_PARKING_CONFIG` | `config/config.local.yaml` under the project root |
| `SMART_PARKING_UPLOAD_DIR` | `data/uploads` under the project root |
| `SMART_PARKING_OUTPUT_DIR` | `data/api_output` under the project root |
| `SMART_PARKING_MAX_UPLOAD_MB` | 2048 MiB |
| `SMART_PARKING_API_PUBLISH` | `0` for local preview. Set to `1` only for intentional publishing. |

Run one server process because the job state and stream are held in memory. The API is intended for trusted access. Control access to processed video, which can include vehicle and plate evidence.
