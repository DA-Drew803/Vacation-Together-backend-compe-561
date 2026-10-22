from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

TransportationPreference = Literal["AUTO", "FLIGHT", "ROAD_TRIP"]


class TripCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class TripUpdate(BaseModel):
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
    )

    state: str | None = None


class TripDatesUpdate(BaseModel):
    selected_start_date: date
    selected_end_date: date

    @model_validator(mode="after")
    def validate_date_order(self) -> "TripDatesUpdate":
        if self.selected_start_date > self.selected_end_date:
            raise ValueError("selected_start_date must be on or before selected_end_date")
        return self


class TripTravelDetailsUpdate(BaseModel):
    departure_location: str | None = Field(default=None, min_length=1, max_length=200)
    transportation_preference: TransportationPreference = "AUTO"


class TripResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    state: str
    selected_destination_id: UUID | None
    voting_started_at: datetime | None
    voting_closed_at: datetime | None
    selected_start_date: date | None
    selected_end_date: date | None
    departure_location: str | None
    transportation_preference: TransportationPreference
