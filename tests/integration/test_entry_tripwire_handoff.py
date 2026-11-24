from types import SimpleNamespace

from vehicle_intelligence.pipeline import Pipeline
from vehicle_intelligence.logic import Tripwire


def make_pipeline():
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.tripwire = Tripwire(
        (0.0, 0.0), (10.0, 0.0), (0.0, 1.0), margin_px=1.0
    )
    pipeline._entry_tripwire_handoffs = {}
    return pipeline


def test_verified_handoff_restores_only_outside_side_entry_evidence():
    pipeline = make_pipeline()
    vehicle = SimpleNamespace(uid="vehicle-a")
    outside = -int(pipeline.tripwire.inside_side)
    pipeline.tripwire.state[4] = {
        "side": outside,
        "last_d": -10.0 * int(pipeline.tripwire.inside_side),
        "last_ts": 100.0,
        "sizes": [],
        "candidate": None,
    }

    pipeline._remember_entry_tripwire_state(4, vehicle, 100.2)
    pipeline.tripwire.forget(4)

    assert pipeline._restore_entry_tripwire_state(9, vehicle, 100.5)
    restored = pipeline.tripwire.state[9]
    assert restored["side"] == outside
    assert restored["last_ts"] == 100.0
    assert restored["candidate"] is None
    assert 4 not in pipeline.tripwire.state


def test_inside_side_state_is_never_transferred_as_exit_evidence():
    pipeline = make_pipeline()
    vehicle = SimpleNamespace(uid="vehicle-b")
    pipeline.tripwire.state[4] = {
        "side": int(pipeline.tripwire.inside_side),
        "last_d": 10.0 * int(pipeline.tripwire.inside_side),
        "last_ts": 100.0,
        "sizes": [],
        "candidate": None,
    }

    pipeline._remember_entry_tripwire_state(4, vehicle, 100.2)
    assert "vehicle-b" not in pipeline._entry_tripwire_handoffs


def test_stale_handoff_evidence_is_rejected():
    pipeline = make_pipeline()
    vehicle = SimpleNamespace(uid="vehicle-c")
    outside = -int(pipeline.tripwire.inside_side)
    pipeline.tripwire.state[4] = {
        "side": outside,
        "last_d": -10.0 * int(pipeline.tripwire.inside_side),
        "last_ts": 100.0,
        "sizes": [],
        "candidate": None,
    }

    pipeline._remember_entry_tripwire_state(4, vehicle, 100.2)
    assert not pipeline._restore_entry_tripwire_state(9, vehicle, 103.0)
    assert 9 not in pipeline.tripwire.state
