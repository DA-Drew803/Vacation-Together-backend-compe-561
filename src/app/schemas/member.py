from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

MemberRole = Literal["OWNER", "MEMBER"]
MemberStatus = Literal["active", "removed"]


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
    joined_at: datetime


class MemberRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user: "MemberUserRead"
    role: MemberRole
    status: MemberStatus
    joined_at: datetime


class MemberUserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    display_name: str
    created_at: datetime


class TripMemberListResponse(BaseModel):
    items: list[MemberRead]


class PreferenceUpdate(BaseModel):
    interests: list[str] = Field(max_length=50)
    free_text: str | None = Field(default=None, max_length=4000)


class PreferenceRead(BaseModel):
    trip_member_id: UUID
    interests: list[str]
    free_text: str | None
    updated_at: datetime
