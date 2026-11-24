import json
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
from fastapi.testclient import TestClient
import numpy as np
import pytest

from vehicle_intelligence import api
from vehicle_intelligence.common import Frame
from vehicle_intelligence.common.config import load_config
from vehicle_intelligence.common.errors import ConfigurationError, ProcessingError
from vehicle_intelligence.main import MemoryPublisher
from vehicle_intelligence.multicam.multicam import run_multi_camera
from vehicle_intelligence.pipeline import Pipeline
from vehicle_intelligence.geometry import Geometry
from vehicle_intelligence.plates import PlateWorker
from vehicle_intelligence.video.grabber import SequentialFileSource


def geometry():
    return {
        "frame_size": [64, 48],
        "tripwire": [[0, 20], [63, 20]], "inside_point": [31, 40],
        "slots": [{"slot_id": "test-slot", "polygon": [[10, 25], [40, 25], [40, 45], [10, 45]]}],
    }


def config(tmp_path):
    return load_config(overrides={
        "parking_area_id": "test-area",
        "plate": {"enabled": False},
        "inference": {"motion_gate": {"enabled": False}, "camera_startup_wait_s": 0.3},
        "camera_motion": {"enabled": False},
        "global_reid": {"enabled": False},
        "publisher": {"snapshot_dir": str(tmp_path / "snapshots")},
    })


def diagnostic_codes(caplog):
    return [record.diagnostic["code"] for record in caplog.records
            if hasattr(record, "diagnostic")]


@pytest.mark.parametrize("failure, code", [
    (RuntimeError("detector failed"), "YOLO_INFERENCE_FAILED"),
    (np.array([[0, 0, 1, 1, float("nan"), 2]]), "DETECTION_OUTPUT_INVALID"),
])
def test_detection_failure_skips_only_affected_frame(tmp_path, caplog, failure, code):
    empty = np.empty((0, 6))
    detector = SimpleNamespace(detect=Mock(side_effect=[failure, empty]))
    pipe = Pipeline(config(tmp_path), Geometry(geometry()), (64, 48), detector,
                    MemoryPublisher(), None)
    pipe.motion_gate_enabled = False
    tracker = Mock(wraps=pipe.tracker.update)
    pipe.tracker.update = tracker
    image = np.full((48, 64, 3), 90, dtype=np.uint8)

    assert pipe.process(Frame(100.0, image, 0)) == []
    assert not pipe.last_frame_ok
    tracker.assert_not_called()
    assert pipe.process(Frame(100.2, image, 1)) == []
    assert pipe.last_frame_ok
    assert tracker.call_args.args[0].shape == (0, 6)
    assert diagnostic_codes(caplog) == [code]


def test_tracker_failure_preserves_pipeline_for_next_frame(tmp_path, caplog):
    pipe = Pipeline(config(tmp_path), Geometry(geometry()), (64, 48), None,
                    MemoryPublisher(), None)
    update = pipe.tracker.update
    pipe.tracker.update = Mock(side_effect=RuntimeError("track failed"))
    image = np.full((48, 64, 3), 90, dtype=np.uint8)
    empty = np.empty((0, 6))
    assert pipe.process(Frame(100.0, image, 0), detections=empty) == []
    assert not pipe.last_frame_ok
    pipe.tracker.update = update
    assert pipe.process(Frame(100.2, image, 1), detections=empty) == []
    assert pipe.last_frame_ok
    assert diagnostic_codes(caplog) == ["TRACKER_UPDATE_FAILED"]


@pytest.mark.parametrize("ocr_failure", [False, True])
def test_ocr_empty_or_failed_returns_no_reads(caplog, ocr_failure):
    read = Mock(side_effect=RuntimeError("OCR failed")) if ocr_failure else Mock(return_value=[])
    worker = PlateWorker(SimpleNamespace(read=read), deterministic=True)
    crop = np.full((30, 60, 3), 100, dtype=np.uint8)
    try:
        worker.submit(42, crop)
        assert worker.wait_idle(timeout=3)
        [(key, reads, _)] = list(worker.poll())
        assert key == 42 and reads == []
    finally:
        worker.stop()
    assert diagnostic_codes(caplog) == (["ALPR_INFERENCE_FAILED"] if ocr_failure else [])


def test_invalid_configuration_reports_section(tmp_path):
    path = tmp_path / "bad.yml"
    path.write_text("detector:\n  max_fps: 0\n")
    with pytest.raises(ConfigurationError, match="detector.max_fps") as error:
        load_config(path)
    assert error.value.diagnostic.context["file"] == str(path)
    assert not error.value.diagnostic.recoverable


def test_early_video_decode_failure_has_frame_context(tmp_path):
    source = SequentialFileSource(str(tmp_path / "broken.mp4"))
    source.cap.release()
    source.cap = SimpleNamespace(read=lambda: (False, None))
    source.n, source.expected_frames = 2, 5
    with pytest.raises(ProcessingError) as error:
        source.read()
    assert source.eof
    assert error.value.diagnostic.code == "VIDEO_FRAME_READ_FAILED"
    assert error.value.diagnostic.context["frame_id"] == 2


def test_multicam_bad_geometry_keeps_good_camera_and_playable_output(tmp_path, caplog):
    geometry_path = tmp_path / "geometry.json"
    geometry_path.write_text(json.dumps(geometry()))
    clip = tmp_path / "clip.mp4"
    writer = cv2.VideoWriter(str(clip), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 48))
    assert writer.isOpened()
    for _ in range(5):
        writer.write(np.full((48, 64, 3), 115, dtype=np.uint8))
    writer.release()
    report_path = tmp_path / "report.json"
    output_path = tmp_path / "demo.mp4"
    cfg = config(tmp_path)
    cfg.update({
        "replay": "sync",
        "cameras": [
            {"camera_id": "good", "source": str(clip), "geometry_file": str(geometry_path)},
            {"camera_id": "bad", "source": str(clip), "geometry_file": str(tmp_path / "missing.json")},
        ],
        "exit_restore": {"preview_db_dir": str(tmp_path)},
        "report": {"path": str(report_path), "plate_evidence_dir": str(tmp_path / "evidence")},
        "demo_video": {"enabled": True, "path": str(output_path), "fps": 10, "tile_height": 48},
    })
    detector = SimpleNamespace(detect_batch=lambda images: [np.empty((0, 6)) for _ in images])
    run_multi_camera(cfg, detector, no_publisher=True)
    report = json.loads(report_path.read_text())
    assert report["failed_camera_ids"] == ["bad"]
    assert report["processing_status"] == "partial"
    assert "SLOT_GEOMETRY_INVALID" in diagnostic_codes(caplog)
    capture = cv2.VideoCapture(str(output_path))
    try:
        assert capture.get(cv2.CAP_PROP_FPS) == pytest.approx(10)
        ok, first = capture.read()
        assert ok and first.shape == (218, 64, 3)
        assert first[:48].mean() > 20
    finally:
        capture.release()


def test_http_diagnostics_and_openapi(monkeypatch):
    client = TestClient(api.app, raise_server_exceptions=False)
    bad = client.post("/api/live/start", json={"replay": "invalid"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "API_VALIDATION"
    assert "input" not in bad.json()["error"]["context"]
    monkeypatch.setattr(api, "_start_worker", Mock(side_effect=RuntimeError("secret traceback text")))
    failed = client.post("/api/live/start", json={"source": "0"})
    assert failed.status_code == 500 and failed.json()["error"]["code"] == "API_INTERNAL"
    assert "secret traceback text" not in failed.text
    assert client.get("/api/frames/latest").status_code == 501
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json").json()
    upload = spec["paths"]["/api/upload"]["post"]
    assert "multipart/form-data" in upload["requestBody"]["content"]
    assert upload["responses"]["422"]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse")


def test_invalid_ocr_crop_does_not_call_reader(caplog):
    reader = SimpleNamespace(read=Mock())
    worker = PlateWorker(reader, deterministic=True)
    try:
        assert worker.submit("test-vehicle", None)
        assert worker.wait_idle(timeout=3)
        assert list(worker.poll()) == [("test-vehicle", [], None)]
    finally:
        worker.stop()
    reader.read.assert_not_called()
    assert diagnostic_codes(caplog) == ["PLATE_CROP_INVALID"]


def test_report_failure_preserves_previous_report(tmp_path, monkeypatch):
    from vehicle_intelligence.reporting import report

    path = tmp_path / "report.json"
    path.write_text('{"previous": true}')
    def fail_write(*args, **kwargs):
        args[1].write("partial")
        raise OSError("disk full")

    monkeypatch.setattr(report.json, "dump", fail_write)
    with pytest.raises(OSError, match="disk full"):
        report.write_report({"new": True}, path)
    assert path.read_text() == '{"previous": true}'
    assert list(tmp_path.iterdir()) == [path]


def test_failed_worker_status_redacts_source_and_releases_slot(monkeypatch):
    from threading import Event

    done = Event()
    monkeypatch.setattr(api, "_status", dict(api._status))
    monkeypatch.setattr(api, "_make_job_config", Mock(return_value={}))
    def fail_run(cfg, **kwargs):
        assert kwargs["no_publisher"]
        done.set()
        raise RuntimeError("internal source credentials")

    monkeypatch.setattr(api, "run", fail_run)
    monkeypatch.setattr(api, "PUBLISH_ENABLED", False)
    client = TestClient(api.app)
    accepted = client.post("/api/live/start", json={"source": "rtsp://user:secret@camera.test/live"})
    assert accepted.status_code == 202
    assert accepted.json()["source"] == "rtsp://camera.test"
    assert done.wait(timeout=3)
    with api._lock:
        worker = api._worker
    if worker is not None:
        worker.join(timeout=3)
    status = client.get("/api/status")
    assert status.json()["state"] == "error"
    assert status.json()["diagnostic"]["code"] == "API_JOB_FAILED"
    assert not status.json()["worker_alive"]
    assert "secret" not in status.text and "credentials" not in status.text


def test_invalid_lifecycle_event_is_not_published(tmp_path, caplog):
    publisher = MemoryPublisher()
    pipe = Pipeline(config(tmp_path), Geometry(geometry()), (64, 48), None,
                    publisher, None)
    event = {
        "event_id": "invalid-transition", "event_type": "PARK_START",
        "camera_id": pipe.cam, "vehicle_uid": "test-vehicle",
        "event_time": "2026-01-01T00:00:00Z",
    }
    assert not pipe._publish_event(event)
    assert publisher.report_events == []
    assert diagnostic_codes(caplog) == ["EVENT_VALIDATION_FAILED"]


def test_unavailable_occupancy_is_reported_as_unknown(tmp_path):
    from vehicle_intelligence.reporting.report import slot_records_for

    cfg = config(tmp_path)
    pipeline = Pipeline(cfg, Geometry(geometry()), (64, 48), None,
                        MemoryPublisher(), None)
    runtime = SimpleNamespace(pipeline=pipeline, cfg=cfg, occupancy_unavailable=True)
    [slot] = slot_records_for(runtime)
    assert slot["state"] == "UNKNOWN" and slot["vehicle_uid"] is None


@pytest.mark.parametrize("text, confidence", [
    ("", 0.0), ("DL01AB1234", 0.2), ("DL01AB1234", 0.93), ("INVALID", 0.93),
])
def test_unconfirmed_ocr_preserves_entry_contract(tmp_path, text, confidence):
    from vehicle_intelligence.plates import PlateRead
    from vehicle_intelligence.vehicle_front.vehicle_service import Vehicle

    crop = np.full((48, 64, 3), 95, dtype=np.uint8)
    vehicle = Vehicle("test-vehicle", 12, 100.0)
    reads = [PlateRead(text, confidence, 0.9, (5, 5, 40, 15))] if text else []
    plates = SimpleNamespace(poll=Mock(return_value=[(vehicle.uid, reads, crop)]))
    pipe = Pipeline(config(tmp_path), Geometry(geometry()), (64, 48), None,
                    MemoryPublisher(), plates)
    pipe.deterministic = False
    pipe.registry.by_uid[vehicle.uid] = vehicle
    pipe._apply_plate_results()
    events = []
    pipe._build_and_append(events, {
        "vehicle": vehicle, "event_type": "ENTRY", "event_id": "test-entry",
        "event_time": 101.0, "detected_at": 101.2, "direction": "IN",
        "slot": None, "inferred": False,
    })
    [event] = events
    assert event["event_type"] == "ENTRY" and event["vehicle_uid"] == vehicle.uid
    assert event["plate_status"] == "UNCONFIRMED"
    assert event["plate"]["text"] is None
    assert pipe._publish_event(event)
