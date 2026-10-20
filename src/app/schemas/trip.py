from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TripCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class TripUpdate(BaseModel):
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
    )

    state: str | None = None

    selected_destination_id: UUID | None = None


class TripResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    state: str
    selected_destination_id: UUID | None
    voting_started_at: datetime | None
    voting_closed_at: datetime | None