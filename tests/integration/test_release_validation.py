import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import sqlite3
from threading import Event, Thread
import time
from types import SimpleNamespace

import cv2
from fastapi.testclient import TestClient
import numpy as np
import pytest

from s1 import api
from s1.config import load_config
from s1.main import run
from s1.publishing.outbox import Outbox
from s1.video.demo_video import DemoRecorder
from s1.video.grabber import LatestFrameGrabber
from s1.pipeline import Pipeline


@pytest.fixture
def local_job(tmp_path):
    geometry = tmp_path / "geometry.json"
    geometry.write_text(json.dumps({
        "frame_size": [96, 64], "tripwire": [[0, 24], [95, 24]],
        "inside_point": [48, 58],
        "slots": [{"slot_id": "test-slot", "polygon": [[10, 30], [60, 30], [60, 60], [10, 60]]}],
    }))
    clip = tmp_path / "input.mp4"
    writer = cv2.VideoWriter(str(clip), cv2.VideoWriter_fourcc(*"mp4v"), 10, (96, 64))
    assert writer.isOpened()
    for i in range(14):
        writer.write(np.full((64, 96, 3), 95 + i, dtype=np.uint8))
    writer.release()
    report = tmp_path / "report.json"
    cfg = load_config(overrides={
        "replay": "sync", "parking_area_id": "test-area",
        "plate": {"enabled": False}, "global_reid": {"enabled": False},
        "inference": {"motion_gate": {"enabled": False}},
        "camera_motion": {"enabled": False},
        "cameras": [{"camera_id": "test-camera", "geometry_file": str(geometry),
                     "source": str(clip)}],
        "publisher": {"snapshot_dir": str(tmp_path / "snapshots")},
        "report": {"path": str(report), "plate_evidence_dir": str(tmp_path / "evidence")},
        "demo_video": {"enabled": True, "fps": 10, "tile_height": 64},
        "exit_restore": {"preview_db_dir": str(tmp_path / "db")},
    })
    return cfg, clip, report


def test_cancel_upload_finalizes_report_and_range_video(tmp_path, monkeypatch, local_job):
    cfg, clip, report = local_job
    entered, release = Event(), Event()
    calls = 0

    def detect_batch(images):
        nonlocal calls
        calls += 1
        if calls == 3:
            entered.set()
            assert release.wait(5)
        return [np.empty((0, 6)) for _ in images]

    detector = SimpleNamespace(detect_batch=detect_batch)
    monkeypatch.setattr(api, "_status", {"state": "idle"})
    monkeypatch.setattr(api, "_output_path", None)
    monkeypatch.setattr(api, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(api, "OUTPUT_DIR", tmp_path / "videos")
    monkeypatch.setattr(api, "PUBLISH_ENABLED", False)
    monkeypatch.setattr(api, "_make_job_config", lambda *args: cfg)
    monkeypatch.setattr(api, "run", lambda config, **kwargs: run(config, detector=detector, **kwargs))

    with TestClient(api.app) as client:
        try:
            assert client.get("/api/output/stream").json()["error"]["code"] == "API_OUTPUT_UNAVAILABLE"
            with clip.open("rb") as stream:
                accepted = client.post("/api/upload", files={"file": ("input.mp4", stream)})
            assert accepted.status_code == 202
            assert entered.wait(5)
            live_head = client.head("/api/output/stream?live=true")
            assert live_head.status_code == 200 and not live_head.content
            assert live_head.headers["content-type"].startswith("multipart/x-mixed-replace;")
            assert "content-length" not in live_head.headers
            with api._lock:
                worker = api._worker
            pending = client.get("/api/output/stream")
            assert pending.status_code == 409
            assert pending.json()["error"]["code"] == "API_OUTPUT_PENDING"
            assert client.post("/api/live/stop").json()["state"] == "cancelling"
            assert client.post("/api/live/stop").json()["state"] == "cancelling"
            assert client.post("/api/live/start", json={"source": "0"}).status_code == 409
        finally:
            release.set()
        worker.join(10)
        assert not worker.is_alive()
        state = client.get("/api/status").json()
        assert state["state"] == "cancelled" and not state["worker_alive"]
        assert state["job_id"] == accepted.json()["job_id"]
        assert client.post("/api/live/stop").json()["state"] == "cancelled"
        assert calls == 3
        result = json.loads(report.read_text())
        assert result["processing_status"] == "cancelled"
        assert not result["lifecycle_events"]
        assert not result["service2_events"]

        [video] = list(api.OUTPUT_DIR.iterdir())
        cap = cv2.VideoCapture(str(video))
        try:
            ok, frame = cap.read()
            assert ok and frame[:64].mean() > 20
        finally:
            cap.release()
        url = state["output_url"]
        head = client.head(url)
        assert head.status_code == 200 and not head.content
        assert head.headers["content-type"] == "video/mp4"
        assert head.headers["accept-ranges"] == "bytes"
        assert int(head.headers["content-length"]) == video.stat().st_size
        assert head.headers["content-disposition"].startswith("inline")
        with client.stream("GET", url) as response:
            assert response.status_code == 200
            assert b"".join(response.iter_bytes()) == video.read_bytes()
        part = client.get(url, headers={"Range": "bytes=0-15"})
        assert part.status_code == 206
        assert part.content == video.read_bytes()[:16]
        assert part.headers["content-range"] == f"bytes 0-15/{video.stat().st_size}"
        outside = client.get(url, headers={"Range": "bytes=100000000-"})
        assert outside.status_code == 416
        assert outside.headers["content-range"] == f"bytes */{video.stat().st_size}"
        assert client.get(url, headers={"Range": "bytes=bad"}).status_code == 400
        video.unlink()
        assert client.get(url).status_code == 404


def test_cancel_single_camera_flushes_at_last_observation(local_job, monkeypatch, capsys):
    cfg, clip, _ = local_job
    cfg.update(cfg.pop("cameras")[0])
    cfg["source"] = str(clip)
    cfg["replay_start_time"] = 100.0
    cancel = Event()
    flushed_at = []
    real_flush = Pipeline.flush

    def flush(pipeline, timestamp):
        flushed_at.append(timestamp)
        return real_flush(pipeline, timestamp)

    def detect(image):
        cancel.set()
        return np.empty((0, 6))

    monkeypatch.setattr(Pipeline, "flush", flush)
    run(cfg, detector=SimpleNamespace(detect=detect), no_publisher=True, cancel_event=cancel)
    assert flushed_at == [100.0]
    assert '"processing_status": "cancelled"' in capsys.readouterr().out


def test_grabber_stop_joins_capture_thread(local_job):
    _, clip, _ = local_job
    grabber = LatestFrameGrabber(str(clip), paced=True).start()
    try:
        assert grabber.read(timeout=3) is not None
        grabber.pause()
    finally:
        grabber.stop()
    assert not grabber._thread.is_alive()


def test_unplayable_recorder_does_not_replace_existing_video(tmp_path, monkeypatch):
    target = tmp_path / "video.mp4"
    target.write_bytes(b"previous output")
    recorder = DemoRecorder(str(target), ["test-camera"])
    recorder.raw_path = recorder._temp_video_path()
    recorder.frames_written = 1
    monkeypatch.setattr(recorder, "_playable", lambda path: False)
    assert recorder.close() is None
    assert recorder.close() is None
    assert target.read_bytes() == b"previous output"
    assert list(tmp_path.iterdir()) == [target]


def test_outbox_uses_isolated_authenticated_destination(tmp_path, monkeypatch):
    key = secrets.token_urlsafe(24)
    fake_key = base64.b64encode(secrets.token_bytes(32)).decode()
    monkeypatch.setenv("S2_API_KEY", key)
    monkeypatch.setenv("S2_API_KEY_HEADER", "X-API-Key")
    monkeypatch.setenv("SP_ENCRYPTION_KEYS", json.dumps({"test": fake_key}))
    monkeypatch.setenv("SP_ACTIVE_ENCRYPTION_KEY", "test")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    delivered = Event()
    data = []

    class Sink(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            data.append((self.path, self.headers.get("X-API-Key"), json.loads(payload)))
            self.send_response(202 if self.headers.get("X-API-Key") == key else 401)
            self.end_headers()
            delivered.set()

    database = str(tmp_path / "outbox.db")
    with ThreadingHTTPServer(("127.0.0.1", 0), Sink) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            queue = Outbox(database, f"http://127.0.0.1:{server.server_port}/test-events")
            event = {"event_id": "isolated-release-event", "event_type": "ENTRY"}
            try:
                queue.publish(event)
                assert delivered.wait(5)
                deadline = time.monotonic() + 5
                while queue.pending() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert queue.pending() == 0
                assert data == [("/test-events", key, event)]
            finally:
                assert queue.stop()
            with sqlite3.connect(database) as db:
                payload, sent = db.execute("SELECT payload, sent FROM outbox").fetchone()
            assert payload.startswith("enc:v1:") and event["event_id"] not in payload
            assert sent == 1
        finally:
            server.shutdown()
            thread.join(5)


def test_cancel_during_camera_startup_leaves_no_false_free_observation(
        local_job, monkeypatch, tmp_path):
    from s1.multicam import multicam

    cfg, _, report = local_job
    cancel = Event()
    source_stopped = Event()

    def read(*args, **kwargs):
        cancel.set()
        return None

    camera = SimpleNamespace(read=read, stop=source_stopped.set, eof=False)
    monkeypatch.setattr(multicam, "open_source", lambda config: camera)
    cfg["demo_video"]["path"] = str(tmp_path / "never-written.mp4")
    detector = SimpleNamespace(detect_batch=lambda _: pytest.fail("inference after cancellation"))
    assert run(cfg, detector=detector, no_publisher=True, cancel_event=cancel) is None
    assert source_stopped.is_set()
    result = json.loads(report.read_text())
    assert result["processing_status"] == "cancelled"
    assert result["summary"]["slots_free"] == 0
    assert not result["failed_camera_ids"]
    assert not (tmp_path / "never-written.mp4").exists()


def test_cancelled_run_surfaces_report_failure_and_preserves_previous_file(
        local_job, monkeypatch):
    from s1.common.errors import ProcessingError
    from s1.reporting import report as reporting

    cfg, _, report = local_job
    cfg["demo_video"]["enabled"] = False
    report.write_text('{"previous": "valid"}')
    cancel = Event()

    def detect_batch(images):
        cancel.set()
        return [np.empty((0, 6)) for _ in images]

    def fail_dump(*args, **kwargs):
        args[1].write("partial data")
        raise OSError("injected disk error")

    monkeypatch.setattr(reporting.json, "dump", fail_dump)
    with pytest.raises(ProcessingError, match="REPORT_GENERATION_FAILED"):
        run(cfg, detector=SimpleNamespace(detect_batch=detect_batch),
            no_publisher=True, cancel_event=cancel)
    assert json.loads(report.read_text()) == {"previous": "valid"}


def test_live_preview_keeps_only_latest_frame_and_terminates():
    import asyncio

    async def read_preview():
        preview = api.LivePreview()
        preview.publish(b"superseded")
        preview.publish(b"latest")
        stream = preview.stream()
        first = await anext(stream)
        assert first == (
            b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 6\r\n\r\nlatest\r\n"
        )
        preview.close()
        preview.publish(b"after closing")
        assert [part async for part in stream] == [b"--frame--\r\n"]

    asyncio.run(read_preview())


def test_shared_inference_recovers_without_inventing_a_parking_transition(local_job):
    cfg, _, report = local_job
    cfg["demo_video"]["enabled"] = False
    cfg["detector"]["max_fps"] = 10
    cfg["tracker"]["min_hits"] = 2
    cfg["parking"].update(baseline_stationary_s=0.2, baseline_window_s=0.8)
    calls = 0

    def detect_batch(images):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("one failed batch")
        return [np.array([[15, 32, 45, 55, 0.95, 2]]) for _ in images]

    run(cfg, detector=SimpleNamespace(detect_batch=detect_batch), no_publisher=True)
    result = json.loads(report.read_text())
    [slot] = result["slot_details"]
    assert result["processing_failures"] == 1 and result["processing_status"] == "partial"
    assert slot["state"] == "OCCUPIED"
    assert slot["current_vehicle"]["occupancy_source"] == "INITIAL_SCAN"
    assert not result["lifecycle_events"]
