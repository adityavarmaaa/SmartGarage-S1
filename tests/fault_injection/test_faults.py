"""Controlled S1 failures using synthetic fixtures and no production endpoints."""
import errno
import json
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import cv2
import numpy as np
import pytest

from s1.common import Frame
from s1.config import load_config, validate_geometry_for_camera
from s1.common.errors import ProcessingError
from s1.events.event_validation import validate_event
from s1.geometry import Geometry
from s1.main import MemoryPublisher
from s1.multicam.multicam import run_multi_camera
from s1.pipeline import Pipeline
from s1.plates import PlateWorker
from s1.reporting import report
from s1.video import demo_video, grabber
from s1.vehicle_front.detection import YoloDetector


def key():
    return uuid4().hex


@pytest.fixture
def fixture(tmp_path):
    width, height = 96, 64
    cam, slot, area = key(), key(), key()
    geometry = {
        "frame_size": [width, height], "tripwire": [[0, height // 3], [width - 1, height // 3]],
        "inside_point": [width // 2, height - 2],
        "slots": [{"slot_id": slot, "polygon": [[8, 26], [68, 26], [68, 58], [8, 58]]}],
    }
    location = tmp_path / f"{key()}.json"
    location.write_text(json.dumps(geometry))
    source = tmp_path / f"{key()}.mp4"
    image = np.full((height, width, 3), 90, dtype=np.uint8)
    report_path = tmp_path / f"{key()}.json"
    cfg = load_config(overrides={
        "camera_id": cam, "parking_area_id": area, "source": str(source),
        "geometry_file": str(location), "replay": "sync", "plate": {"enabled": False},
        "global_reid": {"enabled": False}, "camera_motion": {"enabled": False},
        "inference": {"motion_gate": {"enabled": False}, "camera_startup_wait_s": 0.4},
        "tracker": {"min_hits": 2}, "detector": {"max_fps": 10},
        "exit_restore": {"preview_db_dir": str(tmp_path)},
        "report": {"path": str(report_path), "plate_evidence_dir": str(tmp_path / "evidence")},
        "publisher": {"snapshot_dir": str(tmp_path / "snapshots"), "s2_url": "http://127.0.0.1:1/isolated"},
    })
    detector = Mock()
    detector.detect.return_value = np.empty((0, 6))
    detector.detect_batch.side_effect = lambda frames: [np.empty((0, 6)) for _ in frames]
    publisher = MemoryPublisher()
    pipe = Pipeline(cfg, Geometry(geometry), (width, height), detector, publisher, None)
    return SimpleNamespace(cfg=cfg, geometry=geometry, image=image, detector=detector,
                           pipe=pipe, publisher=publisher, source=source, location=location,
                           report=report_path)


def codes(caplog):
    return {rec.diagnostic["code"] for rec in caplog.records if hasattr(rec, "diagnostic")}


def write_clip(f):
    h, w = f.image.shape[:2]
    writer = cv2.VideoWriter(str(f.source), cv2.VideoWriter_fourcc(*"mp4v"), 10, (w, h))
    assert writer.isOpened()
    try:
        for _ in range(5):
            writer.write(f.image)
    finally:
        writer.release()


def camera(f, **changes):
    return {**{name: f.cfg[name] for name in ("camera_id", "source", "geometry_file")}, **changes}


@pytest.mark.parametrize("kind", ["empty", "none", "scalar", "corrupt"], ids=["D01", "D02", "D03", "D04"])
def test_invalid_frames_skip_inference_and_recover(fixture, caplog, kind):
    f = fixture
    invalid = {"empty": f.image[:0], "none": None, "scalar": 7,
               "corrupt": f.image.astype(np.float32) * np.nan}[kind]
    assert f.pipe.process(Frame(100, invalid, 0)) == []
    assert not f.pipe.last_frame_ok
    f.detector.detect.assert_not_called()
    assert "VIDEO_FRAME_INVALID" in codes(caplog)
    assert f.pipe.process(Frame(101, f.image, 1)) == []
    assert f.pipe.last_frame_ok
    assert f.pipe.stats.frames == 1


def test_missing_model_has_categorized_failure(tmp_path):
    with pytest.raises(ProcessingError) as issue:
        YoloDetector(str(tmp_path / f"{key()}.pt"))
    assert issue.value.diagnostic.code == "YOLO_MODEL_LOAD_FAILED"
    assert not issue.value.diagnostic.recoverable


@pytest.mark.parametrize("output", [np.zeros((1, 3)), [["broken"] * 6], np.array([[4, 3, 2, 1, 1, 2]])],
                         ids=["D07", "D08", "D09"])
def test_invalid_detection_output_recovers(fixture, caplog, output):
    f = fixture
    f.detector.detect.side_effect = [output, np.empty((0, 6))]
    assert f.pipe.process(Frame(100, f.image, 0)) == []
    assert not f.pipe.last_frame_ok
    assert "DETECTION_OUTPUT_INVALID" in codes(caplog)
    assert f.pipe.process(Frame(101, f.image, 1)) == []
    assert f.pipe.last_frame_ok


@pytest.mark.parametrize("bad_tracks", [[SimpleNamespace()], [SimpleNamespace(id=1)] * 2],
                         ids=["T01", "T02"])
def test_invalid_association_is_categorized(fixture, caplog, bad_tracks):
    f = fixture
    f.pipe.tracker.update = Mock(side_effect=[(bad_tracks, []), ([], [])])
    assert f.pipe.process(Frame(100, f.image, 0)) == []
    assert not f.pipe.last_frame_ok
    assert "TRACKER_OUTPUT_INVALID" in codes(caplog)
    assert f.pipe.process(Frame(101, f.image, 1)) == []
    assert f.pipe.last_frame_ok


def test_tracking_gap_expires_ids_and_reacquires(fixture):
    f = fixture
    box = np.array([[12, 12, 32, 42, 0.98, 2], [52, 12, 75, 42, 0.98, 2]])
    update = f.pipe.tracker.update
    update(box, 100)
    observed, _ = update(box, 100.1)
    assert len(observed) == 2
    ids = {track.id for track in observed}
    _, removed = update(np.empty((0, 6)), 110)
    assert {track.id for track in removed} == ids
    update(box, 111)
    new, _ = update(box, 111.1)
    assert len(new) == 2 and not ids.intersection(track.id for track in new)


def test_ocr_pins_result_keys_and_recovers_after_failure(fixture, caplog):
    f = fixture
    keys = [key() for _ in range(3)]
    crop = f.image.copy()
    entered, release = Event(), Event()
    good = SimpleNamespace(text="", conf=0.8, det_conf=0.9)
    calls = iter([RuntimeError("injected OCR failure"), [{"invalid": True}], [good]])

    def read(_):
        entered.set()
        assert release.wait(4)
        result = next(calls)
        if isinstance(result, Exception):
            raise result
        return result

    worker = PlateWorker(SimpleNamespace(read=read), deterministic=True)
    try:
        for uid in keys:
            assert worker.submit(uid, crop)
        assert entered.wait(2)
        assert not worker.wait_idle(timeout=0.02)
        release.set()
        assert worker.wait_idle(timeout=4)
        results = list(worker.poll())
    finally:
        release.set()
        assert worker.stop()
    assert [uid for uid, _, _ in results] == keys
    assert [reads for _, reads, _ in results] == [[], [], [good]]
    assert "ALPR_INFERENCE_FAILED" in codes(caplog)


def test_disappeared_vehicle_ocr_does_not_update_another(fixture, caplog):
    f = fixture
    from s1.vehicle_front.vehicle_service import Vehicle
    vehicle = Vehicle(key(), 1, 100)
    f.pipe.registry.by_uid[vehicle.uid] = vehicle
    f.pipe.plates = SimpleNamespace(poll=lambda: [(key(), [], f.image)])
    f.pipe._apply_plate_results()
    assert not vehicle.voter.reads
    assert "unknown vehicle" in caplog.text


def test_attribute_inference_fault_preserves_vehicle(fixture, monkeypatch, caplog):
    from s1.integration import vehicle_parking_bridge as bridge
    f = fixture
    f.cfg["color"].update(min_box_h=1, interval_s=0)
    monkeypatch.setattr(bridge, "dominant_color_details", Mock(side_effect=RuntimeError("colour failed")))
    box = np.array([[12, 12, 32, 44, 0.99, 2]])
    for i in range(3):
        f.pipe.process(Frame(100 + i, f.image, i), detections=box)
    assert f.pipe.last_frame_ok
    [v] = f.pipe.registry.by_track.values()
    assert v.vehicle_type() is not None and v.color() is None
    assert "VEHICLE_ATTRIBUTE_FAILED" in codes(caplog)


@pytest.mark.parametrize("size", [[-1, 20], [10], [0, 30]], ids=["G01", "G02", "G03"])
def test_bad_calibration_dimensions_rejected(fixture, size):
    g = Geometry({**fixture.geometry, "frame_size": size})
    with pytest.raises(ValueError, match="frame_size"):
        validate_geometry_for_camera(fixture.cfg, g)


def test_calibration_scaling_and_empty_slots(fixture):
    f = fixture
    g = Geometry({**f.geometry, "slots": []})
    assert validate_geometry_for_camera(f.cfg, g)
    scaled = g.scaled(f.image.shape[1] * 2, f.image.shape[0] * 2)
    assert scaled.inside_point == tuple(coord * 2 for coord in g.inside_point)
    assert not scaled.slots


@pytest.mark.parametrize("timestamp", ["wrong", "2026-01-01T12:00:00"], ids=["L01", "L02"])
def test_invalid_event_time_is_rejected(fixture, timestamp):
    with pytest.raises(ValueError):
        validate_event({"event_id": key(), "camera_id": fixture.cfg["camera_id"],
                        "event_type": "ENTRY", "event_time": timestamp, "vehicle_uid": key()})


@pytest.mark.parametrize("content, code", [(None, "VIDEO_SOURCE_NOT_FOUND"),
                                          (b"", "VIDEO_SOURCE_OPEN_FAILED"),
                                          (b"corrupt media", "VIDEO_SOURCE_OPEN_FAILED")],
                         ids=["V01", "V02", "V03"])
def test_missing_empty_or_corrupt_video_never_starts(fixture, content, code):
    if content is not None:
        fixture.source.write_bytes(content)
    with pytest.raises(ProcessingError) as issue:
        grabber.open_source(fixture.cfg)
    assert issue.value.diagnostic.code == code


def test_video_writer_open_and_write_faults(fixture, tmp_path, monkeypatch, caplog):
    f = fixture
    path = tmp_path / f"{key()}.mp4"
    recorder = demo_video.DemoRecorder(str(path), [f.cfg["camera_id"]])
    view = SimpleNamespace(latest_raw=f.image, pipeline=f.pipe, processed_frames=1)
    bad_open = Mock()
    bad_open.isOpened.return_value = False
    bad_write = Mock()
    bad_write.isOpened.return_value = True
    bad_write.write.side_effect = OSError("injected writer failure")
    monkeypatch.setattr(demo_video.cv2, "VideoWriter", Mock(side_effect=[bad_open, bad_write]))
    for tick in (1, 2):
        recorder.tick({f.cfg["camera_id"]: view}, {}, tick)
    assert recorder.errors == 2
    assert recorder.close() is None
    bad_open.release.assert_called_once()
    bad_write.release.assert_called_once()
    assert not path.exists()
    assert "VIDEO_OUTPUT_FRAME_FAILED" in codes(caplog)


@pytest.mark.parametrize("method", ["decode", "startup", "geometry", "malformed-frame", "tracker"],
                         ids=["M01", "M02", "M03", "M04", "M05"])
def test_camera_failure_leaves_other_camera_processing(fixture, monkeypatch, caplog, method):
    from s1.multicam import multicam
    f = fixture
    write_clip(f)
    bad = key()
    other = camera(f, camera_id=bad)
    if method == "geometry":
        other["geometry_file"] += ".missing"
    cfg = {**f.cfg, "cameras": [other, camera(f)]}
    original = multicam.open_source
    bad_source = Mock()
    bad_source.eof = False
    bad_source.dropped_frames = 0
    bad_source.read.side_effect = [Frame(100, None, 0), Frame(101, f.image, 1), OSError("decoder broke")]

    def open_source(config):
        if config["camera_id"] == bad:
            if method == "startup":
                raise OSError("capture init failed")
            if method == "malformed-frame":
                return bad_source
            if method == "decode":
                good = original(config)
                bad_source.read.side_effect = [good.read(), OSError("decoder broke")]
                bad_source.stop.side_effect = good.stop
                return bad_source
        return original(config)

    factory = multicam.Pipeline
    def make_pipeline(config, *args, **kwargs):
        pipe = factory(config, *args, **kwargs)
        if method == "tracker" and config["camera_id"] == bad:
            pipe.tracker.update = Mock(side_effect=RuntimeError("tracker failed"))
        return pipe

    monkeypatch.setattr(multicam, "open_source", open_source)
    monkeypatch.setattr(multicam, "Pipeline", make_pipeline)
    run_multi_camera(cfg, f.detector, no_publisher=True)
    result = json.loads(f.report.read_text())
    assert result["cameras"][f.cfg["camera_id"]]["processed_frames"] > 0
    assert result["processing_status"] == "partial"
    if method == "tracker":
        assert result["cameras"][bad]["processed_frames"] == 0
        assert result["cameras"][bad]["slots"][0]["state"] == "UNKNOWN"
        assert "TRACKER_UPDATE_FAILED" in codes(caplog)
    else:
        assert bad in result["failed_camera_ids"]
    if method in {"decode", "malformed-frame"}:
        bad_source.stop.assert_called_once()


@pytest.mark.parametrize("err", [errno.ENOSPC, errno.EACCES], ids=["R01", "R02"])
def test_report_write_failure_keeps_previous_version(fixture, monkeypatch, err):
    f = fixture
    f.report.write_text('{"previous":true}')
    def fail(*args, **kwargs):
        raise OSError(err, "controlled persistence failure")
    monkeypatch.setattr(report.os, "replace", fail)
    with pytest.raises(OSError) as issue:
        report.write_report({"new": True}, f.report)
    assert issue.value.errno == err
    assert json.loads(f.report.read_text()) == {"previous": True}
    assert sorted(f.source.parent.glob("tmp*")) == []


def test_non_cancelled_job_cannot_succeed_after_report_failure(fixture):
    f = fixture
    write_clip(f)
    unavailable = f.report.parent / key()
    unavailable.write_text("directory obstruction")
    cfg = {**f.cfg, "cameras": [camera(f)], "report": {"path": str(unavailable / f.report.name)}}
    with pytest.raises(ProcessingError, match="REPORT_GENERATION_FAILED"):
        run_multi_camera(cfg, f.detector, no_publisher=True)
    assert unavailable.read_text() == "directory obstruction"


def test_conflicting_reads_never_confirm_a_plate():
    from s1.plates import PlateVoter
    from s1.vehicle_front.plate_recognition import STATE_CODES
    voter = PlateVoter()
    first, second = sorted(STATE_CODES)[:2]
    suffix = int(key()[:8], 16) % 9000 + 1000
    for _ in range(3):
        voter.add(f"{first}01AB{suffix}", 0.95, observation_id=key())
        voter.add(f"{second}01AB{suffix}", 0.95, observation_id=key())
    assert voter.consensus() is not None
    assert not voter.is_final(3, 0.8)


def test_outbox_write_failure_can_retry_without_duplicate_rows(tmp_path, monkeypatch):
    import base64
    import secrets
    import sqlite3
    from s1.publishing.outbox import Outbox

    material = base64.b64encode(secrets.token_bytes(32)).decode()
    monkeypatch.setenv("SP_ENCRYPTION_KEYS", json.dumps({"fixture": material}))
    monkeypatch.setenv("SP_ACTIVE_ENCRYPTION_KEY", "fixture")
    received = Event()
    attempts = []
    def send(self, payload):
        attempts.append(json.loads(payload))
        received.set()
        return True
    monkeypatch.setattr(Outbox, "_post", send)
    database = tmp_path / f"{key()}.db"
    queue = Outbox(str(database), "http://127.0.0.1:1/isolated")
    event = {"event_id": key(), "event_type": "ENTRY"}
    try:
        with queue.lock, queue.db:
            queue.db.execute("""CREATE TRIGGER reject_write BEFORE INSERT ON outbox
                                BEGIN SELECT RAISE(ABORT, 'injected write refusal'); END""")
        with pytest.raises(ProcessingError) as issue:
            queue.publish(event)
        assert issue.value.diagnostic.code == "OUTBOX_PERSIST_FAILED"
        assert queue.pending() == 0
        with queue.lock, queue.db:
            queue.db.execute("DROP TRIGGER reject_write")
        queue.publish(event)
        queue.publish(event)
        assert received.wait(5)
    finally:
        assert queue.stop()
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM outbox").fetchone() == (1,)
    assert attempts == [event]


@pytest.mark.parametrize("fault", ["ocr", "slot", "timeout", "frame-size", "detector", "tracker-init"],
                         ids=["M06", "M07", "M08", "M09", "M10", "M11"])
def test_camera_specific_component_faults(fixture, monkeypatch, caplog, fault):
    from s1.multicam import multicam
    from s1.integration import vehicle_parking_bridge
    f = fixture
    write_clip(f)
    bad = key()
    bad_camera = camera(f, camera_id=bad)
    if fault == "ocr":
        bad_camera["plate"] = {"enabled": True}
        monkeypatch.setattr(multicam, "PlateReader", Mock(side_effect=RuntimeError("no OCR model for test")))
    cfg = {**f.cfg, "cameras": [bad_camera, camera(f)]}
    original = multicam.open_source
    source = Mock()
    source.eof = False
    source.dropped_frames = 0

    def open_source(config):
        if config["camera_id"] == bad and fault in {"timeout", "frame-size"}:
            if fault == "timeout":
                source.read.return_value = None
            else:
                capture = original(config)
                source.read.side_effect = [capture.read(), Frame(101, f.image[:32], 1),
                                           capture.read(), OSError("end isolated source")]
                source.stop.side_effect = capture.stop
            return source
        return original(config)

    factory = multicam.Pipeline
    def make_pipeline(config, *args, **kwargs):
        if fault == "tracker-init" and config["camera_id"] == bad:
            with monkeypatch.context() as local:
                local.setattr(vehicle_parking_bridge, "ByteTracker", Mock(side_effect=RuntimeError("tracker init broke")))
                return factory(config, *args, **kwargs)
        pipe = factory(config, *args, **kwargs)
        if fault == "slot" and config["camera_id"] == bad:
            pipe.slots.update = Mock(side_effect=RuntimeError("slot failed"))
        if fault == "detector" and config["camera_id"] == bad:
            process = pipe.process
            pipe.process = lambda frame, **kwargs: process(frame, detections=np.zeros((2, 2)))
        return pipe

    monkeypatch.setattr(multicam, "open_source", open_source)
    monkeypatch.setattr(multicam, "Pipeline", make_pipeline)
    run_multi_camera(cfg, f.detector, no_publisher=True)
    result = json.loads(f.report.read_text())
    assert result["processing_status"] == "partial"
    assert result["cameras"][f.cfg["camera_id"]]["processed_frames"] > 0
    if fault == "ocr":
        assert "ALPR_MODEL_LOAD_FAILED" in codes(caplog)
        assert result["cameras"][bad]["processed_frames"] > 0
    elif fault == "slot":
        assert "frame processing failed" in caplog.text
        assert result["cameras"][bad]["processed_frames"] == 0
    elif fault == "timeout":
        assert "CAMERA_STARTUP_TIMEOUT" in codes(caplog)
        assert bad in result["failed_camera_ids"]
        source.stop.assert_called_once()
    elif fault == "frame-size":
        assert "VIDEO_FRAME_INVALID" in codes(caplog)
        assert bad in result["failed_camera_ids"]
        source.stop.assert_called_once()
    elif fault == "detector":
        assert "DETECTION_OUTPUT_INVALID" in codes(caplog)
        assert result["cameras"][bad]["processed_frames"] == 0
    else:
        assert "CAMERA_PIPELINE_INIT_FAILED" in codes(caplog)
        assert bad in result["failed_camera_ids"]


def test_backwards_frame_clock_never_reaches_tracker(fixture, caplog):
    f = fixture
    assert f.pipe.process(Frame(101, f.image, 1)) == []
    assert f.pipe.last_frame_ok
    assert f.pipe.process(Frame(100, f.image, 2)) == []
    assert not f.pipe.last_frame_ok and f.pipe.display_ts == 101
    assert "VIDEO_TIMESTAMP_OUT_OF_ORDER" in codes(caplog)
    assert f.pipe.process(Frame(102, f.image, 3)) == []
    assert f.pipe.last_frame_ok and f.pipe.stats.frames == 2


def test_all_camera_sources_unavailable_reports_structured_terminal_failure(fixture):
    f = fixture
    cfg = {**f.cfg, "cameras": [camera(f)]}
    with pytest.raises(ProcessingError) as issue:
        run_multi_camera(cfg, f.detector, no_publisher=True)
    assert issue.value.diagnostic.code == "MULTICAMERA_NO_CAMERAS_INITIALIZED"
    summary = json.loads(f.report.read_text())
    assert summary["processing_status"] == "failed"
    assert summary["failed_camera_ids"] == [f.cfg["camera_id"]]


def test_below_tracker_threshold_does_not_seed_track(fixture):
    f = fixture
    confidence = f.cfg["tracker"]["low_thr"] / 2
    detection = np.array([[12, 12, 32, 42, confidence, 2]])
    observed, removed = f.pipe.tracker.update(detection, 100)
    assert not observed and not removed and not f.pipe.tracker.tracks
    detection[0, 4] = 0.99
    f.pipe.tracker.update(detection, 101)
    observed, _ = f.pipe.tracker.update(detection, 101.1)
    assert len(observed) == 1


def test_empty_ocr_crop_does_not_reach_reader(fixture, caplog):
    reader = Mock()
    worker = PlateWorker(reader, deterministic=True)
    uid = key()
    try:
        assert worker.submit(uid, fixture.image[:0])
        assert worker.wait_idle(timeout=3)
        assert list(worker.poll()) == [(uid, [], None)]
    finally:
        assert worker.stop()
    reader.read.assert_not_called()
    assert "PLATE_CROP_INVALID" in codes(caplog)


def test_invalid_polygon_is_rejected(fixture):
    f = fixture
    bad = {**f.geometry["slots"][0], "polygon": f.geometry["slots"][0]["polygon"][:2]}
    with pytest.raises(ValueError, match="polygon"):
        validate_geometry_for_camera(f.cfg, Geometry({**f.geometry, "slots": [bad]}))
