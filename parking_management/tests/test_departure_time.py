import pytest
from pydantic import ValidationError

from parking_management.app.schemas.vehicle_event import VehicleEvent


@pytest.mark.parametrize("departure", ["invalid", "2026-01-01T10:10:00", "2026-01-01T04:29:59Z", 15, "15"])
def test_rejects_invalid_departure(departure):
    with pytest.raises(ValidationError):
        VehicleEvent(event_type="EXIT", camera_id="camera", parking_area_id="area",
                     timestamp="2026-01-01T10:00:00+05:30", captured_at="2026-01-01T10:00:00+05:30",
                     departure_at=departure, vehicle={})


def test_accepts_explicit_departure_across_timezone_offsets():
    event = VehicleEvent(event_type="EXIT", camera_id="camera", parking_area_id="area",
                         timestamp="2026-01-01T10:00:00+05:30", captured_at="2026-01-01T10:00:00+05:30",
                         departure_at="2026-01-01T04:31:00Z", vehicle={})
    assert (event.departure_at - event.captured_at).total_seconds() == 60
