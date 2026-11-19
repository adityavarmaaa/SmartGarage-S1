"""FastAPI wrapper for Smart Parking Service 1.

Configuration:
  SMART_PARKING_CONFIG=config/config.local.yaml
  SMART_PARKING_MAX_UPLOAD_MB=2048

Run from the project root:
  uvicorn s1.api:app --host 0.0.0.0 --port 8000
"""
import asyncio
import logging
import stat
from contextlib import asynccontextmanager
import os
import threading
import uuid
from pathlib import Path
from typing import Optional, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from .config import load_config
from .common.errors import ConfigurationError, Diagnostic, ProcessingError
from .main import run

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("s1.api")

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get(
    "SMART_PARKING_CONFIG", str(ROOT / "config" / "config.local.yaml")
))
UPLOAD_DIR = Path(os.environ.get("SMART_PARKING_UPLOAD_DIR", str(ROOT / "data" / "uploads")))
OUTPUT_DIR = Path(os.environ.get("SMART_PARKING_OUTPUT_DIR", str(ROOT / "data" / "api_output")))
MAX_UPLOAD_BYTES = int(os.environ.get("SMART_PARKING_MAX_UPLOAD_MB", "2048")) * 1024 * 1024
if MAX_UPLOAD_BYTES <= 0:
    raise ValueError("SMART_PARKING_MAX_UPLOAD_MB must be positive")
PUBLISH_ENABLED = os.environ.get("SMART_PARKING_API_PUBLISH", "0") == "1"
ALLOWED_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".m4v"}

@asynccontextmanager
async def lifespan(app):
    try:
        yield
    finally:
        with _lock:
            worker = _worker
            if _cancel_event is not None:
                _cancel_event.set()
        if worker is not None:
            await run_in_threadpool(worker.join, 30)
            if worker.is_alive():
                log.warning("API_SHUTDOWN_TIMEOUT worker still finalizing")


app = FastAPI(
    lifespan=lifespan,
    title="Smart Parking — Service 1 API",
    description=(
        "Video upload and live-camera processing API. Interactive Swagger UI is at /docs; "
        "OpenAPI schema is at /openapi.json. Processing is asynchronous and one job is "
        "allowed at a time. Jobs support cooperative cancellation and finalized MP4 delivery."
    ),
    version="1.2.0",
    contact={"name": "Smart Parking Engineering"},
)

_lock = threading.Lock()
_worker: Optional[threading.Thread] = None
_cancel_event: Optional[threading.Event] = None
_output_path: Optional[Path] = None
_preview = None
_status = {
    "state": "idle", "source": None, "filename": None,
    "job_id": None, "error": None, "diagnostic": None,
}


class LivePreview:
    """A single latest JPEG shared by clients of one processing job."""

    media_type = "multipart/x-mixed-replace; boundary=frame"

    def __init__(self):
        self._lock = threading.Lock()
        self._frame = None
        self._closed = False

    def publish(self, frame):
        with self._lock:
            if not self._closed:
                self._frame = frame

    def close(self):
        with self._lock:
            self._closed = True

    async def stream(self):
        previous = None
        while True:
            with self._lock:
                frame, closed = self._frame, self._closed
            if frame is not None and frame is not previous:
                previous = frame
                headers = f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame)}\r\n\r\n"
                yield headers.encode() + frame + b"\r\n"
            if closed:
                yield b"--frame--\r\n"
                return
            await asyncio.sleep(0.05)


class LiveStartRequest(BaseModel):
    source: str = Field(
        ...,
        min_length=1,
        description="Camera index or RTSP/RTSPS stream URL.",
        examples=["0", "rtsp://camera-host/stream"],
    )
    replay: Literal["realtime", "sync"] = "realtime"
    no_plates: bool = False


class ErrorDiagnostic(BaseModel):
    code: str
    module: str
    component: str
    operation: str
    category: str
    message: str
    recoverable: bool
    result: str
    action: str
    severity: str
    context: dict
    timestamp: str


class ErrorResponse(BaseModel):
    detail: str
    error: ErrorDiagnostic


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str


class ServiceLinks(BaseModel):
    service: str
    health: str
    docs: str
    openapi: str


class StopResponse(BaseModel):
    state: str
    message: str


class UploadedFile(BaseModel):
    filename: str
    size_bytes: int = Field(ge=0)


class UploadedFiles(BaseModel):
    files: list[UploadedFile]


def _error_responses(**descriptions):
    return {int(status.removeprefix("s")): {"model": ErrorResponse, "description": description}
            for status, description in descriptions.items()}


def _error_response(request, status, code, message, *, headers=None):
    internal = status >= 500 and status != 501
    category = ("internal" if internal else "unsupported" if status == 501
                else "conflict" if status == 409 else "validation")
    error = Diagnostic(
        code, "API", "HTTP", request.method, category, message, status == 409,
        "failed", "Request ended without starting a new job.",
        severity="ERROR" if internal else "WARNING",
    )
    return JSONResponse(status_code=status, content={"detail": message, "error": error.as_dict()},
                        headers=headers)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    codes = {
        400: "API_INVALID_REQUEST", 404: "API_ROUTE_NOT_FOUND",
        409: "API_JOB_ACTIVE", 413: "API_UPLOAD_TOO_LARGE",
        415: "API_UNSUPPORTED_VIDEO_FORMAT", 500: "API_STORAGE_FAILED",
        501: "API_FEATURE_UNAVAILABLE",
    }
    if request.url.path == "/api/output/stream":
        codes.update({404: "API_OUTPUT_UNAVAILABLE", 409: "API_OUTPUT_PENDING"})
    return _error_response(request, exc.status_code,
                           codes.get(exc.status_code, "API_REQUEST_FAILED"),
                           str(exc.detail), headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    # Validation exceptions include the supplied values, which can contain credentials.
    return _error_response(request, 422, "API_VALIDATION",
                           "Request fields are missing or invalid. See the endpoint schema.")


@app.exception_handler(Exception)
async def _internal_error(request: Request, exc: Exception):
    route = request.scope.get("route")
    log.error("API_INTERNAL method=%s route=%s", request.method,
              getattr(route, "path", None), exc_info=exc)
    return _error_response(request, 500, "API_INTERNAL", "The API could not complete the request.")


class JobAccepted(BaseModel):
    job_id: str
    state: Literal["starting"]
    source: str
    filename: Optional[str] = None


class JobStatus(BaseModel):
    state: Literal["idle", "starting", "running", "cancelling", "cancelled", "completed", "error"]
    source: Optional[str] = None
    filename: Optional[str] = None
    job_id: Optional[str] = None
    error: Optional[str] = None
    diagnostic: Optional[ErrorDiagnostic] = None
    worker_alive: bool
    output_url: Optional[str] = None
    live_output_url: Optional[str] = None


def _public_source(source):
    if source.lower().startswith(("rtsp://", "rtsps://")):
        parsed = urlsplit(source)
        return f"{parsed.scheme}://{parsed.netloc.rsplit('@', 1)[-1]}"
    return Path(source).name


def _discard_upload(destination):
    try:
        destination.unlink(missing_ok=True)
    except OSError:
        log.exception("API_UPLOAD_CLEANUP_FAILED")


def _set_status(**changes):
    with _lock:
        _status.update(changes)


def _make_job_config(source: str, replay: str, no_plates: bool):
    """Load the selected project config and target one configured camera.

    The API accepts one uploaded/live source per job. For a multi-camera project
    config, the first camera supplies the geometry/calibration template; the
    requested source replaces its video source. Other cameras are not silently
    started by this single-source API.
    """
    cfg = load_config(str(CONFIG_PATH))
    cameras = cfg.get("cameras")
    if isinstance(cameras, list) and cameras:
        camera = dict(cameras[0])
        camera["source"] = source
        camera["replay"] = replay
        if no_plates:
            camera["plate"] = {**camera.get("plate", {}), "enabled": False}
        cfg["cameras"] = [camera]
    else:
        cfg["source"] = source
    cfg["replay"] = replay
    if replay == "sync" and not Path(source).is_file():
        cfg["replay"] = "realtime"
        for camera in cfg.get("cameras", []):
            camera["replay"] = "realtime"
    if no_plates:
        cfg.setdefault("plate", {})["enabled"] = False
    return cfg


def _worker_run(source: str, replay: str, no_plates: bool, job_id: str, cancel_event):
    global _worker, _cancel_event, _output_path, _preview
    preview = None
    log.info("Starting job=%s replay=%s plates=%s", job_id, replay, not no_plates)
    try:
        output = None
        if not cancel_event.is_set():
            cfg = _make_job_config(source, replay, no_plates)
            expected_path = None
            if cfg.get("cameras") and cfg.get("demo_video", {}).get("enabled"):
                expected_path = OUTPUT_DIR.resolve() / f"{job_id}.mp4"
                cfg["demo_video"] = {**cfg["demo_video"], "path": str(expected_path)}
                preview = LivePreview()
            with _lock:
                if not cancel_event.is_set():
                    _status["state"] = "running"
                    _preview = preview
            result = run(cfg, show=False, no_publisher=not PUBLISH_ENABLED, cancel_event=cancel_event,
                         frame_sink=preview.publish if preview is not None else None)
            if expected_path is not None and result is not None and Path(result) == expected_path:
                output = expected_path
        with _lock:
            _output_path = output
            _status["state"] = "cancelled" if cancel_event.is_set() else "completed"
        log.info("Finished job=%s cancellation_requested=%s", job_id, cancel_event.is_set())
    except (Exception, SystemExit) as exc:
        original = getattr(exc, "diagnostic", None)
        code = original.code if isinstance(exc, (ConfigurationError, ProcessingError)) else "API_JOB_FAILED"
        module = original.module if original is not None else "API"
        message = ("Job configuration could not be loaded." if isinstance(exc, ConfigurationError)
                   else "Processing could not complete. Consult the job's internal logs.")
        error = Diagnostic(
            code, module, "processing worker", "execute", "processing",
            message, False, "failed", "Released the worker slot.",
            context={"job_id": job_id},
        )
        error.log(log, exc_info=True)
        _set_status(state="error", error=message, diagnostic=error.as_dict())
    finally:
        if preview is not None:
            preview.close()
        with _lock:
            if _worker is threading.current_thread():
                _worker = None
                _cancel_event = None
                _preview = None
            final_state = _status["state"]
        log.info("[JOB %s] Worker stopped; state=%s", job_id, final_state)


def _start_worker(source: str, replay: str, no_plates: bool, filename=None):
    global _worker, _cancel_event, _output_path
    with _lock:
        if _worker is not None and _worker.is_alive():
            raise HTTPException(
                status_code=409,
                detail="A processing job is already running. Wait for it to finish before starting another.",
            )
        job_id = uuid.uuid4().hex
        _cancel_event = threading.Event()
        _output_path = None
        _status.update(
            state="starting", source=_public_source(source), filename=filename,
            job_id=job_id, error=None, diagnostic=None,
        )
        _worker = threading.Thread(
            target=_worker_run,
            args=(source, replay, no_plates, job_id, _cancel_event),
            daemon=True,
            name=f"s1-job-{job_id[:8]}",
        )
        try:
            _worker.start()
        except RuntimeError:
            _worker = None
            _cancel_event = None
            _status.update(state="error", error="Could not start the processing worker.")
            raise
    return {"job_id": job_id, "state": "starting", "source": _public_source(source), "filename": filename}


@app.get("/", tags=["Health"], response_model=ServiceLinks)
def root():
    return {
        "service": "Smart Parking Service 1",
        "health": "/health",
        "docs": "/docs",
        "openapi": "/openapi.json",
    }


@app.get("/health", tags=["Health"], response_model=HealthResponse,
         description="Process liveness. Job inputs and camera availability are checked during processing.")
def health():
    return {"status": "ok", "service": "smart-parking-s1"}


@app.get("/api/status", response_model=JobStatus, tags=["Processing"])
def get_status():
    with _lock:
        result = dict(_status)
        worker = _worker
        result["live_output_url"] = (
            "/api/output/stream?live=true"
            if _preview is not None and result["state"] == "running" else None
        )
        result["output_url"] = "/api/output/stream" if _output_path is not None else None
    result["worker_alive"] = bool(worker is not None and worker.is_alive())
    return result


@app.post(
    "/api/upload", status_code=202, response_model=JobAccepted,
    tags=["Video Upload"],
    summary="Upload a video and start asynchronous processing",
    description="Stores the upload and queues one background job. HTTP 202 acknowledges acceptance. Check /api/status for the result.",
    responses=_error_responses(s400="Invalid filename or empty video", s409="Job active",
                               s413="Configured size limit exceeded", s415="Unsupported extension",
                               s422="Invalid request fields", s500="Storage or startup failed"),
)
async def upload_video(
    file: UploadFile = File(..., description="Video file: MP4, AVI, MOV, MKV or M4V."),
    replay: Literal["realtime", "sync"] = "realtime",
    no_plates: bool = False,
):
    original_name = (file.filename or "").strip()
    filename = Path(original_name.replace(chr(92), '/')).name
    if not filename or filename in {".", ".."}:
        await file.close()
        raise HTTPException(status_code=400, detail="A valid filename is required.")
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        await file.close()
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported video extension. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    destination = UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
    def save_and_start():
        total = 0
        created = False
        accepted = False
        try:
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as output:
                created = True
                while chunk := file.file.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=f"Upload exceeds configured limit of {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
                        )
                    output.write(chunk)
            if total == 0:
                raise HTTPException(status_code=400, detail="Uploaded video is empty.")
            result = _start_worker(str(destination), replay, no_plates, filename)
            accepted = True
            return result
        except OSError as exc:
            log.exception("API_STORAGE_FAILED while saving upload")
            raise HTTPException(status_code=500, detail="Could not save uploaded video.") from exc
        finally:
            if created and not accepted:
                _discard_upload(destination)

    try:
        return await run_in_threadpool(save_and_start)
    finally:
        await file.close()


@app.post(
    "/api/live/start", status_code=202, response_model=JobAccepted,
    tags=["Live Camera"], summary="Start processing a camera or RTSP source",
    description="Uses the first configured camera's calibration. Live input uses realtime processing.",
    responses=_error_responses(s400="Unsupported camera source", s409="Job active",
                               s422="Invalid request fields", s500="Worker startup failed"),
)
def start_live(request: LiveStartRequest):
    source = request.source.strip()
    try:
        parsed = urlsplit(source)
        is_rtsp = parsed.scheme in ("rtsp", "rtsps") and parsed.hostname and parsed.port != 0
    except ValueError:
        is_rtsp = False
    if not (source.isascii() and source.isdecimal()) and not is_rtsp:
        raise HTTPException(status_code=400, detail="Use an RTSP/RTSPS URL or a non-negative camera index.")
    return _start_worker(source, request.replay, request.no_plates)


@app.post("/api/live/stop", tags=["Live Camera"], response_model=StopResponse,
          description="Requests cancellation of the current live or upload job. Poll job status until cleanup finishes.")
def stop_live():
    with _lock:
        state = _status["state"]
        if state not in {"starting", "running", "cancelling"} or _cancel_event is None:
            return {"state": state, "message": "No active processing job."}
        if state != "cancelling":
            _cancel_event.set()
            _status["state"] = "cancelling"
            log.info("API_CANCEL_REQUESTED job=%s", _status["job_id"])
    return {"state": "cancelling", "message": "Cancellation requested. Poll /api/status for completion."}


@app.get("/api/video/uploaded", tags=["Video Upload"], response_model=UploadedFiles,
         responses=_error_responses(s500="Upload directory unavailable"))
def uploaded_videos():
    files = []
    if not UPLOAD_DIR.exists():
        return {"files": files}
    for path in sorted(UPLOAD_DIR.iterdir()):
        if path.is_file():
            try:
                files.append({"filename": path.name, "size_bytes": path.stat().st_size})
            except OSError:
                log.warning("Could not stat upload file: %s", path)
    return {"files": files}


@app.get("/api/frames/latest", tags=["Processed Output"], status_code=501,
         response_model=ErrorResponse, response_description="Feature unavailable")
def latest_frame():
    raise HTTPException(status_code=501, detail="Processed-frame output is not implemented.")


_video_responses = {
    200: {"description": "Finalized MP4 or live MJPEG",
          "content": {"video/mp4": {}, "multipart/x-mixed-replace": {}}},
    206: {"description": "Byte range", "content": {"video/mp4": {}, "multipart/byteranges": {}}},
    400: {"description": "Invalid Range header", "content": {"text/plain": {}}},
    416: {"description": "Range outside the file", "headers": {"Content-Range": {"schema": {"type": "string"}}}},
    **_error_responses(s404="No finalized video is available", s409="Video pending or preview initializing", s422="Invalid live query value", s500="Video storage unavailable"),
}


@app.head("/api/output/stream", tags=["Processed Output"], response_class=FileResponse,
          responses=_video_responses, description="Returns headers for the finalized demo MP4.")
@app.get("/api/output/stream", tags=["Processed Output"], response_class=FileResponse,
         responses=_video_responses,
         description="Streams a finalized MP4 with byte ranges or the active job's live MJPEG preview.")
def output_stream(request: Request, live: bool = False):
    with _lock:
        state, path, preview = _status["state"], _output_path, _preview
    if live:
        if state == "starting":
            raise HTTPException(409, "The live preview is still initializing.")
        if preview is None or state != "running":
            raise HTTPException(404, "There is no active live preview.")
        headers = {"Cache-Control": "no-store"}
        if request.method == "HEAD":
            response = Response(media_type=preview.media_type, headers=headers)
            del response.headers["content-length"]
            return response
        return StreamingResponse(preview.stream(), media_type=preview.media_type, headers=headers)
    if state in {"starting", "running", "cancelling"}:
        raise HTTPException(409, "Processed video will be available after job finalization.")
    if path is not None:
        try:
            metadata = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            metadata = None
        if metadata is not None and stat.S_ISREG(metadata.st_mode) and metadata.st_size:
            return FileResponse(path, media_type="video/mp4", stat_result=metadata,
                                filename="processed.mp4", content_disposition_type="inline",
                                headers={"Cache-Control": "no-store"})
    raise HTTPException(404, "The latest job has no finalized processed video.")
