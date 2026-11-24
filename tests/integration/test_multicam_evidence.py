import cv2
import numpy as np

from vehicle_intelligence.common import Frame
from vehicle_intelligence.grabber import LatestFrameGrabber


def make_grabber(history=2.0, interval=0.5, capacity=1):
    return LatestFrameGrabber(
        source="unused.mp4",
        buffer_capacity=capacity,
        evidence_history_seconds=history,
        evidence_sample_interval_s=interval,
        evidence_max_width=160,
        evidence_jpeg_quality=60,
    )


def make_frame(idx, ts):
    image = np.full((240, 320, 3), idx % 255, dtype=np.uint8)
    return Frame(ts=ts, image=image, idx=idx)


def test_evidence_history_retains_recent_window_in_order():
    grabber = make_grabber(history=1.5, interval=0.5)
    for idx in range(6):
        grabber._append_evidence(make_frame(idx, idx * 0.5))

    records = grabber.evidence_snapshot()
    assert [x["idx"] for x in records] == [2, 3, 4, 5]
    assert [x["ts"] for x in records] == [1.0, 1.5, 2.0, 2.5]


def test_evidence_sampling_interval_is_respected():
    grabber = make_grabber(history=2.0, interval=0.5)
    for idx, ts in enumerate([0.0, 0.1, 0.2, 0.5, 0.7, 1.0]):
        grabber._append_evidence(make_frame(idx, ts))

    assert [x["ts"] for x in grabber.evidence_snapshot()] == [0.0, 0.5, 1.0]


def test_evidence_history_is_independent_of_normal_frame_queue():
    grabber = make_grabber(history=2.0, interval=0.5, capacity=1)

    for idx in range(4):
        frame = make_frame(idx, idx * 0.5)
        grabber._append_evidence(frame)

        if len(grabber._frames) >= grabber.buffer_capacity:
            grabber._frames.popleft()
            grabber.dropped_frames += 1
        grabber._frames.append(frame)

    assert len(grabber._frames) == 1
    assert [x["idx"] for x in grabber.evidence_snapshot()] == [0, 1, 2, 3]


def test_evidence_jpeg_decodes_and_preserves_metadata():
    grabber = make_grabber(history=2.0, interval=0.5)
    grabber._append_evidence(make_frame(7, 3.5))

    record = grabber.evidence_snapshot()[0]
    decoded = cv2.imdecode(
        np.frombuffer(record["jpeg"], dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )

    assert record["idx"] == 7
    assert record["ts"] == 3.5
    assert record["width"] == 320
    assert record["height"] == 240
    assert decoded is not None
    assert decoded.shape[1] == 160


def test_evidence_snapshot_timestamp_filter():
    grabber = make_grabber(history=5.0, interval=0.5)
    for idx in range(5):
        grabber._append_evidence(make_frame(idx, idx * 0.5))

    records = grabber.evidence_snapshot(start_ts=0.5, end_ts=1.5)
    assert [x["idx"] for x in records] == [1, 2, 3]
