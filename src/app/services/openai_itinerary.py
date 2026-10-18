from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
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
MAX_REVISION_INSTRUCTION_LENGTH = 500
MAX_DATABASE_AMOUNT_CENTS = 999_999_999_999
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
    REPLACE_ACTIVITY = "replace_activity"


@dataclass(frozen=True)
class RevisionIntent:
    kind: RevisionIntentKind
    target_day: int | None = None
    target_text: str | None = None
    replacement_text: str | None = None


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


class _GeneratedRevisionDraft(_GeneratedItineraryDraft):
    outcome: Literal["APPLIED", "NO_CHANGE", "UNSUPPORTED"]
    outcome_reason: str = Field(min_length=1, max_length=240)


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


REVISION_SYSTEM_INSTRUCTIONS = """You revise an existing structured per-person trip itinerary.
Apply the member's instruction when it is feasible and safe. Preserve the selected destination,
travel dates, and all unrelated itinerary content wherever practical. If the instruction names a
specific day, copy every other day's items exactly, including category, title, position, and cost.
Keep at least one cost line for flights, lodging, food, local_transport, and activities, with at
least one line on every travel day and sequential positions starting at 1. All amounts use a
PER_PERSON basis and integer cents. The total must exactly equal the line-item sum and must not
exceed the limiting per-person budget. Return APPLIED for a feasible revision, NO_CHANGE when the
current content already satisfies it, or UNSUPPORTED when it requires changing destination or
dates, exceeds constraints, requests unavailable real-time facts, or otherwise cannot be applied
safely. For NO_CHANGE or UNSUPPORTED, copy the current items and total exactly. Estimates are not
verified availability, live prices, booking quotes, or current weather. Never make authorization,
voting, approval, or membership decisions.
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

    def revise(
        self,
        context: TripContext,
        current: ItineraryDraft,
        current_version: int,
        instruction: str,
    ) -> ItineraryDraft:
        travel_days, budget_cents, currency, destination_name, group_size = _validate_context(
            context
        )
        if current_version < 1:
            raise ItineraryProviderInputError("Current itinerary version must be positive")
        normalized_instruction = _normalize_revision_instruction(instruction)
        intent = _parse_revision_intent(normalized_instruction, context.interests)
        current_items = _serialize_current_draft(current)
        current_total_cents = _amount_to_cents(
            current.estimated_total_cost,
            "Current itinerary total",
            allow_zero=True,
        )
        current_payload = _GeneratedItineraryDraft.model_validate(
            {
                "cost_basis": "PER_PERSON",
                "currency": currency,
                "items": current_items,
                "estimated_total_cost_cents": current_total_cents,
            }
        )
        _validate_output(current_payload, travel_days, budget_cents, currency)
        _validate_revision_intent_against_current(intent, current_payload)

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
            "current_itinerary_version": current_version,
            "current_itinerary": current_payload.model_dump(mode="json"),
            "revision_instruction": normalized_instruction,
            "required_cost_categories": [category.value for category in CostCategory],
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
                text_format=_GeneratedRevisionDraft,
                store=False,
                timeout=self.timeout_seconds,
            )
        except (APITimeoutError, TimeoutError) as error:
            raise ItineraryProviderTimeoutError("OpenAI itinerary revision timed out") from error
        except OpenAIError as error:
            raise ItineraryProviderError("OpenAI itinerary revision failed") from error

        try:
            revised = _GeneratedRevisionDraft.model_validate(response.output_parsed)
        except (AttributeError, TypeError, ValidationError) as error:
            raise ItineraryProviderResponseError(
                "OpenAI returned an incomplete or invalid itinerary revision"
            ) from error

        _validate_output(revised, travel_days, budget_cents, currency)
        content_changed = _validate_revision_output(
            revised,
            current_payload,
            intent,
        )
        if revised.outcome == "UNSUPPORTED":
            raise ItineraryRevisionUnsupportedError(
                "The requested itinerary revision cannot be applied safely"
            )
        if not content_changed:
            return current.model_copy(deep=True)
        return _to_itinerary_draft(revised)


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


def _amount_to_cents(
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
        raise ItineraryProviderInputError(
            f"{field_name} must use no more than two decimal places"
        )
    cents_value = int(cents)
    minimum = 0 if allow_zero else 1
    if cents_value < minimum or cents_value > MAX_DATABASE_AMOUNT_CENTS:
        raise ItineraryProviderInputError(f"{field_name} is outside the supported range")
    return cents_value


def _cents_to_float(cents: int) -> float:
    return float(Decimal(cents) / Decimal(100))


def _category_label(category: CostCategory) -> str:
    return category.value.replace("_", " ").title()


def _normalize_revision_instruction(instruction: str) -> str:
    printable = "".join(character for character in instruction if character.isprintable())
    normalized = " ".join(printable.split())
    if not normalized:
        raise ItineraryProviderInputError("Revision instruction must not be blank")
    if len(normalized) > MAX_REVISION_INSTRUCTION_LENGTH:
        raise ItineraryProviderInputError(
            f"Revision instruction must not exceed {MAX_REVISION_INSTRUCTION_LENGTH} characters"
        )
    return normalized


def _parse_revision_intent(instruction: str, interests: list[str]) -> RevisionIntent:
    """Parse only the MVP's complete, single-intent instruction grammar.

    Supported forms are one cheaper-day command or one ``replace X with Y`` activity command.
    Recognized group-interest phrases are classified as unsupported. Partial matches and trailing
    clauses are also intentionally unsupported.
    """
    normalized = instruction.casefold()
    ordinal_days = "|".join(re.escape(value) for value in ORDINAL_DAY_NUMBERS)
    day_expression = rf"(?:day(?: number)? \d{{1,2}}|the (?:{ordinal_days}) day)"
    walking_patterns = (
        rf"(?:reduce|limit|minimi[sz]e) walking (?:on|for) {day_expression}[.!]?",
    )
    if any(re.fullmatch(pattern, normalized) for pattern in walking_patterns):
        raise ItineraryRevisionUnsupportedError(
            "Walking reduction cannot be verified from the current itinerary fields"
        )
    cheaper_patterns = (
        rf"make (?P<day>{day_expression}) cheaper[.!]?",
        rf"reduce (?:the )?(?:cost|price|budget) (?:for|on) (?P<day>{day_expression})[.!]?",
    )
    for pattern in cheaper_patterns:
        cheaper_match = re.fullmatch(pattern, normalized)
        if cheaper_match is not None:
            return RevisionIntent(
                kind=RevisionIntentKind.CHEAPER_DAY,
                target_day=_parse_day_reference(cheaper_match.group("day")),
            )

    word = r"[^\W_]+(?:[-'][^\W_]+)*"
    phrase = rf"{word}(?: {word}){{0,3}}"
    replace_match = re.fullmatch(
        rf"replace (?:(?:the|a|an) )?(?P<target>{phrase}) "
        rf"with (?:(?:the|a|an) )?(?P<replacement>{phrase})[.!]?",
        normalized,
    )
    if replace_match is not None:
        target_text = replace_match.group("target")
        replacement_text = replace_match.group("replacement")
        if not _is_supported_activity_phrase(target_text) or not _is_supported_activity_phrase(
            replacement_text
        ):
            raise ItineraryRevisionUnsupportedError(
                "An activity replacement must contain only one clear replacement intent"
            )
        return RevisionIntent(
            kind=RevisionIntentKind.REPLACE_ACTIVITY,
            target_text=target_text,
            replacement_text=replacement_text,
        )

    interest_patterns = (
        r"adjust activities to better match (?:our|group) interests[.!]?",
        r"(?:match|tailor|align) activities (?:to|with) (?:our|group) interests[.!]?",
    )
    if any(re.fullmatch(pattern, normalized) for pattern in interest_patterns):
        del interests
        raise ItineraryRevisionUnsupportedError(
            "Interest-alignment revisions require verifiable structured activity tags"
        )

    raise ItineraryRevisionUnsupportedError(
        "The revision instruction is not supported by the current MVP"
    )


def _parse_day_reference(value: str) -> int:
    numeric_match = re.search(r"\d{1,2}", value)
    if numeric_match is not None:
        return int(numeric_match.group())
    ordinal = value.removeprefix("the ").removesuffix(" day")
    return ORDINAL_DAY_NUMBERS[ordinal]


def _is_supported_activity_phrase(value: str) -> bool:
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


def _serialize_current_draft(current: ItineraryDraft) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for item in current.items:
        category, title = _split_category_title(item.title)
        serialized.append(
            {
                "day_number": item.day_number,
                "position": item.position,
                "category": category,
                "title": title,
                "estimated_cost_cents": _amount_to_cents(
                    item.estimated_cost,
                    "Current itinerary item cost",
                    allow_zero=True,
                ),
            }
        )
    return serialized


def _validate_revision_intent_against_current(
    intent: RevisionIntent,
    current: _GeneratedItineraryDraft,
) -> None:
    if intent.kind == RevisionIntentKind.CHEAPER_DAY:
        if not any(item.day_number == intent.target_day for item in current.items):
            raise ItineraryRevisionUnsupportedError(
                "The requested cheaper day is not present in the itinerary"
            )
        return
    if intent.kind == RevisionIntentKind.REPLACE_ACTIVITY:
        target_text = intent.target_text or ""
        matching = [
            item
            for item in current.items
            if item.category == CostCategory.ACTIVITIES
            and _normalized_phrase_matches(target_text, item.title)
        ]
        if len(matching) != 1:
            raise ItineraryRevisionUnsupportedError(
                "An activity replacement must identify exactly one current activity"
            )
        return


def _split_category_title(title: str) -> tuple[str, str]:
    normalized = title.strip()
    for category in CostCategory:
        prefix = f"{_category_label(category)}:"
        if normalized.casefold().startswith(prefix.casefold()):
            item_title = normalized[len(prefix) :].strip()
            if item_title:
                return category.value, item_title
    raise ItineraryProviderInputError(
        "Current itinerary items must include a supported cost-category prefix"
    )


def _to_itinerary_draft(draft: _GeneratedItineraryDraft) -> ItineraryDraft:
    return ItineraryDraft(
        items=[
            ItineraryItemInput(
                day_number=item.day_number,
                position=item.position,
                title=f"{_category_label(item.category)}: {item.title.strip()}",
                estimated_cost=_cents_to_float(item.estimated_cost_cents),
            )
            for item in draft.items
        ],
        estimated_total_cost=_cents_to_float(draft.estimated_total_cost_cents),
    )


def _revision_item_fingerprint(item: _GeneratedItineraryItem) -> tuple[Any, ...]:
    return (
        item.day_number,
        item.position,
        item.category,
        item.title,
        item.estimated_cost_cents,
    )


def _validate_revision_output(
    revised: _GeneratedRevisionDraft,
    current: _GeneratedItineraryDraft,
    intent: RevisionIntent,
) -> bool:
    current_fingerprint = tuple(_revision_item_fingerprint(item) for item in current.items)
    revised_fingerprint = tuple(_revision_item_fingerprint(item) for item in revised.items)
    unchanged = (
        current_fingerprint == revised_fingerprint
        and current.estimated_total_cost_cents == revised.estimated_total_cost_cents
    )
    if revised.outcome in {"NO_CHANGE", "UNSUPPORTED"} and not unchanged:
        raise ItineraryProviderResponseError(
            "No-change and unsupported revisions must preserve the current itinerary"
        )
    if unchanged:
        return False

    if revised.outcome != "APPLIED":
        return False

    if intent.kind == RevisionIntentKind.CHEAPER_DAY:
        _validate_cheaper_day(revised, current, intent)
    elif intent.kind == RevisionIntentKind.REPLACE_ACTIVITY:
        _validate_activity_replacement(revised, current, intent)
    return True


def _validate_cheaper_day(
    revised: _GeneratedRevisionDraft,
    current: _GeneratedItineraryDraft,
    intent: RevisionIntent,
) -> None:
    target_day = intent.target_day
    current_target_items = [item for item in current.items if item.day_number == target_day]
    revised_target_items = [item for item in revised.items if item.day_number == target_day]
    if not current_target_items or not revised_target_items:
        raise ItineraryProviderResponseError("The requested day is not present in the itinerary")
    _require_unchanged_items(
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


def _validate_activity_replacement(
    revised: _GeneratedRevisionDraft,
    current: _GeneratedItineraryDraft,
    intent: RevisionIntent,
) -> None:
    target_text = intent.target_text or ""
    replacement_text = intent.replacement_text or ""
    matching = [
        item
        for item in current.items
        if item.category == CostCategory.ACTIVITIES
        and _normalized_phrase_matches(target_text, item.title)
    ]
    if len(matching) != 1:
        raise ItineraryRevisionUnsupportedError(
            "An activity replacement must identify exactly one current activity"
        )
    target = matching[0]
    unrelated = [
        item
        for item in current.items
        if not (item.day_number == target.day_number and item.position == target.position)
    ]
    revised_unrelated = [
        item
        for item in revised.items
        if not (item.day_number == target.day_number and item.position == target.position)
    ]
    _require_unchanged_items(
        unrelated,
        revised_unrelated,
        "An activity replacement must preserve all unrelated itinerary content",
    )
    replacements = [
        item
        for item in revised.items
        if item.day_number == target.day_number and item.position == target.position
    ]
    if len(replacements) != 1:
        raise ItineraryProviderResponseError("The targeted activity must have one replacement")
    replacement = replacements[0]
    if (
        replacement.category != CostCategory.ACTIVITIES
        or replacement.title == target.title
        or _normalized_phrase_matches(target_text, replacement.title)
        or not _normalized_phrase_matches(replacement_text, replacement.title)
    ):
        raise ItineraryProviderResponseError(
            "The targeted activity was not replaced by the requested activity"
        )


def _normalized_phrase_matches(phrase: str, value: str) -> bool:
    normalized_phrase = " ".join(phrase.casefold().split())
    normalized_value = " ".join(value.casefold().split())
    phrase_pattern = (
        r"(?<!\w)" + re.escape(normalized_phrase).replace(r"\ ", r"\s+") + r"(?!\w)"
    )
    return re.search(phrase_pattern, normalized_value) is not None


def _require_unchanged_items(
    current: list[_GeneratedItineraryItem],
    revised: list[_GeneratedItineraryItem],
    message: str,
) -> None:
    if tuple(_revision_item_fingerprint(item) for item in current) != tuple(
        _revision_item_fingerprint(item) for item in revised
    ):
        raise ItineraryProviderResponseError(message)
