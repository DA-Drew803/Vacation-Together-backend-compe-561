from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

MemberRole = Literal["OWNER", "MEMBER"]
MemberStatus = Literal["ACTIVE", "REMOVED"]


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


class DateRange(BaseModel):
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_date_order(self) -> "DateRange":
        if self.start_date > self.end_date:
            raise ValueError("start_date must be on or before the end date")
        return self


class AvailabilityUpdate(BaseModel):
    ranges: list[DateRange] = Field(min_length=1)


class AvailabilityRead(BaseModel):
    member_id: UUID
    ranges: list[DateRange]


class BudgetUpdate(BaseModel):
    max_amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: str = Field(min_length=3, max_length=3)


class BudgetRead(BaseModel):
    member_id: UUID
    max_amount: Decimal
    currency: str


class MemberRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    display_name: str
    role: MemberRole
    status: MemberStatus
    voting_eligible: bool


class PreferenceUpdate(BaseModel):
    interests: list[str] = Field(default_factory=list, max_length=50)
    private_notes: str | None = Field(default=None, max_length=2000)


class PreferenceRead(BaseModel):
    member_id: UUID
    interests: list[str]
    private_notes: str | None
