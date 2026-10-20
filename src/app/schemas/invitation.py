from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class InvitationCreate(BaseModel):
    invitee_email: str = Field(
        min_length=5,
        max_length=320,
    )


class InvitationUpdate(BaseModel):
    status: str


class InvitationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    trip_id: UUID
    inviter_id: UUID
    invitee_email: str
    token: str
    status: str
    created_at: datetime
    expires_at: datetime