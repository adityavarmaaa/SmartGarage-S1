
from types import SimpleNamespace

from vehicle_intelligence.pipeline import Pipeline


def make_pipeline(callback, saved, ready=False):
    p = Pipeline.__new__(Pipeline)
    p.cfg = {"plate": {"emit_deadline_s": 3.0}}
    p.cam = "CAM-TEST"
    p.now = lambda: 100.0
    p.slots = SimpleNamespace(slot_info=lambda sid: {"id": sid})
    p.pending_parking_events = []
    p.evidence_event_callback = callback
    p._save_pending_event = lambda pending: saved.append(pending.copy())
    p._plate_ready = lambda vehicle: ready
    p._build_and_append = lambda out, pending: out.append(pending)
    return p


def test_lifecycle_event_starts_evidence_after_pending_save():
    order = []
    saved = []

    def callback(event_id, event_time):
        order.append(("callback", event_id, event_time))
        assert len(saved) == 1
        return {"accepted": True}

    p = make_pipeline(callback, saved)
    p._emit([], "ENTRY", SimpleNamespace(uid="vehicle-1"), 95.0)

    assert len(saved) == 1
    assert order[0][0] == "callback"
    assert order[0][1] == saved[0]["event_id"]
    assert order[0][2] == 95.0
    assert len(p.pending_parking_events) == 1


def test_non_lifecycle_event_does_not_start_evidence():
    calls = []
    saved = []

    p = make_pipeline(
        lambda *args: calls.append(args),
        saved,
    )
    output = []
    p._emit(output, "VEHICLE_UPDATE", SimpleNamespace(uid="vehicle-2"), 96.0)

    assert calls == []
    assert len(output) == 1
    assert saved == []


def test_evidence_starts_before_ocr_delayed_publication():
    calls = []
    saved = []

    p = make_pipeline(
        lambda event_id, event_time: (
            calls.append((event_id, event_time)) or {"accepted": True}
        ),
        saved,
        ready=False,
    )

    p._emit([], "PARK_START", SimpleNamespace(uid="vehicle-3"), 97.0)

    assert len(calls) == 1
    assert calls[0][1] == 97.0
    assert len(p.pending_parking_events) == 1
    # Event remains pending for OCR, but evidence capture has already started.


def test_evidence_callback_failure_does_not_break_lifecycle_event():
    saved = []

    def failing_callback(*args):
        raise RuntimeError("simulated evidence failure")

    p = make_pipeline(failing_callback, saved)
    p._emit([], "EXIT", SimpleNamespace(uid="vehicle-4"), 98.0)

    assert len(saved) == 1
    assert len(p.pending_parking_events) == 1
