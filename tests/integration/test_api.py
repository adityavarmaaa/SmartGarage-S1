from fastapi.testclient import TestClient
import vehicle_intelligence.api as api


client = TestClient(api.app)


def test_health_and_root():
    assert client.get("/health").json()["status"] == "ok"
    root = client.get("/").json()
    assert root["docs"] == "/docs"
    assert root["openapi"] == "/openapi.json"


def test_status_shape():
    response = client.get("/api/status")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] in {"idle", "starting", "running", "completed", "error"}
    assert "worker_alive" in body


def test_live_rejects_http_page():
    response = client.post(
        "/api/live/start",
        json={"source": "https://example.com/camera", "replay": "realtime"},
    )
    assert response.status_code == 400


def test_upload_rejects_unsupported_extension():
    response = client.post(
        "/api/upload",
        files={"file": ("notes.txt", b"not a video", "text/plain")},
    )
    assert response.status_code == 415


def test_upload_rejects_empty_video():
    response = client.post(
        "/api/upload",
        files={"file": ("empty.mp4", b"", "video/mp4")},
    )
    assert response.status_code == 400


def test_upload_accepted_and_worker_is_mocked(monkeypatch):
    monkeypatch.setattr(
        api, "_start_worker",
        lambda source, replay, no_plates, filename=None: {
            "job_id": "test-job", "state": "starting",
            "source": source, "filename": filename,
        },
    )
    response = client.post(
        "/api/upload",
        files={"file": ("clip.mp4", b"fake-video-bytes", "video/mp4")},
    )
    assert response.status_code == 202
    assert response.json()["job_id"] == "test-job"


def test_stream_openapi_matches_head_and_range_contract():
    spec = client.get("/openapi.json").json()
    operations = spec["paths"]["/api/output/stream"]
    get = operations["get"]["responses"]
    head = operations["head"]["responses"]
    assert get.keys() == head.keys()
    assert {"video/mp4", "multipart/x-mixed-replace"} <= get["200"]["content"].keys()
    assert all("content" not in response for response in head.values())
    assert {"Content-Type", "Content-Length", "Accept-Ranges"} <= head["200"]["headers"].keys()
    for responses in (get, head):
        assert responses["416"]["headers"]["Content-Range"]["example"] == "bytes */1234"


def test_lifespan_signals_and_joins_active_worker(monkeypatch):
    from threading import Event, Thread

    cancel = Event()
    observed = Event()

    def worker_run():
        if cancel.wait(5):
            observed.set()

    worker = Thread(target=worker_run)
    monkeypatch.setattr(api, "_worker", worker)
    monkeypatch.setattr(api, "_cancel_event", cancel)
    worker.start()
    with TestClient(api.app) as session:
        assert session.get("/health").status_code == 200
        assert worker.is_alive()
    assert observed.is_set()
    assert not worker.is_alive()
