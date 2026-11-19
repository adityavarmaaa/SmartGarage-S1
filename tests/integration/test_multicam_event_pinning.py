import numpy as np

from s1.common import Frame
from s1.grabber import LatestFrameGrabber


def make_grabber(
    history=1.0,
    interval=0.5,
    event_max_bytes=5 * 1024 * 1024,
    total_max_bytes=12 * 1024 * 1024,
    max_events=4,
):
    return LatestFrameGrabber(
        source="unused.mp4",
        evidence_history_seconds=history,
        evidence_sample_interval_s=interval,
        evidence_max_width=160,
        evidence_jpeg_quality=60,
        evidence_event_max_count=max_events,
        evidence_event_max_bytes=event_max_bytes,
        evidence_event_total_max_bytes=total_max_bytes,
    )


def add_frame(grabber, idx, ts):
    image = np.full((240, 320, 3), idx % 255, dtype=np.uint8)
    grabber._append_evidence(Frame(ts=ts, image=image, idx=idx))


def test_event_pins_pre_and_post_frames_after_ring_eviction():
    grabber = make_grabber(history=1.0, interval=0.5)

    add_frame(grabber, 0, 0.0)
    add_frame(grabber, 1, 0.5)
    add_frame(grabber, 2, 1.0)

    started = grabber.start_evidence_event(
        "entry-1",
        trigger_ts=1.0,
        pre_event_seconds=1.0,
        post_event_seconds=1.0,
    )

    assert started["accepted"]
    assert started["pre_event_frames"] == 3

    add_frame(grabber, 3, 1.5)
    add_frame(grabber, 4, 2.0)

    # Rolling ring has evicted old frames, but event retains them.
    rolling = grabber.evidence_snapshot()
    event = grabber.get_evidence_event("entry-1")

    assert [x["idx"] for x in rolling] == [2, 3, 4]
    assert [x["idx"] for x in event["frames"]] == [0, 1, 2, 3, 4]
    assert event["status"] == "COMPLETE"
    assert event["capture_complete"] is True
    assert event["complete"] is True
    assert event["overflow"] is False


def test_event_overflow_is_explicit_and_not_marked_complete():
    grabber = make_grabber(
        history=2.0,
        interval=0.5,
        event_max_bytes=1,
        total_max_bytes=10000,
    )

    started = grabber.start_evidence_event(
        "overflow-1",
        trigger_ts=0.0,
        pre_event_seconds=0.0,
        post_event_seconds=0.5,
    )
    assert started["accepted"]

    add_frame(grabber, 1, 0.5)

    event = grabber.get_evidence_event("overflow-1")

    assert event["overflow"] is True
    assert event["overflow_reason"] == "per_event_byte_limit"
    assert event["capture_complete"] is True
    assert event["complete"] is False
    assert event["status"] == "COMPLETE_WITH_OVERFLOW"
    assert grabber.evidence_event_overflows == 1


def test_event_capacity_rejects_new_event_without_evicting_old_event():
    grabber = make_grabber(max_events=1)
    add_frame(grabber, 0, 0.0)

    first = grabber.start_evidence_event(
        "first", trigger_ts=0.0, pre_event_seconds=0.0, post_event_seconds=0.0
    )
    second = grabber.start_evidence_event(
        "second", trigger_ts=0.0, pre_event_seconds=0.0, post_event_seconds=0.0
    )

    assert first["accepted"]
    assert second["accepted"] is False
    assert second["reason"] == "event_count_limit"
    assert grabber.get_evidence_event("first") is not None
    assert grabber.get_evidence_event("second") is None
    assert grabber.evidence_events_rejected == 1


def test_release_event_frees_memory_budget():
    grabber = make_grabber()
    add_frame(grabber, 0, 0.0)

    started = grabber.start_evidence_event(
        "release-me",
        trigger_ts=0.0,
        pre_event_seconds=0.0,
        post_event_seconds=0.0,
    )
    assert started["accepted"]

    before = grabber._evidence_event_bytes
    assert before > 0

    assert grabber.release_evidence_event("release-me") is True
    assert grabber._evidence_event_bytes == 0
    assert grabber.get_evidence_event("release-me") is None


def test_stopping_grabber_marks_unfinished_event_interrupted():
    grabber = make_grabber()
    add_frame(grabber, 0, 0.0)

    started = grabber.start_evidence_event(
        "unfinished",
        trigger_ts=0.0,
        pre_event_seconds=0.0,
        post_event_seconds=5.0,
    )
    assert started["accepted"]

    grabber.stop()
    event = grabber.get_evidence_event("unfinished")

    assert event["status"] == "INTERRUPTED"
    assert event["complete"] is False
    assert event["capture_complete"] is False
    assert event["interrupted_reason"] == "capture_stopped"
