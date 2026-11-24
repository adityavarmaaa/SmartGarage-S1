import uuid

import pytest

from vehicle_intelligence.s2_mapper import S2Mapper


class RecordingPublisher:
    def __init__(self, fail=False):
        self.events = []
        self.fail = fail

    def publish(self, event):
        if self.fail:
            raise RuntimeError("Simulated test publisher failure")
        self.events.append(dict(event))


def make_event(event_type, uid, camera, area, slot):
    return {
        "event_type": event_type,
        "event_id": uuid.uuid4().hex,
        "vehicle_uid": uid,
        "camera_id": camera,
        "parking_area_id": area,
        "parking": {"slot_id": slot},
    }


def make_parked_mapper(tmp_path, publisher):
    mapper = S2Mapper(
        publisher,
        db_path=str(tmp_path / "mapper_test.db"),
    )

    uid = uuid.uuid4().hex
    camera = "TEST-" + uuid.uuid4().hex
    area = "TEST-" + uuid.uuid4().hex
    slot = "TEST-" + uuid.uuid4().hex

    first_seen = "2026-01-01T10:00:00+05:30"
    parked_at = "2026-01-01T10:05:00+05:30"

    event = make_event("ENTRY", uid, camera, area, slot)

    mapper._upsert_uid_session(
        event,
        first_seen,
        parked_at=parked_at,
        status="PARKED",
        slot_id=slot,
    )

    return mapper, event, first_seen, parked_at


def test_uid_only_exit_publishes_and_closes_session(tmp_path):
    publisher = RecordingPublisher()
    mapper, event, first_seen, parked_at = make_parked_mapper(
        tmp_path, publisher
    )

    try:
        exit_event = dict(event)
        exit_event.update({
            "event_type": "PARK_END",
            "event_id": uuid.uuid4().hex,
            "event_time": "2026-01-01T10:15:00+05:30",
            "timestamp": "2026-01-01T10:15:00+05:30",
        })

        mapper.publish(exit_event)

        assert len(publisher.events) == 1
        published = publisher.events[0]

        assert published["event_type"] == "EXIT"
        assert published["vehicle_uid"] == event["vehicle_uid"]
        assert published["timestamp"] == first_seen
        assert published["captured_at"] == parked_at
        assert published["received_at"] == exit_event["event_time"]
        assert published["departure_at"] == exit_event["event_time"]

        session = mapper._uid_session(exit_event)
        assert session is not None
        assert session[3] == "CLOSED"
    finally:
        mapper._db.close()


def test_exit_without_matching_session_is_not_published(tmp_path):
    publisher = RecordingPublisher()
    mapper = S2Mapper(
        publisher,
        db_path=str(tmp_path / "missing_session.db"),
    )

    try:
        event = make_event(
            "PARK_END",
            uuid.uuid4().hex,
            "TEST-" + uuid.uuid4().hex,
            "TEST-" + uuid.uuid4().hex,
            "TEST-" + uuid.uuid4().hex,
        )
        event["event_time"] = "2026-01-01T10:15:00+05:30"

        mapper.publish(event)

        assert publisher.events == []
    finally:
        mapper._db.close()


def test_publisher_failure_does_not_close_uid_session(tmp_path):
    publisher = RecordingPublisher(fail=True)
    mapper, event, _, _ = make_parked_mapper(tmp_path, publisher)

    try:
        exit_event = dict(event)
        exit_event.update({
            "event_type": "PARK_END",
            "event_id": uuid.uuid4().hex,
            "event_time": "2026-01-01T10:15:00+05:30",
        })

        with pytest.raises(RuntimeError):
            mapper.publish(exit_event)

        session = mapper._uid_session(exit_event)
        assert session is not None
        assert session[3] == "PARKED"
    finally:
        mapper._db.close()
