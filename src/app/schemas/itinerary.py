from datetime import date
from math import isfinite
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class Budget(BaseModel):
    """The limiting per-person budget supplied by deterministic group matching."""

    max_amount: float = Field(gt=0)
    currency: str = "USD"


ItineraryStatus = Literal["GENERATING", "DRAFT", "REVISING", "FINALIZED", "FAILED"]
RevisionStatus = Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "NO_CHANGE"]


class Error(BaseModel):
    code: str
    message: str


class Itinerary(BaseModel):
    id: str
    trip_id: str
    destination_id: str
    version: int = Field(ge=1)
    status: ItineraryStatus
    days: list[dict[str, Any]]
    estimated_total_cost: Budget


class RevisionInput(BaseModel):
    instruction: str


class Revision(BaseModel):
    id: UUID
    status: RevisionStatus
    requested_version: int


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


class WeatherDay(BaseModel):
    date: date
    condition: str
    temperature_high: float | None = None
    temperature_low: float | None = None
    precipitation_probability: int | None = Field(default=None, ge=0, le=100)

    @field_validator("condition")
    @classmethod
    def validate_condition(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Weather condition must not be blank")
        return normalized

    @field_validator("temperature_high", "temperature_low")
    @classmethod
    def validate_temperature(cls, value: float | None) -> float | None:
        if value is not None and not isfinite(value):
            raise ValueError("Weather temperatures must be finite")
        return value

    @model_validator(mode="after")
    def validate_temperature_range(self) -> "WeatherDay":
        if (
            self.temperature_low is not None
            and self.temperature_high is not None
            and self.temperature_low > self.temperature_high
        ):
            raise ValueError("Weather low temperature must not exceed high temperature")
        return self


class FoodPreferences(BaseModel):
    cuisines: list[str] = Field(default_factory=list)
    dining_style: str | None = None
    dietary_preferences: list[str] = Field(default_factory=list)


class ItineraryItemMetadata(BaseModel):
    """Provider-side semantic metadata; intentionally not persisted by the DB models."""

    category: str
    tags: list[str] = Field(default_factory=list)
    environment: str = "UNKNOWN"
    mobility: str = "UNKNOWN"
    intensity: str = "UNKNOWN"


class TransportationPlan(BaseModel):
    mode: Literal["FLIGHT", "ROAD_TRIP"]
    estimated_cost_per_person: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    reason: str | None = Field(default=None, max_length=500)


class TripContext(BaseModel):
    trip_id: str
    selected_destination_id: str
    destination_name: str
    start_date: str
    end_date: str
    budget: Budget
    eligible_members: list[EligibleMember]
    interests: list[str] = Field(default_factory=list)
    food_preferences: FoodPreferences | None = None
    travel_preferences: list[str] = Field(default_factory=list)
    pace: str | None = None
    mobility_preference: str | None = None
    weather: list[WeatherDay] | None = None
    transportation: TransportationPlan | None = None

    @model_validator(mode="after")
    def reject_duplicate_weather_dates(self) -> "TripContext":
        if self.weather is not None:
            dates = [weather_day.date for weather_day in self.weather]
            if len(dates) != len(set(dates)):
                raise ValueError("Weather dates must be unique")
        return self


class ItineraryItemInput(BaseModel):
    day_number: int = Field(ge=1)
    position: int = Field(ge=1)
    title: str = Field(min_length=1)
    estimated_cost: float = Field(ge=0)
    metadata: ItineraryItemMetadata | None = None


class ItineraryDraft(BaseModel):
    items: list[ItineraryItemInput] = Field(min_length=1)
    estimated_total_cost: float = Field(ge=0)
