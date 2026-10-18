from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Literal, Protocol

from openai import APITimeoutError, OpenAI, OpenAIError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.schemas.itinerary import ItineraryDraft, ItineraryItemInput, TripContext

DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
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
MAX_DATABASE_AMOUNT_CENTS = 999_999_999_999


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


class ItineraryDraftProvider(Protocol):
    def generate(self, context: TripContext) -> ItineraryDraft: ...


class CostCategory(StrEnum):
    FLIGHTS = "flights"
    LODGING = "lodging"
    FOOD = "food"
    LOCAL_TRANSPORT = "local_transport"
    ACTIVITIES = "activities"


class _GeneratedItineraryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day_number: int = Field(ge=1, le=MAX_TRAVEL_DAYS)
    position: int = Field(ge=1, le=MAX_ITEMS_PER_DAY)
    category: CostCategory
    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)
    estimated_cost_cents: int = Field(ge=0, le=MAX_DATABASE_AMOUNT_CENTS)


class _GeneratedItineraryDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cost_basis: Literal["PER_PERSON"]
    currency: str = Field(min_length=3, max_length=3)
    items: list[_GeneratedItineraryItem] = Field(min_length=5)
    estimated_total_cost_cents: int = Field(ge=0, le=MAX_DATABASE_AMOUNT_CENTS)


SYSTEM_INSTRUCTIONS = """You generate a structured per-person group-trip cost itinerary.
Use only the destination name, dates, limiting per-person budget, currency, group size, interests,
and constraints supplied by the application. All estimated costs use a PER_PERSON basis. Include
at least one cost line for every required category: flights, lodging, food, local_transport, and
activities. Organize every cost line by travel day, with at least one line on every day. Day
numbers start at 1 and cover only the supplied date range. Positions must be sequential from 1
within each day. Use integer cents and make estimated_total_cost_cents equal the exact sum of all
line-item estimated_cost_cents. Keep that total at or below the limiting per-person budget. Values
are unverified planning estimates, not live prices or booking quotes. Do not claim live
availability, verified prices, current weather, reservations, voting results, authorization
decisions, approvals, or a different destination.
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
        travel_days, budget_cents, currency, destination_name, group_size = _validate_context(
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
            "interests": _normalize_interests(context.interests),
            "required_cost_categories": [category.value for category in CostCategory],
            "constraints": [
                "Keep the selected destination unchanged.",
                "Treat all costs as unverified per-person planning estimates.",
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
                text_format=_GeneratedItineraryDraft,
                store=False,
                timeout=self.timeout_seconds,
            )
        except (APITimeoutError, TimeoutError) as error:
            raise ItineraryProviderTimeoutError("OpenAI itinerary generation timed out") from error
        except OpenAIError as error:
            raise ItineraryProviderError("OpenAI itinerary generation failed") from error

        try:
            parsed = _GeneratedItineraryDraft.model_validate(response.output_parsed)
        except (AttributeError, TypeError, ValidationError) as error:
            raise ItineraryProviderResponseError(
                "OpenAI returned an incomplete or invalid itinerary"
            ) from error

        _validate_output(parsed, travel_days, budget_cents, currency)
        return ItineraryDraft(
            items=[
                ItineraryItemInput(
                    day_number=item.day_number,
                    position=item.position,
                    title=f"{_category_label(item.category)}: {item.title.strip()}",
                    estimated_cost=_cents_to_float(item.estimated_cost_cents),
                )
                for item in parsed.items
            ],
            estimated_total_cost=_cents_to_float(parsed.estimated_total_cost_cents),
        )


def _validate_context(context: TripContext) -> tuple[int, int, str, str, int]:
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
        raise ItineraryProviderInputError(
            f"Trip length must not exceed {MAX_TRAVEL_DAYS} days"
        )

    budget_cents = _amount_to_cents(context.budget.max_amount, "Group budget")
    currency = context.budget.currency.strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ItineraryProviderInputError("Budget currency must be a three-letter code")
    group_size = sum(member.voting_eligible for member in context.eligible_members)
    if not 1 <= group_size <= MAX_ELIGIBLE_MEMBERS:
        raise ItineraryProviderInputError(
            f"Eligible group size must be between 1 and {MAX_ELIGIBLE_MEMBERS}"
        )
    return travel_days, budget_cents, currency, destination_name, group_size


def _normalize_interests(interests: list[str]) -> list[str]:
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


def _validate_output(
    draft: _GeneratedItineraryDraft,
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

    items_by_day: dict[int, list[_GeneratedItineraryItem]] = {}
    for item in draft.items:
        items_by_day.setdefault(item.day_number, []).append(item)
        if not item.title.strip() or item.title != item.title.strip():
            raise ItineraryProviderResponseError("Itinerary item titles must be nonblank and trimmed")

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


def _amount_to_cents(value: float | Decimal, field_name: str) -> int:
    try:
        amount = Decimal(str(value))
        cents = amount * 100
    except InvalidOperation as error:
        raise ItineraryProviderInputError(f"{field_name} must be a valid amount") from error
    if not cents.is_finite() or cents != cents.to_integral_value():
        raise ItineraryProviderInputError(
            f"{field_name} must use no more than two decimal places"
        )
    cents_value = int(cents)
    if cents_value <= 0 or cents_value > MAX_DATABASE_AMOUNT_CENTS:
        raise ItineraryProviderInputError(f"{field_name} is outside the supported range")
    return cents_value


def _cents_to_float(cents: int) -> float:
    return float(Decimal(cents) / Decimal(100))


def _category_label(category: CostCategory) -> str:
    return category.value.replace("_", " ").title()
