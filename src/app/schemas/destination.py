import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class Destination(BaseModel):
    id: uuid.UUID
    name: str


class EstimatedCost(BaseModel):
    amount: float = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)


WeatherDataType = Literal[
    "forecast",
    "historical_average",
    "seasonal_average",
    "unavailable",
]


class WeatherData(BaseModel):
    data_type: WeatherDataType
    temperature_low: float | None = None
    temperature_high: float | None = None
    temperature_unit: str | None = None
    condition: str | None = None
    precipitation_probability: float | None = Field(
        default=None,
        ge=0,
        le=100,
    )
    source: str | None = None
    period_start: date | None = None
    period_end: date | None = None


RecommendationDataStatus = Literal[
    "pending",
    "calculated",
    "partial",
    "unavailable",
    "failed",
]


class DestinationCandidate(BaseModel):
    id: uuid.UUID
    destination: Destination
    match_score: float = Field(ge=0, le=1)
    match_explanation: str
    estimated_cost_per_person: EstimatedCost
    recommendation_data_status: RecommendationDataStatus
    weather: WeatherData | None = None


class DestinationCandidateListResponse(BaseModel):
    candidates: list[DestinationCandidate]


class DestinationVoteSelection(BaseModel):
    destination_candidate_id: uuid.UUID


class VoteBallot(BaseModel):
    selections: list[DestinationVoteSelection]


class VoteBallotUpdateRequest(BaseModel):
    selections: list[DestinationVoteSelection] = Field(min_length=1, max_length=1)


class VotingStateResponse(BaseModel):
    trip_id: uuid.UUID
    status: Literal["OPEN", "CLOSED"]
    selected_candidate_id: uuid.UUID | None = None


class CloseVotingRequest(BaseModel):
    selected_candidate_id: uuid.UUID | None = None


class CandidateResponse(BaseModel):
    id: uuid.UUID
    destination_id: uuid.UUID
    name: str
    match_score: float = Field(ge=0, le=1)
    estimated_cost: EstimatedCost
    weather: dict | None = None
    reason: str


class VoteInput(BaseModel):
    candidate_id: uuid.UUID


class VoteResponse(BaseModel):
    candidate_id: uuid.UUID | None = None
    member_id: uuid.UUID


class VotingProgress(BaseModel):
    status: Literal["NOT_STARTED", "OPEN", "CLOSED"]
    eligible_count: int
    voted_count: int
    selected_destination_id: uuid.UUID | None = None
    tallies: dict[str, int]
