from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class Budget(BaseModel):
    max_amount: float = Field(gt=0)
    currency: str = "USD"


ItineraryStatus = Literal["GENERATING", "DRAFT", "REVISING", "FINALIZED", "FAILED"]
RevisionStatus = Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "NO_CHANGE"]


class Error(BaseModel):
    code: str
    message: str


class Itinerary(BaseModel):
    id: UUID
    trip_id: UUID
    destination_id: UUID
    version: int = Field(ge=1)
    status: ItineraryStatus
    days: list[dict[str, Any]]
    estimated_total_cost: Budget


class RevisionInput(BaseModel):
    instruction: str


class Revision(BaseModel):
    id: UUID
    instruction: str
    status: RevisionStatus
    base_version: int


class ApprovalInput(BaseModel):
    version: int = Field(ge=1)
    approved: bool


class Approval(BaseModel):
    member_id: UUID
    version: int
    approved: bool


class ApprovalProgress(BaseModel):
    version: int
    approved_count: int
    eligible_count: int
    finalized: bool


class EligibleMember(BaseModel):
    member_id: UUID
    display_name: str
    voting_eligible: bool = True


class TripContext(BaseModel):
    trip_id: UUID
    selected_destination_id: UUID
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
