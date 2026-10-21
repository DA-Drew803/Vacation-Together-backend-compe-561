from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class TripMemberCreate(BaseModel):
    user_id: UUID
    role: str = "MEMBER"


class TripMemberUpdate(BaseModel):
    role: str


class TripMemberResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    trip_id: UUID
    user_id: UUID
    role: str
    status: str
    voting_eligible: bool
    joined_at: datetime
