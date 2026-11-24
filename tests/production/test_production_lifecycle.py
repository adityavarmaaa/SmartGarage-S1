"""Synthetic lifecycle contract tests. No publishing destination is configured."""
import base64
from datetime import datetime, timedelta, timezone
import json
import secrets
from unittest.mock import Mock
from uuid import uuid4

import pytest

from vehicle_intelligence.parking.parking_lifecycle import build_timeline
from vehicle_intelligence.publishing.s2_mapper import S2Mapper


@pytest.fixture
def session(tmp_path, monkeypatch):
    key = base64.b64encode(secrets.token_bytes(32)).decode()
    monkeypatch.setenv("SP_ENCRYPTION_KEYS", json.dumps({"fixture": key}))
    monkeypatch.setenv("SP_ACTIVE_ENCRYPTION_KEY", "fixture")
    monkeypatch.setenv("SP_HMAC_KEY", key)
    identities = {attr: uuid4().hex for attr in ("vehicle_uid", "camera_id", "parking_area_id")}
    slot = uuid4().hex
    def event(kind, minute, **overrides):
        stamp = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=minute)).isoformat()
        return {"event_type": kind, "event_id": uuid4().hex, **identities, "parking": {"slot_id": slot},
                "bay": {"id": slot}, "event_time": stamp, "timestamp": stamp, **overrides}
    return tmp_path / f"{uuid4().hex}.sqlite", event


def test_semantic_park_start_retry_after_restart(session):
    path, event = session
    sender = Mock()
    entry, start = event("ENTRY", 0), event("PARK_START", 1)
    mapper = S2Mapper(sender, str(path))
    try:
        mapper.publish(entry)
        mapper.publish(start)
    finally:
        mapper._db.close()
    mapper = S2Mapper(sender, str(path))
    try:
        mapper.publish({**start, "event_id": uuid4().hex})
    finally:
        mapper._db.close()
    assert sender.publish.call_count == 1
    assert sender.publish.call_args.args[0]["event_id"] == start["event_id"]


def test_retry_uses_first_id_after_outbox_failure(session):
    path, event = session
    sender = Mock()
    sender.publish.side_effect = [OSError("outbox unavailable"), None]
    start = event("PARK_START", 1)
    mapper = S2Mapper(sender, str(path))
    try:
        with pytest.raises(OSError):
            mapper.publish(start)
        mapper.publish({**start, "event_id": uuid4().hex})
    finally:
        mapper._db.close()
    assert [c.args[0]["event_id"] for c in sender.publish.call_args_list] == [start["event_id"]] * 2


def test_delayed_duplicate_entry_cannot_reopen_parked_session(session):
    path, event = session
    sender = Mock()
    mapper = S2Mapper(sender, str(path))
    try:
        entry, start, end = event("ENTRY", 0), event("PARK_START", 1), event("PARK_END", 2)
        mapper.publish(entry)
        mapper.publish(start)
        mapper.publish({**entry, "event_id": uuid4().hex})
        assert mapper._uid_session(start)[3] == "PARKED"
        mapper.publish(end)
        mapper.publish({**end, "event_id": uuid4().hex})
        mapper.publish(event("EXIT", 3))
        assert [call.args[0]["event_type"] for call in sender.publish.call_args_list] == ["ENTRY", "EXIT"]
    finally:
        mapper._db.close()


def test_exit_cannot_close_another_slot_or_vehicle_on_conflicting_plate(session):
    path, event = session
    sender = Mock()
    mapper = S2Mapper(sender, str(path))
    first_plate, another_plate = [uuid4().hex.upper() for _ in range(2)]
    other_uid, other_slot = uuid4().hex, uuid4().hex
    first = event("PARK_START", 1, plate_status="CONFIRMED", vehicle={"plate": first_plate})
    other = event("PARK_START", 2, plate_status="CONFIRMED", vehicle={"plate": another_plate},
                  vehicle_uid=other_uid, parking={"slot_id": other_slot})
    try:
        mapper.publish(first)
        mapper.publish(other)
        mapper.publish(event("PARK_END", 3, plate_status="CONFIRMED", vehicle={"plate": another_plate}))
        assert mapper._uid_session(first)[3] == "PARKED"
        assert mapper._uid_session(other)[3] == "PARKED"
        assert sender.publish.call_count == 2
    finally:
        mapper._db.close()


@pytest.mark.parametrize("kind", ["ENTRY", "PARK_START", "PARK_END", "EXIT"])
def test_report_ignores_same_observation_with_fresh_id(session, kind):
    _, event = session
    events = [event(stage, i) for i, stage in enumerate(("ENTRY", "PARK_START", "PARK_END", "EXIT"))]
    duplicate = {**next(x for x in events if x["event_type"] == kind), "event_id": uuid4().hex}
    rows = build_timeline([*reversed(events), duplicate])
    assert len(rows) == 1
    assert rows[0]["status"] == "EXITED" and rows[0]["parked_duration_s"] == 60


def test_unconfirmed_ocr_never_reaches_downstream_payload(session):
    path, event = session
    sender = Mock()
    mapper = S2Mapper(sender, str(path))
    candidate = uuid4().hex
    try:
        mapper.publish(event("PARK_START", 1, plate_status="CANDIDATE",
                             vehicle={"plate": candidate}, plate={"text": candidate},
                             plate_candidate=candidate))
        published = sender.publish.call_args.args[0]
        assert candidate not in json.dumps(published)
        assert published["vehicle"]["plate"] is None
        assert published["plate"]["text"] is None
        assert published["plate_status"] == "CANDIDATE"
    finally:
        mapper._db.close()


def test_concurrent_start_retries_queue_once(session):
    from concurrent.futures import ThreadPoolExecutor
    path, event = session
    sender = Mock()
    mapper = S2Mapper(sender, str(path))
    start = event("PARK_START", 1)
    try:
        reruns = [{**start, "event_id": uuid4().hex} for _ in range(8)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(mapper.publish, reruns))
        assert sender.publish.call_count == 1
    finally:
        mapper._db.close()


def test_trace_preserves_original_time_slot_and_uid(session):
    import os
    from pathlib import Path
    path, event = session
    sender = Mock()
    mapper = S2Mapper(sender, str(path))
    stages = ("ENTRY", "PARK_START", "PARK_END", "EXIT")
    source = [event(stage, i, plate_status="UNCONFIRMED") for i, stage in enumerate(stages)]
    try:
        for item in source:
            mapper.publish(item)
        outgoing = [c.args[0] for c in sender.publish.call_args_list]
        timeline = build_timeline(mapper.raw_events)
        assert [item["event_type"] for item in outgoing] == ["ENTRY", "EXIT"]
        assert all(item["vehicle_uid"] == source[0]["vehicle_uid"] for item in outgoing)
        assert all(item["parking"] == source[0]["parking"] for item in outgoing)
        assert all(item["timestamp"] == source[0]["event_time"] for item in outgoing)
        assert outgoing[1]["received_at"] == source[2]["event_time"]
        assert timeline[0]["plate_status"] == "UNCONFIRMED"
        destination = os.getenv("S1_HANDOVER_TRACE_PATH")
        if destination:
            Path(destination).write_text(json.dumps({
                "source": source, "mapped": outgoing, "timeline": timeline,
                "test": "test_trace_preserves_original_time_slot_and_uid",
            }, indent=2) + "\n")
    finally:
        mapper._db.close()


def test_stale_end_cannot_close_a_newer_parking_cycle(session):
    path, event = session
    sender = Mock()
    mapper = S2Mapper(sender, str(path))
    try:
        old_end = event("PARK_END", 2)
        for value in (event("ENTRY", 0), event("PARK_START", 1), old_end,
                      event("ENTRY", 5), event("PARK_START", 6)):
            mapper.publish(value)
        mapper.publish({**old_end, "event_id": uuid4().hex})
        assert mapper._uid_session(old_end)[3] == "PARKED"
        assert sender.publish.call_count == 3
    finally:
        mapper._db.close()
