from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Literal, Protocol
from weakref import WeakValueDictionary

from openai import APITimeoutError, OpenAI, OpenAIError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.schemas.itinerary import (
    ItineraryDraft,
    ItineraryItemInput,
    ItineraryItemMetadata,
    TripContext,
)

DEFAULT_OPENAI_MODEL = "gpt-6-luna"
PER_PERSON_COST_BASIS = "PER_PERSON"
DEFAULT_TIMEOUT_SECONDS = 30.0
OPENAI_MAX_RETRIES = 0
MIN_TIMEOUT_SECONDS = 1.0
MAX_TIMEOUT_SECONDS = 60.0
MAX_INTERESTS = 20
MAX_INTEREST_LENGTH = 80
MAX_DESTINATION_LENGTH = 120
MAX_ELIGIBLE_MEMBERS = 100
MAX_TRAVEL_DAYS = 31
MAX_ITEMS_PER_DAY = 12
MAX_TITLE_LENGTH = 160
MAX_REVISION_INSTRUCTION_LENGTH = 500
MAX_DATABASE_AMOUNT_CENTS = 999_999_999_999
VALID_ENVIRONMENTS = {"INDOOR", "OUTDOOR", "MIXED", "UNKNOWN"}
VALID_MOBILITY = {"LOW", "MEDIUM", "HIGH", "UNKNOWN"}
VALID_INTENSITY = {"LOW", "MEDIUM", "HIGH", "UNKNOWN"}
ORDINAL_DAY_NUMBERS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "eleventh": 11,
    "twelfth": 12,
    "thirteenth": 13,
    "fourteenth": 14,
    "fifteenth": 15,
    "sixteenth": 16,
    "seventeenth": 17,
    "eighteenth": 18,
    "nineteenth": 19,
    "twentieth": 20,
    "twenty-first": 21,
    "twenty-second": 22,
    "twenty-third": 23,
    "twenty-fourth": 24,
    "twenty-fifth": 25,
    "twenty-sixth": 26,
    "twenty-seventh": 27,
    "twenty-eighth": 28,
    "twenty-ninth": 29,
    "thirtieth": 30,
    "thirty-first": 31,
}


class ItineraryProviderError(RuntimeError):
    """Base error for safe itinerary-provider failures."""


class ItineraryProviderConfigurationError(ItineraryProviderError):
    """Raised when required provider configuration is missing or invalid."""


class ItineraryProviderInputError(ItineraryProviderError):
    """Raised when normalized trip context cannot produce a safe itinerary."""


class ItineraryProviderResponseError(ItineraryProviderError):
    """Raised when provider output fails application-level validation."""


class ItineraryProviderTimeoutError(ItineraryProviderError):
    """Raised when the provider request exceeds the configured timeout."""


class ItineraryRevisionUnsupportedError(ItineraryProviderError):
    """Raised when a requested revision cannot be applied safely."""


def create_trust_boundary() -> tuple[type[ItineraryDraft], Any, Any]:
    """Create a draft type whose provenance capability stays inside this module."""
    issuance_capability = object()
    issued_drafts: WeakValueDictionary[int, ItineraryDraft] = WeakValueDictionary()

    class RichItineraryDraft(ItineraryDraft):
        """Provider-owned draft with validated semantic metadata retained in memory."""

        def __init__(self, **data: Any) -> None:
            if data.pop("_issuance_capability", None) is not issuance_capability:
                raise TypeError("Trusted itinerary drafts must be issued by the provider")
            super().__init__(**data)

    def issue(**data: Any) -> ItineraryDraft:
        draft = RichItineraryDraft(
            _issuance_capability=issuance_capability,
            **data,
        )
        issued_drafts[id(draft)] = draft
        return draft

    def is_issued(draft: ItineraryDraft) -> bool:
        return issued_drafts.get(id(draft)) is draft

    return RichItineraryDraft, issue, is_issued


RichItineraryDraft, issue_trusted_draft, is_issued_trusted_draft = create_trust_boundary()


class ItineraryDraftProvider(Protocol):
    def generate(self, context: TripContext) -> ItineraryDraft: ...


class ItineraryRevisionProvider(Protocol):
    def revise(
        self,
        context: TripContext,
        current: ItineraryDraft,
        current_version: int,
        instruction: str,
    ) -> ItineraryDraft: ...


class CostCategory(StrEnum):
    FLIGHTS = "flights"
    LODGING = "lodging"
    FOOD = "food"
    LOCAL_TRANSPORT = "local_transport"
    ACTIVITIES = "activities"


class RevisionIntentKind(StrEnum):
    CHEAPER_DAY = "cheaper_day"
    COST_ONLY = "cost_only"
    SEMANTIC = "semantic"
    REPLACE_ACTIVITY = "replace_activity"


@dataclass(frozen=True)
class RevisionIntent:
    kind: RevisionIntentKind
    target_day: int | None = None
    target_text: str | None = None
    replacement_text: str | None = None


class RevisionTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day_number: int = Field(ge=1, le=MAX_TRAVEL_DAYS)
    position: int = Field(ge=1, le=MAX_ITEMS_PER_DAY)
    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)
    requested_activity: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)


class GeneratedItineraryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day_number: int = Field(ge=1, le=MAX_TRAVEL_DAYS)
    position: int = Field(ge=1, le=MAX_ITEMS_PER_DAY)
    category: CostCategory
    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)
    estimated_cost_cents: int = Field(ge=0, le=MAX_DATABASE_AMOUNT_CENTS)
    tags: list[str] = Field(default_factory=list, max_length=12)
    environment: str = "UNKNOWN"
    mobility: str = "UNKNOWN"
    intensity: str = "UNKNOWN"


class GeneratedItineraryDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cost_basis: Literal["PER_PERSON"]
    currency: str = Field(min_length=3, max_length=3)
    items: list[GeneratedItineraryItem] = Field(min_length=5)
    estimated_total_cost_cents: int = Field(ge=0, le=MAX_DATABASE_AMOUNT_CENTS)
    destination_name: str = Field(min_length=1, max_length=MAX_DESTINATION_LENGTH)
    start_date: date
    end_date: date


class RevisionItineraryItem(BaseModel):
    """Provider view of an item, with optional trusted in-memory metadata."""

    model_config = ConfigDict(extra="forbid")

    day_number: int = Field(ge=1, le=MAX_TRAVEL_DAYS)
    position: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)
    estimated_cost_cents: int = Field(ge=0, le=MAX_DATABASE_AMOUNT_CENTS)
    category: str | None = None
    tags: list[str] = Field(default_factory=list, max_length=12)
    environment: str = "UNKNOWN"
    mobility: str = "UNKNOWN"
    intensity: str = "UNKNOWN"


class RevisionCurrentDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cost_basis: Literal["PER_PERSON"]
    currency: str = Field(min_length=3, max_length=3)
    items: list[RevisionItineraryItem] = Field(min_length=1)
    estimated_total_cost_cents: int = Field(ge=0, le=MAX_DATABASE_AMOUNT_CENTS)


class GeneratedRevisionDraft(RevisionCurrentDraft):
    outcome: Literal["APPLIED", "NO_CHANGE", "UNSUPPORTED"]
    outcome_reason: str = Field(min_length=1, max_length=240)
    operation: Literal["COST_ONLY", "SEMANTIC"] = "COST_ONLY"
    destination_name: str = Field(min_length=1, max_length=MAX_DESTINATION_LENGTH)
    start_date: date
    end_date: date
    replacement_target: RevisionTarget | None = None


SYSTEM_INSTRUCTIONS = """You generate a structured group-trip itinerary using per-person costs.
Use only the destination, dates, limiting per-person budget, currency, group size, interests, food
preferences, travel preferences, pace, mobility preference, weather, and constraints supplied by
the application. Weather is optional: when it is unavailable, do not invent weather conditions.
All estimated costs use the PER_PERSON basis. For shared costs such as lodging or local transport,
return the estimated share for one traveler. Include at least one cost line for every required
category: flights, lodging, food, local_transport, and activities. Organize every cost line by
travel day, with at least one line on every day. Day numbers start at 1 and cover only the supplied
date range. Positions must be sequential from 1 within each day. Use integer cents and make
estimated_total_cost_cents equal the exact sum of all line-item costs. Keep that total at or below
the limiting per-person budget.

For every item, provide trusted semantic metadata: tags, environment (INDOOR, OUTDOOR, or MIXED),
mobility (LOW, MEDIUM, or HIGH), and intensity (LOW, MEDIUM, or HIGH). Do not encode metadata in
titles. Titles may name suggested venues, but they are unverified planning suggestions, not live
availability, verified prices, reservations, current weather, or booking quotes. Never change the
selected destination or supplied dates.
"""


REVISION_SYSTEM_INSTRUCTIONS = """Interpret the free-form revision instruction against the
structured trip context and current itinerary. Return operation COST_ONLY for budget-only changes
and SEMANTIC for preference, food, pace, mobility, weather, or activity-content changes. For
COST_ONLY, preserve every item's day, position, title, and item count; only costs may change. For
SEMANTIC, you may rebalance or replace itinerary content only when the current items contain
trusted metadata, and every returned item must contain trusted metadata too. Never infer metadata
from position or title prefixes.

Always preserve the selected destination and supplied dates. All costs use the PER_PERSON basis;
shared costs must be represented as one traveler's estimated share. Keep every day in the supplied
trip range, positions sequential, costs nonnegative, and the exact total equal to the sum of item
costs. Keep the total at or below the limiting per-person budget. For an activity replacement,
return replacement_target identifying the exact existing day, position, and title being replaced.
Return APPLIED only when the requested
change was made, NO_CHANGE when the current itinerary already satisfies the request, or UNSUPPORTED
when it cannot be applied safely. For NO_CHANGE or UNSUPPORTED, copy the current items and total
exactly. Weather is optional; never invent weather when it is unavailable. Estimates are not live
prices, availability, reservations, or booking quotes.
"""


class OpenAIItineraryProvider:
    """Generate validated itinerary drafts with the OpenAI Responses API."""

    def __init__(
        self,
        *,
        client: Any,
        model: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not model.strip():
            raise ItineraryProviderConfigurationError("OPENAI_MODEL must not be blank")
        if not MIN_TIMEOUT_SECONDS <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise ItineraryProviderConfigurationError(
                f"OPENAI_TIMEOUT_SECONDS must be between {MIN_TIMEOUT_SECONDS:g} and "
                f"{MAX_TIMEOUT_SECONDS:g}"
            )
        self._client = client
        self.model = model.strip()
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls) -> OpenAIItineraryProvider:
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise ItineraryProviderConfigurationError("OPENAI_API_KEY is not configured")

        model = os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
        raw_timeout = os.getenv("OPENAI_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
        try:
            timeout_seconds = float(raw_timeout)
        except ValueError as error:
            raise ItineraryProviderConfigurationError(
                "OPENAI_TIMEOUT_SECONDS must be a number"
            ) from error

        if not MIN_TIMEOUT_SECONDS <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise ItineraryProviderConfigurationError(
                f"OPENAI_TIMEOUT_SECONDS must be between {MIN_TIMEOUT_SECONDS:g} and "
                f"{MAX_TIMEOUT_SECONDS:g}"
            )

        return cls(
            client=OpenAI(
                api_key=api_key,
                max_retries=OPENAI_MAX_RETRIES,
                timeout=timeout_seconds,
            ),
            model=model,
            timeout_seconds=timeout_seconds,
        )

    def generate(self, context: TripContext) -> ItineraryDraft:
        travel_days, budget_cents, currency, destination_name, group_size = validate_context(
            context
        )
        request_context = {
            "selected_destination": destination_name,
            "travel_dates": {
                "start_date": context.start_date,
                "end_date": context.end_date,
                "number_of_days": travel_days,
            },
            "limiting_per_person_budget": {
                "amount_cents": budget_cents,
                "currency": currency,
            },
            "group_size": group_size,
            "interests": normalize_interests(context.interests),
            **preference_payload(context),
            "required_cost_categories": [category.value for category in CostCategory],
            "constraints": [
                "Keep the selected destination unchanged.",
                "Treat all costs as unverified per-person planning estimates.",
                "Represent shared costs as the estimated share for one traveler.",
                "Do not make voting, authorization, approval, or membership decisions.",
            ],
        }

        try:
            response = self._client.responses.parse(
                model=self.model,
                input=[
                    {"role": "system", "content": SYSTEM_INSTRUCTIONS},
                    {
                        "role": "user",
                        "content": json.dumps(request_context, sort_keys=True),
                    },
                ],
                text_format=GeneratedItineraryDraft,
                store=False,
                timeout=self.timeout_seconds,
            )
        except (APITimeoutError, TimeoutError) as error:
            raise ItineraryProviderTimeoutError("OpenAI itinerary generation timed out") from error
        except OpenAIError as error:
            raise ItineraryProviderError("OpenAI itinerary generation failed") from error

        try:
            parsed = GeneratedItineraryDraft.model_validate(response.output_parsed)
        except (AttributeError, TypeError, ValidationError) as error:
            raise ItineraryProviderResponseError(
                "OpenAI returned an incomplete or invalid itinerary"
            ) from error

        validate_output(parsed, travel_days, budget_cents, currency)
        validate_identity(
            parsed.destination_name,
            parsed.start_date,
            parsed.end_date,
            destination_name,
            date.fromisoformat(context.start_date),
            date.fromisoformat(context.end_date),
        )
        return issue_trusted_draft(
            items=[
                ItineraryItemInput(
                    day_number=item.day_number,
                    position=item.position,
                    title=item.title.strip(),
                    estimated_cost=cents_to_float(item.estimated_cost_cents),
                    metadata=metadata_from_generated_item(item),
                )
                for item in parsed.items
            ],
            estimated_total_cost=cents_to_float(parsed.estimated_total_cost_cents),
        )

    def revise(
        self,
        context: TripContext,
        current: ItineraryDraft,
        current_version: int,
        instruction: str,
    ) -> ItineraryDraft:
        travel_days, budget_cents, currency, destination_name, group_size = validate_context(
            context
        )
        if current_version < 1:
            raise ItineraryProviderInputError("Current itinerary version must be positive")
        normalized_instruction = normalize_revision_instruction(instruction)
        intent = parse_revision_intent(normalized_instruction, context.interests)
        current_items = serialize_current_draft(current)
        current_total_cents = amount_to_cents(
            current.estimated_total_cost,
            "Current itinerary total",
            allow_zero=True,
        )
        try:
            current_payload = RevisionCurrentDraft.model_validate(
                {
                    "cost_basis": PER_PERSON_COST_BASIS,
                    "currency": currency,
                    "items": current_items,
                    "estimated_total_cost_cents": current_total_cents,
                }
            )
        except ValidationError as error:
            raise ItineraryProviderInputError(
                "Current itinerary contains invalid or unsupported data"
            ) from error
        validate_current_revision_draft(
            current_payload,
            travel_days,
            budget_cents,
            currency,
        )
        validate_revision_intent_against_current(
            intent,
            current_payload,
            trusted_source=is_issued_trusted_draft(current),
        )

        request_context = {
            "selected_destination": destination_name,
            "travel_dates": {
                "start_date": context.start_date,
                "end_date": context.end_date,
                "number_of_days": travel_days,
            },
            "limiting_per_person_budget": {
                "amount_cents": budget_cents,
                "currency": currency,
            },
            "group_size": group_size,
            "interests": normalize_interests(context.interests),
            **preference_payload(context),
            "current_itinerary_version": current_version,
            "current_itinerary": current_payload.model_dump(
                mode="json",
                exclude_none=True,
                exclude_defaults=True,
            ),
            "revision_instruction": normalized_instruction,
        }

        try:
            response = self._client.responses.parse(
                model=self.model,
                input=[
                    {"role": "system", "content": REVISION_SYSTEM_INSTRUCTIONS},
                    {
                        "role": "user",
                        "content": json.dumps(request_context, sort_keys=True),
                    },
                ],
                text_format=GeneratedRevisionDraft,
                store=False,
                timeout=self.timeout_seconds,
            )
        except (APITimeoutError, TimeoutError) as error:
            raise ItineraryProviderTimeoutError("OpenAI itinerary revision timed out") from error
        except OpenAIError as error:
            raise ItineraryProviderError("OpenAI itinerary revision failed") from error

        try:
            revised = GeneratedRevisionDraft.model_validate(response.output_parsed)
        except (AttributeError, TypeError, ValidationError) as error:
            raise ItineraryProviderResponseError(
                "OpenAI returned an incomplete or invalid itinerary revision"
            ) from error

        if revised.currency.upper() != currency:
            raise ItineraryProviderResponseError(
                "Itinerary currency does not match the trip budget"
            )
        validate_identity(
            revised.destination_name,
            revised.start_date,
            revised.end_date,
            destination_name,
            date.fromisoformat(context.start_date),
            date.fromisoformat(context.end_date),
        )
        if revised.outcome == "APPLIED":
            validate_applied_revision_draft(
                revised,
                travel_days,
                budget_cents,
                currency,
            )
        content_changed = validate_revision_output(
            revised,
            current_payload,
            intent,
        )
        if revised.outcome == "UNSUPPORTED":
            raise ItineraryRevisionUnsupportedError(
                "The requested itinerary revision cannot be applied safely"
            )
        if not content_changed:
            return current
        return to_itinerary_draft(revised, current_payload, current)


def validate_context(context: TripContext) -> tuple[int, int, str, str, int]:
    destination_name = " ".join(context.destination_name.split())
    if not context.selected_destination_id.strip() or not destination_name:
        raise ItineraryProviderInputError("A selected destination is required")
    if len(destination_name) > MAX_DESTINATION_LENGTH:
        raise ItineraryProviderInputError(
            f"Destination name must not exceed {MAX_DESTINATION_LENGTH} characters"
        )
    try:
        start_date = date.fromisoformat(context.start_date)
        end_date = date.fromisoformat(context.end_date)
    except ValueError as error:
        raise ItineraryProviderInputError("Trip dates must use ISO format YYYY-MM-DD") from error
    if end_date < start_date:
        raise ItineraryProviderInputError("Trip end date must not be before start date")

    travel_days = (end_date - start_date).days + 1
    if travel_days > MAX_TRAVEL_DAYS:
        raise ItineraryProviderInputError(f"Trip length must not exceed {MAX_TRAVEL_DAYS} days")

    budget_cents = amount_to_cents(context.budget.max_amount, "Limiting per-person budget")
    currency = context.budget.currency.strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ItineraryProviderInputError("Budget currency must be a three-letter code")
    group_size = sum(member.voting_eligible for member in context.eligible_members)
    if not 1 <= group_size <= MAX_ELIGIBLE_MEMBERS:
        raise ItineraryProviderInputError(
            f"Eligible group size must be between 1 and {MAX_ELIGIBLE_MEMBERS}"
        )
    if context.weather is not None:
        trip_dates = {
            start_date.fromordinal(start_date.toordinal() + offset) for offset in range(travel_days)
        }
        for weather_day in context.weather:
            if weather_day.date not in trip_dates:
                raise ItineraryProviderInputError(
                    "Weather dates must fall within the trip date range"
                )
    return travel_days, budget_cents, currency, destination_name, group_size


def normalize_interests(interests: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for interest in interests:
        printable = "".join(character for character in interest if character.isprintable())
        value = " ".join(printable.split())[:MAX_INTEREST_LENGTH]
        key = value.casefold()
        if value and key not in seen:
            normalized.append(value)
            seen.add(key)
        if len(normalized) == MAX_INTERESTS:
            break
    return normalized


def preference_payload(context: TripContext) -> dict[str, Any]:
    food = context.food_preferences
    weather = None
    if context.weather is not None:
        weather = [day.model_dump(mode="json") for day in context.weather]
    return {
        "food_preferences": food.model_dump(mode="json") if food is not None else None,
        "travel_preferences": normalize_interests(context.travel_preferences),
        "pace": context.pace,
        "mobility_preference": context.mobility_preference,
        "weather": weather,
        "weather_note": (
            "Structured weather data supplied; use it as planning context."
            if weather is not None
            else "Weather data is unavailable for these dates. Do not assume specific conditions."
        ),
    }


def metadata_from_generated_item(item: GeneratedItineraryItem) -> ItineraryItemMetadata:
    validate_metadata_values(
        item.category.value, item.tags, item.environment, item.mobility, item.intensity
    )
    return ItineraryItemMetadata(
        category=item.category.value,
        tags=item.tags,
        environment=item.environment,
        mobility=item.mobility,
        intensity=item.intensity,
    )


def validate_metadata_values(
    category: str,
    tags: list[str],
    environment: str,
    mobility: str,
    intensity: str,
) -> None:
    if category not in {item.value for item in CostCategory}:
        raise ItineraryProviderResponseError("Itinerary category metadata is invalid")
    if any(not tag.strip() for tag in tags):
        raise ItineraryProviderResponseError("Itinerary semantic metadata must be nonblank")
    if environment.upper() not in VALID_ENVIRONMENTS:
        raise ItineraryProviderResponseError("Itinerary environment metadata is invalid")
    if mobility.upper() not in VALID_MOBILITY:
        raise ItineraryProviderResponseError("Itinerary mobility metadata is invalid")
    if intensity.upper() not in VALID_INTENSITY:
        raise ItineraryProviderResponseError("Itinerary intensity metadata is invalid")


def validate_identity(
    returned_destination: str,
    returned_start: date,
    returned_end: date,
    destination: str,
    start_date: date,
    end_date: date,
) -> None:
    if " ".join(returned_destination.split()) != destination:
        raise ItineraryProviderResponseError(
            "OpenAI returned an incomplete or invalid itinerary: destination cannot change"
        )
    if returned_start != start_date:
        raise ItineraryProviderResponseError(
            "OpenAI returned an incomplete or invalid itinerary: start date cannot change"
        )
    if returned_end != end_date:
        raise ItineraryProviderResponseError(
            "OpenAI returned an incomplete or invalid itinerary: end date cannot change"
        )


def validate_output(
    draft: GeneratedItineraryDraft,
    travel_days: int,
    budget_cents: int,
    currency: str,
) -> None:
    if draft.currency.upper() != currency:
        raise ItineraryProviderResponseError("Itinerary currency does not match the trip budget")

    expected_days = set(range(1, travel_days + 1))
    actual_days = {item.day_number for item in draft.items}
    if actual_days != expected_days:
        raise ItineraryProviderResponseError(
            "Itinerary must include cost items for every travel day and no days outside the trip"
        )

    actual_categories = {item.category for item in draft.items}
    if actual_categories != set(CostCategory):
        raise ItineraryProviderResponseError(
            "Itinerary must account for flights, lodging, food, local transport, and activities"
        )

    items_by_day: dict[int, list[GeneratedItineraryItem]] = {}
    for item in draft.items:
        items_by_day.setdefault(item.day_number, []).append(item)
        if not item.title.strip() or item.title != item.title.strip():
            raise ItineraryProviderResponseError(
                "Itinerary item titles must be nonblank and trimmed"
            )

    for items in items_by_day.values():
        if len(items) > MAX_ITEMS_PER_DAY:
            raise ItineraryProviderResponseError(
                f"Itinerary must not exceed {MAX_ITEMS_PER_DAY} items per day"
            )
        positions = sorted(item.position for item in items)
        if positions != list(range(1, len(items) + 1)):
            raise ItineraryProviderResponseError(
                "Itinerary item positions must be sequential from 1 within each day"
            )

    item_total_cents = sum(item.estimated_cost_cents for item in draft.items)
    if draft.estimated_total_cost_cents != item_total_cents:
        raise ItineraryProviderResponseError(
            "Estimated total cost must equal the sum of itinerary item costs"
        )
    if draft.estimated_total_cost_cents > budget_cents:
        raise ItineraryProviderResponseError(
            "Estimated per-person total cost exceeds the limiting per-person budget"
        )


def amount_to_cents(
    value: float | Decimal,
    field_name: str,
    *,
    allow_zero: bool = False,
) -> int:
    try:
        amount = Decimal(str(value))
        cents = amount * 100
    except InvalidOperation as error:
        raise ItineraryProviderInputError(f"{field_name} must be a valid amount") from error
    if not cents.is_finite() or cents != cents.to_integral_value():
        raise ItineraryProviderInputError(f"{field_name} must use no more than two decimal places")
    cents_value = int(cents)
    minimum = 0 if allow_zero else 1
    if cents_value < minimum or cents_value > MAX_DATABASE_AMOUNT_CENTS:
        raise ItineraryProviderInputError(f"{field_name} is outside the supported range")
    return cents_value


def cents_to_float(cents: int) -> float:
    return float(Decimal(cents) / Decimal(100))


def normalize_revision_instruction(instruction: str) -> str:
    printable = "".join(character for character in instruction if character.isprintable())
    normalized = " ".join(printable.split())
    if not normalized:
        raise ItineraryProviderInputError("Revision instruction must not be blank")
    if len(normalized) > MAX_REVISION_INSTRUCTION_LENGTH:
        raise ItineraryProviderInputError(
            f"Revision instruction must not exceed {MAX_REVISION_INSTRUCTION_LENGTH} characters"
        )
    return normalized


def parse_revision_intent(instruction: str, interests: list[str]) -> RevisionIntent:
    """Classify hard safety constraints while leaving meaning to the model.

    This is deliberately a small safety classifier, not an intent engine. It extracts only the
    day needed for deterministic cheaper-day validation and blocks requests that explicitly try to
    mutate backend-owned trip fields before an API call.
    """
    del interests
    normalized = instruction.casefold()
    if re.search(r"\b(change|move|switch)\b.*\b(destination|city|date|dates|trip)\b", normalized):
        raise ItineraryRevisionUnsupportedError(
            "Destination and date changes are not supported by the itinerary provider"
        )
    if re.search(r"\b(book|reserve|availability|live price|hotel)\b", normalized):
        raise ItineraryRevisionUnsupportedError(
            "Booking and live-availability requests are not supported by the itinerary provider"
        )
    if re.search(r"\b(cheaper|reduce|lower|less expensive)\b", normalized) and re.search(
        r"\breplace\b", normalized
    ):
        raise ItineraryRevisionUnsupportedError(
            "Multiple cost and replacement operations are not supported together"
        )

    if re.search(r"\b(walking|physically demanding|mobility)\b", normalized):
        return RevisionIntent(kind=RevisionIntentKind.SEMANTIC, target_text="walking")

    day_match = re.search(
        r"\b(?:day(?: number)?\s*(?P<number>\d{1,2})|the\s*(?P<ordinal>"
        + "|".join(re.escape(value) for value in ORDINAL_DAY_NUMBERS)
        + r")\s+day)\b",
        normalized,
    )
    cheaper = re.search(r"\b(cheaper|reduce|lower|less expensive)\b", normalized)
    semantic_markers = (
        "rain",
        "weather",
        "food",
        "museum",
        "nature",
        "relax",
        "nightlife",
        "walk",
        "walking",
        "culture",
        "art",
    )
    if cheaper is not None and any(marker in normalized for marker in semantic_markers):
        return RevisionIntent(kind=RevisionIntentKind.SEMANTIC)
    if day_match is not None and cheaper is not None and re.search(r"\bday\b", normalized):
        day = (
            int(day_match.group("number"))
            if day_match.group("number")
            else ORDINAL_DAY_NUMBERS[day_match.group("ordinal")]
        )
        if re.search(r"\b(cheaper|reduce|lower|less expensive)\b", normalized):
            return RevisionIntent(kind=RevisionIntentKind.CHEAPER_DAY, target_day=day)
    if re.search(r"\b(cost|costs|budget|cheaper|spend|price|under \$?\d)", normalized):
        target_text = None
        if re.search(r"\b(food|dining|meals|restaurants?)\b", normalized):
            target_text = CostCategory.FOOD.value
        elif re.search(r"\b(activities|activity|attractions?)\b", normalized):
            target_text = CostCategory.ACTIVITIES.value
        return RevisionIntent(kind=RevisionIntentKind.COST_ONLY, target_text=target_text)
    if normalized.startswith("replace "):
        if re.search(r"\b(another|activity)\b", normalized):
            raise ItineraryRevisionUnsupportedError(
                "An activity replacement must contain one clear replacement intent and is not supported"
            )
        return RevisionIntent(kind=RevisionIntentKind.REPLACE_ACTIVITY)
    if "activities" in normalized and "interest" in normalized:
        return RevisionIntent(kind=RevisionIntentKind.SEMANTIC, target_text="interests")
    return RevisionIntent(kind=RevisionIntentKind.SEMANTIC)


def parse_day_reference(value: str) -> int:
    numeric_match = re.search(r"\d{1,2}", value)
    if numeric_match is not None:
        return int(numeric_match.group())
    ordinal = value.removeprefix("the ").removesuffix(" day")
    return ORDINAL_DAY_NUMBERS[ordinal]


def is_supported_activity_phrase(value: str) -> bool:
    prohibited_tokens = {
        "activity",
        "and",
        "another",
        "also",
        "availability",
        "book",
        "booking",
        "change",
        "date",
        "dates",
        "destination",
        "different",
        "hotel",
        "then",
        "something",
        "trip",
        "weather",
    }
    return not ({token.casefold() for token in value.split()} & prohibited_tokens)


def validate_revision_draft_common(
    draft: RevisionCurrentDraft,
    travel_days: int,
    budget_cents: int,
    currency: str,
    error_type: type[ItineraryProviderInputError | ItineraryProviderResponseError],
) -> None:
    if draft.currency.upper() != currency:
        raise error_type("Itinerary currency does not match the trip budget")

    expected_days = set(range(1, travel_days + 1))
    actual_days = {item.day_number for item in draft.items}
    if actual_days != expected_days:
        raise error_type(
            "Itinerary must include cost items for every travel day and no days outside the trip"
        )

    items_by_day: dict[int, list[RevisionItineraryItem]] = {}
    for item in draft.items:
        items_by_day.setdefault(item.day_number, []).append(item)
        if not item.title.strip() or item.title != item.title.strip():
            raise error_type("Itinerary item titles must be nonblank and trimmed")

    for items in items_by_day.values():
        if len(items) > MAX_ITEMS_PER_DAY:
            raise error_type(f"Itinerary must not exceed {MAX_ITEMS_PER_DAY} items per day")
        positions = [item.position for item in items]
        if positions != list(range(1, len(items) + 1)):
            raise error_type(
                "Itinerary items must be ordered with sequential positions from 1 within each day"
            )

    item_total_cents = sum(item.estimated_cost_cents for item in draft.items)
    if draft.estimated_total_cost_cents != item_total_cents:
        raise error_type("Estimated total cost must equal the sum of itinerary item costs")
    if draft.estimated_total_cost_cents > budget_cents:
        raise error_type("Estimated per-person total cost exceeds the limiting per-person budget")


def validate_current_revision_draft(
    draft: RevisionCurrentDraft,
    travel_days: int,
    budget_cents: int,
    currency: str,
) -> None:
    validate_revision_draft_common(
        draft,
        travel_days,
        budget_cents,
        currency,
        ItineraryProviderInputError,
    )


def validate_applied_revision_draft(
    draft: GeneratedRevisionDraft,
    travel_days: int,
    budget_cents: int,
    currency: str,
) -> None:
    validate_revision_draft_common(
        draft,
        travel_days,
        budget_cents,
        currency,
        ItineraryProviderResponseError,
    )


def validate_revision_intent_against_current(
    intent: RevisionIntent,
    current: RevisionCurrentDraft,
    *,
    trusted_source: bool,
) -> None:
    if intent.kind in {RevisionIntentKind.CHEAPER_DAY, RevisionIntentKind.COST_ONLY}:
        if intent.kind == RevisionIntentKind.CHEAPER_DAY and not any(
            item.day_number == intent.target_day for item in current.items
        ):
            raise ItineraryRevisionUnsupportedError(
                "The requested cheaper day is not present in the itinerary"
            )
        if (
            intent.kind == RevisionIntentKind.COST_ONLY
            and intent.target_text is not None
            and (not trusted_source or any(not has_metadata(item) for item in current.items))
        ):
            raise ItineraryRevisionUnsupportedError(
                "Category-specific cost revisions require trusted structured metadata "
                "and are not supported"
            )
        return
    if intent.kind in {RevisionIntentKind.SEMANTIC, RevisionIntentKind.REPLACE_ACTIVITY}:
        if not trusted_source:
            raise ItineraryRevisionUnsupportedError(
                "Semantic revisions require a provider-generated trusted draft"
            )
        if not current.items or any(not has_metadata(item) for item in current.items):
            if intent.kind == RevisionIntentKind.REPLACE_ACTIVITY:
                raise ItineraryRevisionUnsupportedError(
                    "Activity replacement requires trusted structured activity categories "
                    "and is not supported"
                )
            if intent.target_text == "walking":
                raise ItineraryRevisionUnsupportedError(
                    "Walking reduction cannot be verified from the current itinerary fields "
                    "and is not supported"
                )
            if intent.target_text == "interests":
                raise ItineraryRevisionUnsupportedError(
                    "Interest-alignment revisions require verifiable structured activity tags "
                    "and are not supported"
                )
            raise ItineraryRevisionUnsupportedError(
                "Semantic revisions require trusted structured itinerary metadata and are not supported"
            )
        return


def has_metadata(item: RevisionItineraryItem) -> bool:
    return (
        item.category is not None and item.environment != "UNKNOWN" and item.mobility != "UNKNOWN"
    )


def metadata_from_revision_item(item: RevisionItineraryItem) -> ItineraryItemMetadata | None:
    if item.category is None:
        return None
    validate_metadata_values(
        item.category,
        item.tags,
        item.environment,
        item.mobility,
        item.intensity,
    )
    return ItineraryItemMetadata(
        category=item.category,
        tags=item.tags,
        environment=item.environment,
        mobility=item.mobility,
        intensity=item.intensity,
    )


def metadata_for_current_item(item: ItineraryItemInput) -> dict[str, Any] | None:
    return item.metadata.model_dump(mode="json") if item.metadata is not None else None


def serialize_current_draft(current: ItineraryDraft) -> list[dict[str, Any]]:
    return [
        {
            "day_number": item.day_number,
            "position": item.position,
            "title": item.title,
            "estimated_cost_cents": amount_to_cents(
                item.estimated_cost,
                "Current itinerary item cost",
                allow_zero=True,
            ),
            **(metadata_for_current_item(item) or {}),
        }
        for item in current.items
    ]


def validate_semantic_revision(
    revised: GeneratedRevisionDraft,
    current: RevisionCurrentDraft,
) -> None:
    if any(not has_metadata(item) for item in revised.items):
        raise ItineraryProviderResponseError(
            "Semantic revisions must return trusted metadata for every item"
        )
    if not any(
        revision_item_fingerprint(item) != revision_item_fingerprint(previous)
        for item, previous in zip(revised.items, current.items, strict=False)
    ) and len(revised.items) == len(current.items):
        raise ItineraryProviderResponseError("A semantic revision must change itinerary content")


def validate_cost_only_revision(
    revised: GeneratedRevisionDraft,
    current: RevisionCurrentDraft,
    *,
    target_day: int | None,
    target_category: str | None,
) -> None:
    current_structure = [(item.day_number, item.position, item.title) for item in current.items]
    revised_structure = [(item.day_number, item.position, item.title) for item in revised.items]
    if revised_structure != current_structure:
        raise ItineraryProviderResponseError("A cost-only revision may change only estimated costs")
    current_total = current.estimated_total_cost_cents
    revised_total = revised.estimated_total_cost_cents
    if revised_total >= current_total:
        raise ItineraryProviderResponseError(
            "A cost-only revision must strictly reduce the requested cost"
        )
    if target_category is not None:
        for previous, updated in zip(current.items, revised.items, strict=True):
            if (
                previous.estimated_cost_cents != updated.estimated_cost_cents
                and previous.category != target_category
            ):
                raise ItineraryProviderResponseError(
                    "A category-specific cost revision changed an unrelated category"
                )
    if target_day is not None:
        validate_cheaper_day(
            revised,
            current,
            RevisionIntent(
                kind=RevisionIntentKind.CHEAPER_DAY,
                target_day=target_day,
            ),
        )


def validate_revision_intent_against_current_legacy(
    intent: RevisionIntent,
    current: RevisionCurrentDraft,
) -> None:
    """Compatibility alias retained for downstream imports during the transition."""
    validate_revision_intent_against_current(intent, current, trusted_source=False)


def to_itinerary_draft(
    draft: GeneratedRevisionDraft,
    current_payload: RevisionCurrentDraft,
    current: ItineraryDraft,
) -> ItineraryDraft:
    canonical_titles = {(item.day_number, item.position): item.title for item in current.items}
    current_items = {(item.day_number, item.position): item for item in current_payload.items}

    def canonical_title(item: RevisionItineraryItem) -> str:
        key = (item.day_number, item.position)
        previous = current_items.get(key)
        if previous is not None and revision_item_fingerprint(previous) == (
            revision_item_fingerprint(item)
        ):
            return canonical_titles[key]
        return item.title.strip()

    return issue_trusted_draft(
        items=[
            ItineraryItemInput(
                day_number=item.day_number,
                position=item.position,
                title=canonical_title(item),
                estimated_cost=cents_to_float(item.estimated_cost_cents),
                metadata=metadata_from_revision_item(item),
            )
            for item in draft.items
        ],
        estimated_total_cost=cents_to_float(draft.estimated_total_cost_cents),
    )


def revision_item_fingerprint(
    item: GeneratedItineraryItem | RevisionItineraryItem,
) -> tuple[Any, ...]:
    return (
        item.day_number,
        item.position,
        item.title,
        item.estimated_cost_cents,
    )


def semantic_item_fingerprint(item: RevisionItineraryItem) -> tuple[Any, ...]:
    return (
        revision_item_fingerprint(item),
        item.category,
        tuple(item.tags),
        item.environment,
        item.mobility,
        item.intensity,
    )


def validate_revision_output(
    revised: GeneratedRevisionDraft,
    current: RevisionCurrentDraft,
    intent: RevisionIntent,
) -> bool:
    current_fingerprint = tuple(revision_item_fingerprint(item) for item in current.items)
    revised_fingerprint = tuple(revision_item_fingerprint(item) for item in revised.items)
    unchanged = (
        current_fingerprint == revised_fingerprint
        and current.estimated_total_cost_cents == revised.estimated_total_cost_cents
    )
    if revised.outcome in {"NO_CHANGE", "UNSUPPORTED"} and not unchanged:
        raise ItineraryProviderResponseError(
            "No-change and unsupported revisions must preserve the current itinerary"
        )
    if revised.outcome != "APPLIED":
        return False

    if intent.kind == RevisionIntentKind.CHEAPER_DAY:
        validate_cheaper_day(revised, current, intent)
    elif intent.kind == RevisionIntentKind.COST_ONLY:
        validate_cost_only_revision(
            revised,
            current,
            target_day=None,
            target_category=intent.target_text,
        )
    elif intent.kind in {RevisionIntentKind.SEMANTIC, RevisionIntentKind.REPLACE_ACTIVITY}:
        validate_semantic_revision(revised, current)
        if intent.kind == RevisionIntentKind.REPLACE_ACTIVITY:
            validate_activity_replacement(revised, current)
    return True


def validate_cheaper_day(
    revised: GeneratedRevisionDraft,
    current: RevisionCurrentDraft,
    intent: RevisionIntent,
) -> None:
    target_day = intent.target_day
    current_target_items = [item for item in current.items if item.day_number == target_day]
    revised_target_items = [item for item in revised.items if item.day_number == target_day]
    if not current_target_items or not revised_target_items:
        raise ItineraryProviderResponseError("The requested day is not present in the itinerary")
    if len(current.items) != len(revised.items):
        raise ItineraryProviderResponseError(
            "A cheaper-day revision must preserve the item count and ordering"
        )
    current_structure = [(item.day_number, item.position, item.title) for item in current.items]
    revised_structure = [(item.day_number, item.position, item.title) for item in revised.items]
    if revised_structure != current_structure:
        raise ItineraryProviderResponseError(
            "A cheaper-day revision may change only estimated costs on the requested day"
        )
    require_unchanged_items(
        [item for item in current.items if item.day_number != target_day],
        [item for item in revised.items if item.day_number != target_day],
        "A day-specific revision must preserve all unrelated days",
    )
    current_cost = sum(item.estimated_cost_cents for item in current_target_items)
    revised_cost = sum(item.estimated_cost_cents for item in revised_target_items)
    if revised_cost >= current_cost:
        raise ItineraryProviderResponseError(
            "A cheaper-day revision must strictly reduce that day's estimated cost"
        )


def validate_activity_replacement(
    revised: GeneratedRevisionDraft,
    current: RevisionCurrentDraft,
) -> None:
    target = revised.replacement_target
    if target is None:
        raise ItineraryProviderResponseError(
            "Activity replacement must identify one exact target item"
        )
    if len(revised.items) != len(current.items):
        raise ItineraryProviderResponseError(
            "Activity replacement must preserve the itinerary item count"
        )
    target_key = (target.day_number, target.position)
    previous_target = next(
        (item for item in current.items if (item.day_number, item.position) == target_key),
        None,
    )
    if previous_target is None or previous_target.title != target.title:
        raise ItineraryProviderResponseError(
            "Activity replacement target does not match the current itinerary"
        )
    if previous_target.category != CostCategory.ACTIVITIES.value:
        raise ItineraryProviderResponseError("Activity replacement target must be an activity")
    updated_target = next(
        item for item in revised.items if (item.day_number, item.position) == target_key
    )
    if updated_target.category != CostCategory.ACTIVITIES.value:
        raise ItineraryProviderResponseError("Activity replacement must remain an activity")
    if updated_target.title == previous_target.title:
        raise ItineraryProviderResponseError("Activity replacement must change the target title")
    changed_keys: list[tuple[int, int]] = []
    for previous, updated in zip(current.items, revised.items, strict=True):
        if (previous.day_number, previous.position) != (updated.day_number, updated.position):
            raise ItineraryProviderResponseError(
                "Activity replacement must preserve item day and position"
            )
        changed = semantic_item_fingerprint(previous) != semantic_item_fingerprint(updated)
        if changed:
            changed_keys.append((previous.day_number, previous.position))
        if changed and (previous.day_number, previous.position) != target_key:
            raise ItineraryProviderResponseError(
                "Activity replacement cannot modify items outside the requested target"
            )
    if changed_keys != [target_key]:
        raise ItineraryProviderResponseError(
            "Activity replacement must change exactly the requested target item"
        )


def require_unchanged_items(
    current: list[GeneratedItineraryItem] | list[RevisionItineraryItem],
    revised: list[GeneratedItineraryItem] | list[RevisionItineraryItem],
    message: str,
) -> None:
    if tuple(revision_item_fingerprint(item) for item in current) != tuple(
        revision_item_fingerprint(item) for item in revised
    ):
        raise ItineraryProviderResponseError(message)
