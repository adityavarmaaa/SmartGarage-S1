
from datetime import datetime
from typing import Optional

from pydantic import AwareDatetime, BaseModel, Field, field_validator, model_validator


class VehicleData(BaseModel):
    type: Optional[str] = None
    type_confidence: Optional[float] = None
    color: Optional[str] = None
    color_confidence: Optional[float] = None
    plate: Optional[str] = None
    plate_confidence: Optional[float] = None


class ParkingData(BaseModel):
    slot_id: Optional[str] = None
    slot_confidence: Optional[float] = None


class SnapshotData(BaseModel):
    vehicle: Optional[str] = None
    plate: Optional[str] = None


class VehicleEvent(BaseModel):
    event_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    event_type: str
    camera_id: str
    parking_area_id: str
    vehicle_uid: Optional[str] = None
    track_id: Optional[int] = None
    timestamp: datetime
    captured_at: Optional[datetime] = None
    received_at: Optional[datetime] = None
    departure_at: Optional[AwareDatetime] = None

    vehicle: VehicleData
    parking: Optional[ParkingData] = None
    snapshot: Optional[SnapshotData] = None


    @field_validator("departure_at", mode="before")
    @classmethod
    def require_departure_datetime(cls, value):
        if value is None or isinstance(value, datetime):
            return value
        if isinstance(value, str):
            return datetime.fromisoformat(value)
        raise ValueError("departure_at must be an ISO-8601 datetime with a timezone offset")

    @model_validator(mode="after")
    def validate_departure(self):
        if self.departure_at is not None:
            if self.event_type != "EXIT":
                raise ValueError("departure_at is only valid for EXIT")
            if self.captured_at is not None and (
                self.captured_at.utcoffset() is None or self.departure_at < self.captured_at
            ):
                raise ValueError("departure_at must be at or after timezone-aware captured_at")
        return self
