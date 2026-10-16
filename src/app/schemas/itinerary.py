from typing import Any

from pydantic import BaseModel, Field


class Budget(BaseModel):
    max_amount: float = Field(gt=0)
    currency: str = "USD"


class Itinerary(BaseModel):
    id: str
    trip_id: str
    destination_id: str
    version: int = Field(ge=1)
    status: str
    days: list[dict[str, Any]]
    estimated_total_cost: Budget


class RevisionInput(BaseModel):
    instruction: str


class Revision(BaseModel):
    id: str
    instruction: str
    status: str
    base_version: int


class ApprovalInput(BaseModel):
    version: int = Field(ge=1)
    approved: bool


class Approval(BaseModel):
    member_id: str
    version: int
    approved: bool


class ApprovalProgress(BaseModel):
    version: int
    approved_count: int
    eligible_count: int
    finalized: bool


class EligibleMember(BaseModel):
    member_id: str
    display_name: str
    voting_eligible: bool = True


class TripContext(BaseModel):
    trip_id: str
    selected_destination_id: str
    destination_name: str
    start_date: str
    end_date: str
    budget: Budget
    eligible_members: list[EligibleMember]
    interests: list[str] = Field(default_factory=list)


class ItineraryItemInput(BaseModel):
    day_number: int = Field(ge=1)
    position: int = Field(ge=1)
    title: str = Field(min_length=1)
    estimated_cost: float = Field(ge=0)


class ItineraryDraft(BaseModel):
    items: list[ItineraryItemInput] = Field(min_length=1)
    estimated_total_cost: float = Field(ge=0)
