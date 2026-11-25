from __future__ import annotations

import json
import logging
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

logger = logging.getLogger(__name__)

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


class ItineraryRevisionPatchError(ItineraryProviderResponseError):
    """Raised when a proposed revision patch fails deterministic validation."""


class ItineraryProviderTimeoutError(ItineraryProviderError):
    """Raised when the provider request exceeds the configured timeout."""


class ItineraryRevisionUnsupportedError(ItineraryProviderResponseError):
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
        *,
        allow_persisted_metadata_revisions: bool = False,
        interpreted_intent: RevisionIntent | None = None,
        repair_feedback: str | None = None,
    ) -> ItineraryDraft: ...


class CostCategory(StrEnum):
    FLIGHTS = "flights"
    ROAD_TRIP = "road_trip"
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
    situational_constraints: tuple[str, ...] = ()
    preserve_unrelated_days: bool = False
    explicit_preserve_terms: tuple[str, ...] = ()
    forbidden_terms: tuple[str, ...] = ()
    budget_target_cents: int | None = None
    explicit_preserve_all: bool = False


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


class DestinationRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=MAX_DESTINATION_LENGTH)
    location: str = Field(min_length=2, max_length=MAX_DESTINATION_LENGTH)
    estimated_cost_cents: int = Field(ge=0, le=MAX_DATABASE_AMOUNT_CENTS)
    match_explanation: str = Field(min_length=1, max_length=240)
    tags: list[str] = Field(default_factory=list, max_length=12)


class GeneratedDestinationRecommendations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[DestinationRecommendation] = Field(min_length=5, max_length=5)


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


class RevisionPatchItem(BaseModel):
    """One targeted change proposed against an existing itinerary slot."""

    model_config = ConfigDict(extra="forbid")

    day_number: int = Field(ge=1, le=MAX_TRAVEL_DAYS)
    position: int = Field(ge=1, le=MAX_ITEMS_PER_DAY)
    operation: Literal["UPDATE", "REPLACE"] = "UPDATE"
    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE_LENGTH)
    estimated_cost_cents: int | None = Field(default=None, ge=0, le=MAX_DATABASE_AMOUNT_CENTS)
    category: str | None = None
    tags: list[str] | None = Field(default=None, max_length=12)
    environment: str | None = None
    mobility: str | None = None
    intensity: str | None = None


class GeneratedRevisionPatch(BaseModel):
    """AI response containing only the itinerary slots that need to change."""

    model_config = ConfigDict(extra="forbid")

    cost_basis: Literal["PER_PERSON"]
    currency: str = Field(min_length=3, max_length=3)
    outcome: Literal["APPLIED", "NO_CHANGE", "UNSUPPORTED"]
    outcome_reason: str = Field(min_length=1, max_length=240)
    changes: list[RevisionPatchItem] = Field(
        default_factory=list, max_length=MAX_TRAVEL_DAYS * MAX_ITEMS_PER_DAY
    )
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
category supplied by the application, plus lodging, food, local_transport, and activities. Organize every cost line by
travel day, with at least one line on every day. Day numbers start at 1 and cover only the supplied
date range. Positions must be sequential from 1 within each day. Aim for morning, afternoon, and evening
activities each day, but include rest periods when appropriate. When the budget is constrained, prioritize
free or low-cost activities such as parks, beaches, walks, viewpoints, public spaces, and self-guided
exploration. A free activity may have an estimated cost of zero. Do not invent paid activities merely to
fill the schedule. Use integer cents and make
estimated_total_cost_cents equal the exact sum of all line-item costs. Keep that total at or below
the limiting per-person budget.

For every item, provide trusted semantic metadata: tags, environment (INDOOR, OUTDOOR, or MIXED),
mobility (LOW, MEDIUM, or HIGH), and intensity (LOW, MEDIUM, or HIGH). Do not encode metadata in
titles. Titles may name suggested venues, but they are unverified planning suggestions, not live
availability, verified prices, reservations, current weather, or booking quotes. Never change the
selected destination or supplied dates.
"""


REVISION_SYSTEM_INSTRUCTIONS = """Interpret the free-form revision instruction against the
structured trip context and current itinerary. Translate ordinary situational language into
travel-planning constraints without diagnosing illness or making medical recommendations. Return
an APPLIED patch containing only the existing day/position slots and fields that must change. Do
not reproduce unchanged itinerary items. The server will preserve and merge all untouched content.

Use UPDATE for a cost or small field change and REPLACE when an activity needs new content. A patch
must identify an existing day_number and position. For a replacement, include the new title and
complete trusted semantic metadata. For a semantic or cost goal, include only the slots and fields
needed to satisfy the request; do not reproduce unrelated itinerary content. A cost request may
replace an activity when that is necessary to reach the requested cost goal. If the user names a
day, patch only that day. Preserve explicitly named activities by omitting them from changes.
Unrelated fields are not automatically immutable unless the interpreted constraints say so.
For "more free time", represent the added downtime by replacing an in-scope activity slot with
a valid low-cost or free leisure/rest item and trusted metadata such as free_time, leisure, rest,
or unstructured; do not invent new day or position slots.

Always preserve the selected destination and supplied dates. All costs use the PER_PERSON basis and
are validated by the server. Return NO_CHANGE or UNSUPPORTED with an empty changes list. Never
invent weather, live prices, availability, reservations, or booking quotes.

If validation_feedback is supplied, treat it as a deterministic server violation from the previous
patch. Preserve the original instruction and interpreted scope, correct only that violation, and
return another PATCH only. Do not broaden the change to unrelated slots or fields.
"""


DESTINATION_SYSTEM_INSTRUCTIONS = """Recommend exactly five distinct travel destinations for a
group trip. Use the supplied shared dates, per-person budget, group size, and interests to rank
the recommendations. Do not return any destination listed in previous_destination_names unless
there are not enough reasonable alternatives. Costs are planning estimates per person, not live
prices or reservations. Return only the structured fields in the requested schema. Do not make
authentication, membership, voting, approval, or booking decisions. Do not invent live
availability or current prices.
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

    def generate_destination_recommendations(
        self,
        context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Generate structured destination candidates without writing application state."""
        try:
            response = self._client.responses.parse(
                model=self.model,
                input=[
                    {"role": "system", "content": DESTINATION_SYSTEM_INSTRUCTIONS},
                    {"role": "user", "content": json.dumps(context, sort_keys=True)},
                ],
                text_format=GeneratedDestinationRecommendations,
                store=False,
                timeout=self.timeout_seconds,
            )
        except (APITimeoutError, TimeoutError) as error:
            raise ItineraryProviderTimeoutError(
                "OpenAI destination recommendation timed out"
            ) from error
        except OpenAIError as error:
            raise ItineraryProviderError("OpenAI destination recommendation failed") from error

        try:
            parsed = GeneratedDestinationRecommendations.model_validate(response.output_parsed)
        except (AttributeError, TypeError, ValidationError) as error:
            raise ItineraryProviderResponseError(
                "OpenAI returned invalid destination recommendations"
            ) from error
        return [candidate.model_dump(mode="json") for candidate in parsed.candidates]

    def generate(self, context: TripContext) -> ItineraryDraft:
        travel_days, budget_cents, currency, destination_name, group_size = validate_context(
            context
        )
        transportation_mode = context.transportation.mode if context.transportation else None
        required_categories = required_cost_categories(transportation_mode)
        constraints = [
            "Keep the selected destination unchanged.",
            "Treat all costs as unverified per-person planning estimates.",
            "Represent shared costs as the estimated share for one traveler.",
            "Do not make voting, authorization, approval, or membership decisions.",
        ]
        if transportation_mode == "ROAD_TRIP":
            constraints.append(
                "Use a road trip for long-distance transportation; do not include flights."
            )
        elif transportation_mode == "FLIGHT":
            constraints.append(
                "Use flights for long-distance transportation; do not include road-trip costs."
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
            "transportation": context.transportation.model_dump()
            if context.transportation
            else None,
            "required_cost_categories": [category.value for category in required_categories],
            "constraints": constraints,
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

        validate_output(parsed, travel_days, budget_cents, currency, transportation_mode)
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
        *,
        allow_persisted_metadata_revisions: bool = False,
        interpreted_intent: RevisionIntent | None = None,
        repair_feedback: str | None = None,
    ) -> ItineraryDraft:
        travel_days, budget_cents, currency, destination_name, group_size = validate_context(
            context
        )
        if current_version < 1:
            raise ItineraryProviderInputError("Current itinerary version must be positive")
        normalized_instruction = normalize_revision_instruction(instruction)
        intent = interpreted_intent or parse_revision_intent(
            normalized_instruction, context.interests
        )
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
        persisted_metadata_trusted = all(has_valid_metadata(item) for item in current_payload.items)
        validate_revision_intent_against_current(
            intent,
            current_payload,
            trusted_source=is_issued_trusted_draft(current)
            or (allow_persisted_metadata_revisions and persisted_metadata_trusted),
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
            "interpreted_revision": {
                "kind": intent.kind.value,
                "target_day": intent.target_day,
                "target_category": intent.target_text,
                "scope": "day" if intent.target_day is not None else "itinerary",
                "situational_constraints": list(intent.situational_constraints),
                "preserve_unrelated_days": intent.preserve_unrelated_days,
                "explicit_preserve_terms": list(intent.explicit_preserve_terms),
                "forbidden_terms": list(intent.forbidden_terms),
                "budget_target_cents": intent.budget_target_cents,
                "explicit_preserve_all": intent.explicit_preserve_all,
            },
        }
        if repair_feedback is not None:
            request_context["validation_feedback"] = {
                "violation": repair_feedback,
                "repair_instruction": (
                    "Generate a corrected PATCH only. Preserve the original revision_instruction "
                    "and interpreted scope. Change no fields outside that scope."
                ),
            }

        try:
            response = self._client.responses.parse(
                model=self.model,
                input=[
                    {"role": "system", "content": REVISION_SYSTEM_INSTRUCTIONS},
                    {"role": "user", "content": json.dumps(request_context, sort_keys=True)},
                ],
                text_format=GeneratedRevisionPatch,
                store=False,
                timeout=self.timeout_seconds,
            )
        except (APITimeoutError, TimeoutError) as error:
            raise ItineraryProviderTimeoutError("OpenAI itinerary revision timed out") from error
        except OpenAIError as error:
            raise ItineraryProviderError("OpenAI itinerary revision failed") from error

        try:
            patch = coerce_revision_patch(response.output_parsed, current_payload)
            revised = apply_revision_patch(patch, current_payload, intent)
            validate_identity(
                revised.destination_name,
                revised.start_date,
                revised.end_date,
                destination_name,
                date.fromisoformat(context.start_date),
                date.fromisoformat(context.end_date),
            )
            if revised.outcome == "APPLIED":
                validate_applied_revision_draft(revised, travel_days, budget_cents, currency)
            content_changed = validate_revision_output(revised, current_payload, intent)
            if revised.outcome == "UNSUPPORTED":
                raise ItineraryRevisionUnsupportedError(
                    "The requested itinerary revision cannot be applied safely"
                )
            if not content_changed:
                return current
            return to_itinerary_draft(revised, current_payload, current)
        except (AttributeError, TypeError, ValidationError) as error:
            raise ItineraryRevisionPatchError(
                "OpenAI returned an incomplete or invalid itinerary revision patch"
            ) from error
        except ItineraryRevisionUnsupportedError:
            raise
        except ItineraryProviderResponseError as error:
            raise ItineraryRevisionPatchError(str(error)) from error


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


def required_cost_categories(transportation_mode: str | None) -> set[CostCategory]:
    categories = set(CostCategory)
    if transportation_mode == "ROAD_TRIP":
        categories.remove(CostCategory.FLIGHTS)
    else:
        categories.remove(CostCategory.ROAD_TRIP)
    return categories


def validate_output(
    draft: GeneratedItineraryDraft,
    travel_days: int,
    budget_cents: int,
    currency: str,
    transportation_mode: str | None = None,
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
    if actual_categories != required_cost_categories(transportation_mode):
        if transportation_mode != "ROAD_TRIP":
            raise ItineraryProviderResponseError(
                "Itinerary must account for flights, lodging, food, local transport, and activities"
            )
        raise ItineraryProviderResponseError(
            "Itinerary must account for road-trip transportation, lodging, food, local transport, and activities"
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
    """Extract enforceable constraints while leaving broad meaning to the model.

    The parser deliberately does not decide which individual fields a semantic request may
    change. It only records constraints that are explicit enough for deterministic validation.
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

    day_match = re.search(
        r"\b(?:day(?: number)?\s*(?P<number>\d{1,2})|the\s*(?P<ordinal>"
        + "|".join(re.escape(value) for value in ORDINAL_DAY_NUMBERS)
        + r")\s+day)\b",
        normalized,
    )
    target_day = None
    if day_match is not None:
        target_day = (
            int(day_match.group("number"))
            if day_match.group("number")
            else ORDINAL_DAY_NUMBERS[day_match.group("ordinal")]
        )
    situational_constraints: list[str] = []
    if re.search(
        r"\b(sick|ill|unwell|(?:not )?feeling well|tired|exhausted|parents?)\b", normalized
    ):
        situational_constraints.extend(
            [
                "low physical effort",
                "minimal walking",
                "easy access",
                "seating or places to rest",
                "quiet and relaxed pace",
            ]
        )
    if re.search(r"\b(rain|raining|storm|bad weather|indoors?|indoor)\b", normalized):
        situational_constraints.append("indoor or weather-protected")
    if re.search(r"\b(relax\w*|quiet|slow|chill|rest)\b", normalized):
        situational_constraints.append("relaxed activity with unstructured time")
    if re.search(r"\bfree[- ]?time|downtime|leisure|unstructured time\b", normalized):
        situational_constraints.append("more unstructured free time")
    if re.search(
        r"\b(two hours|2 hours|an hour|one hour|before (?:our|the) flight|flight)\b", normalized
    ):
        situational_constraints.extend(["short duration", "nearby with minimal transfer time"])
    if re.search(r"\b(spent too much|cheap|cheaper|budget|low[- ]cost|save money)\b", normalized):
        situational_constraints.append("lower-cost option")
    situational_constraints = list(dict.fromkeys(situational_constraints))
    preserve_unrelated_days = target_day is not None

    budget_target_cents = None
    budget_match = re.search(r"\b(?:under|below|less than)\s*\$?([0-9][0-9,]*)", normalized)
    if budget_match is not None:
        budget_target_cents = int(budget_match.group(1).replace(",", "")) * 100

    preserve_terms: list[str] = []
    generic_preservation = re.compile(
        r"(?:destination|trip|itinerary|budget|date|dates|travel days?|number of travel days|"
        r"important activities|most important activities|overall trip|group|membership|approvals?)",
        re.IGNORECASE,
    )
    for preserve_match in re.finditer(
        r"\b(?:keep|preserve|don't change|do not change)\s+(?P<term>[^,.]+?)(?=\s+(?:but|and|while)\b|[,.]|$)",
        instruction,
        flags=re.IGNORECASE,
    ):
        term = " ".join(preserve_match.group("term").split()).strip()
        if (
            term
            and not re.match(r"(?:the )?trip\s+(?:under|below|less than)\b", term.casefold())
            and not generic_preservation.search(term)
        ):
            preserve_terms.append(term)

    forbidden_terms: list[str] = []
    for forbidden_match in re.finditer(
        r"\b(?:don't want|do not want|no|replace)\s+(?P<term>[a-z][a-z ]{2,40}?)(?=\s+(?:with|by|and|on|because)\b|[,.]|$)",
        normalized,
    ):
        term = forbidden_match.group("term").strip()
        if term not in {"it", "them", "everything else"}:
            forbidden_terms.append(term)

    target_category = None
    if re.search(r"\b(food|dining|meals?|restaurants?|seafood|local food)\b", normalized):
        target_category = CostCategory.FOOD.value
    elif re.search(r"\b(activities|activity|attractions?)\b", normalized):
        target_category = CostCategory.ACTIVITIES.value
    explicit_preserve_all = bool(
        re.search(r"\bwithout changing anything else\b|\bdo not change anything else\b", normalized)
    )
    semantic_target = next(
        (
            value
            for value in (
                "seafood",
                "local food",
                "photography",
                "outdoor",
                "indoor",
                "romantic",
                "relaxing",
                "free time",
                "nature",
                "culture",
                "nightlife",
                "food",
            )
            if value in normalized
        ),
        None,
    )

    if re.search(r"\b(walking|physically demanding|mobility)\b", normalized):
        return RevisionIntent(
            kind=RevisionIntentKind.SEMANTIC,
            target_day=target_day,
            target_text="walking",
            situational_constraints=tuple(situational_constraints),
            preserve_unrelated_days=preserve_unrelated_days,
            explicit_preserve_terms=tuple(preserve_terms),
            forbidden_terms=tuple(dict.fromkeys(forbidden_terms)),
            budget_target_cents=budget_target_cents,
            explicit_preserve_all=explicit_preserve_all,
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
    if (
        cheaper is not None
        and any(marker in normalized for marker in semantic_markers)
        and target_category is None
        and budget_target_cents is None
    ):
        return RevisionIntent(
            kind=RevisionIntentKind.SEMANTIC,
            target_day=target_day,
            target_text=semantic_target,
            situational_constraints=tuple(situational_constraints),
            preserve_unrelated_days=preserve_unrelated_days,
            explicit_preserve_terms=tuple(preserve_terms),
            forbidden_terms=tuple(dict.fromkeys(forbidden_terms)),
            budget_target_cents=budget_target_cents,
            explicit_preserve_all=explicit_preserve_all,
        )
    if (
        day_match is not None
        and cheaper is not None
        and re.search(r"\bday\b", normalized)
        and re.search(r"\b(cheaper|reduce|lower|less expensive)\b", normalized)
    ):
        return RevisionIntent(
            kind=RevisionIntentKind.CHEAPER_DAY,
            target_day=target_day,
            situational_constraints=tuple(situational_constraints),
            preserve_unrelated_days=preserve_unrelated_days,
            explicit_preserve_terms=tuple(preserve_terms),
            forbidden_terms=tuple(dict.fromkeys(forbidden_terms)),
            budget_target_cents=budget_target_cents,
            explicit_preserve_all=explicit_preserve_all,
        )
    cost_signal = re.search(
        r"\b(cost|costs|cheaper|reduce|lower|less expensive|spend|price)\b", normalized
    )
    if cost_signal is not None or budget_target_cents is not None:
        return RevisionIntent(
            kind=RevisionIntentKind.COST_ONLY,
            target_day=target_day,
            target_text=target_category,
            situational_constraints=tuple(situational_constraints),
            preserve_unrelated_days=preserve_unrelated_days,
            explicit_preserve_terms=tuple(preserve_terms),
            forbidden_terms=tuple(dict.fromkeys(forbidden_terms)),
            budget_target_cents=budget_target_cents,
            explicit_preserve_all=explicit_preserve_all,
        )
    if re.search(r"\b(replace|don't want|do not want|remove)\b", normalized):
        return RevisionIntent(
            kind=RevisionIntentKind.REPLACE_ACTIVITY,
            target_day=target_day,
            situational_constraints=tuple(situational_constraints),
            preserve_unrelated_days=preserve_unrelated_days,
            target_text=target_category,
            replacement_text=normalized,
            explicit_preserve_terms=tuple(preserve_terms),
            forbidden_terms=tuple(dict.fromkeys(forbidden_terms)),
            budget_target_cents=budget_target_cents,
            explicit_preserve_all=explicit_preserve_all,
        )
    if "activities" in normalized and "interest" in normalized:
        return RevisionIntent(
            kind=RevisionIntentKind.SEMANTIC,
            target_day=target_day,
            target_text="interests",
            situational_constraints=tuple(situational_constraints),
            preserve_unrelated_days=preserve_unrelated_days,
            explicit_preserve_terms=tuple(preserve_terms),
            forbidden_terms=tuple(dict.fromkeys(forbidden_terms)),
            budget_target_cents=budget_target_cents,
            explicit_preserve_all=explicit_preserve_all,
        )
    return RevisionIntent(
        kind=RevisionIntentKind.SEMANTIC,
        target_day=target_day,
        target_text=semantic_target,
        situational_constraints=tuple(situational_constraints),
        preserve_unrelated_days=preserve_unrelated_days,
        explicit_preserve_terms=tuple(preserve_terms),
        forbidden_terms=tuple(dict.fromkeys(forbidden_terms)),
        budget_target_cents=budget_target_cents,
        explicit_preserve_all=explicit_preserve_all,
    )


def parse_day_reference(value: str) -> int:
    numeric_match = re.search(r"\d{1,2}", value)
    if numeric_match is not None:
        return int(numeric_match.group())
    ordinal = value.removeprefix("the ").removesuffix(" day")
    return ORDINAL_DAY_NUMBERS[ordinal]


def apply_revision_patch(
    patch: GeneratedRevisionPatch,
    current: RevisionCurrentDraft,
    intent: RevisionIntent,
) -> GeneratedRevisionDraft:
    """Merge a validated, targeted AI patch onto the authoritative current itinerary."""
    if patch.outcome != "APPLIED":
        if patch.changes:
            raise ItineraryProviderResponseError(
                "NO_CHANGE and UNSUPPORTED revisions must not contain changes"
            )
        return GeneratedRevisionDraft(
            cost_basis=patch.cost_basis,
            currency=patch.currency,
            items=current.items,
            estimated_total_cost_cents=current.estimated_total_cost_cents,
            destination_name=patch.destination_name,
            start_date=patch.start_date,
            end_date=patch.end_date,
            outcome=patch.outcome,
            outcome_reason=patch.outcome_reason,
            operation=(
                "COST_ONLY"
                if intent.kind in {RevisionIntentKind.COST_ONLY, RevisionIntentKind.CHEAPER_DAY}
                else "SEMANTIC"
            ),
            replacement_target=patch.replacement_target,
        )

    current_by_slot = {(item.day_number, item.position): item for item in current.items}
    seen_slots: set[tuple[int, int]] = set()
    merged = dict(current_by_slot)
    for change in patch.changes:
        slot = (change.day_number, change.position)
        if slot in seen_slots:
            raise ItineraryProviderResponseError(
                "A revision patch cannot change the same slot twice"
            )
        seen_slots.add(slot)
        previous = current_by_slot.get(slot)
        if previous is None:
            raise ItineraryProviderResponseError(
                "A revision patch may target only existing itinerary activities"
            )
        if intent.target_day is not None and change.day_number != intent.target_day:
            raise ItineraryProviderResponseError(
                "A day-specific revision must preserve all semantic metadata exactly; preserve all unrelated days"
            )

        supplied_fields = {
            "title": change.title,
            "estimated_cost_cents": change.estimated_cost_cents,
            "category": change.category,
            "tags": change.tags,
            "environment": change.environment,
            "mobility": change.mobility,
            "intensity": change.intensity,
        }
        if not any(value is not None for value in supplied_fields.values()):
            raise ItineraryProviderResponseError("Every revision patch entry must change a field")
        if intent.kind in {
            RevisionIntentKind.COST_ONLY,
            RevisionIntentKind.CHEAPER_DAY,
        } and not any(value is not None for value in supplied_fields.values()):
            raise ItineraryProviderResponseError("Every revision patch entry must change a field")
        if change.operation == "REPLACE" and any(
            value is None
            for value in (
                change.title,
                change.category,
                change.environment,
                change.mobility,
                change.intensity,
            )
        ):
            raise ItineraryProviderResponseError(
                "A replacement patch must include complete trusted semantic metadata; preserve all semantic metadata exactly unless replacement metadata is supplied"
            )

        merged[slot] = RevisionItineraryItem(
            day_number=previous.day_number,
            position=previous.position,
            title=change.title if change.title is not None else previous.title,
            estimated_cost_cents=(
                change.estimated_cost_cents
                if change.estimated_cost_cents is not None
                else previous.estimated_cost_cents
            ),
            category=change.category if change.category is not None else previous.category,
            tags=change.tags if change.tags is not None else previous.tags,
            environment=change.environment
            if change.environment is not None
            else previous.environment,
            mobility=change.mobility if change.mobility is not None else previous.mobility,
            intensity=change.intensity if change.intensity is not None else previous.intensity,
        )

    items = [merged[(item.day_number, item.position)] for item in current.items]
    return GeneratedRevisionDraft(
        cost_basis=patch.cost_basis,
        currency=patch.currency,
        items=items,
        estimated_total_cost_cents=sum(item.estimated_cost_cents for item in items),
        destination_name=patch.destination_name,
        start_date=patch.start_date,
        end_date=patch.end_date,
        outcome="APPLIED",
        outcome_reason=patch.outcome_reason,
        operation=(
            "COST_ONLY"
            if intent.kind in {RevisionIntentKind.COST_ONLY, RevisionIntentKind.CHEAPER_DAY}
            else "SEMANTIC"
        ),
        replacement_target=patch.replacement_target,
    )


def coerce_revision_patch(
    raw: Any,
    current: RevisionCurrentDraft,
) -> GeneratedRevisionPatch:
    """Accept old full-draft fixtures during the contract transition, but normalize them to a patch."""
    try:
        return GeneratedRevisionPatch.model_validate(raw)
    except ValidationError as patch_error:
        try:
            legacy = GeneratedRevisionDraft.model_validate(raw)
        except (AttributeError, TypeError, ValidationError) as error:
            raise patch_error from error
        if legacy.outcome != "APPLIED":
            if (
                [
                    (item.day_number, item.position, item.title, item.estimated_cost_cents)
                    for item in legacy.items
                ]
                != [
                    (item.day_number, item.position, item.title, item.estimated_cost_cents)
                    for item in current.items
                ]
                or [semantic_metadata_fingerprint(item) for item in legacy.items]
                != [semantic_metadata_fingerprint(item) for item in current.items]
                or legacy.estimated_total_cost_cents != current.estimated_total_cost_cents
            ):
                raise ItineraryProviderResponseError(
                    "No-change and unsupported revisions must preserve the current itinerary"
                )
            changes: list[RevisionPatchItem] = []
        else:
            current_by_slot = {(item.day_number, item.position): item for item in current.items}
            changes = []
            for item in legacy.items:
                previous = current_by_slot.get((item.day_number, item.position))
                if previous is None:
                    changes.append(
                        RevisionPatchItem(
                            day_number=item.day_number,
                            position=item.position,
                            operation="REPLACE",
                            title=item.title,
                            estimated_cost_cents=item.estimated_cost_cents,
                            category=item.category,
                            tags=item.tags,
                            environment=item.environment,
                            mobility=item.mobility,
                            intensity=item.intensity,
                        )
                    )
                    continue
                changed = (
                    item.title != previous.title
                    or item.estimated_cost_cents != previous.estimated_cost_cents
                    or semantic_metadata_fingerprint(item)
                    != semantic_metadata_fingerprint(previous)
                )
                if changed:
                    changes.append(
                        RevisionPatchItem(
                            day_number=item.day_number,
                            position=item.position,
                            operation="REPLACE" if item.title != previous.title else "UPDATE",
                            title=item.title if item.title != previous.title else None,
                            estimated_cost_cents=(
                                item.estimated_cost_cents
                                if item.estimated_cost_cents != previous.estimated_cost_cents
                                else None
                            ),
                            category=item.category
                            if item.title != previous.title or item.category != previous.category
                            else None,
                            tags=item.tags
                            if item.title != previous.title or item.tags != previous.tags
                            else None,
                            environment=item.environment
                            if item.title != previous.title
                            or item.environment != previous.environment
                            else None,
                            mobility=item.mobility
                            if item.title != previous.title or item.mobility != previous.mobility
                            else None,
                            intensity=item.intensity
                            if item.title != previous.title or item.intensity != previous.intensity
                            else None,
                        )
                    )
        return GeneratedRevisionPatch(
            cost_basis=legacy.cost_basis,
            currency=legacy.currency,
            outcome=legacy.outcome,
            outcome_reason=legacy.outcome_reason,
            changes=changes,
            destination_name=legacy.destination_name,
            start_date=legacy.start_date,
            end_date=legacy.end_date,
            replacement_target=legacy.replacement_target,
        )


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
    if intent.target_day is not None and not any(
        item.day_number == intent.target_day for item in current.items
    ):
        raise ItineraryRevisionUnsupportedError(
            "The requested revision day is not present in the itinerary"
        )
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
        if not current.items:
            raise ItineraryRevisionUnsupportedError("The current itinerary has no activities")
        if not trusted_source:
            raise ItineraryRevisionUnsupportedError(
                "Semantic and replacement revisions are not supported without trusted metadata "
                "(trusted semantic metadata is required); the request cannot be applied safely"
            )
        return


def has_metadata(item: RevisionItineraryItem) -> bool:
    return (
        item.category is not None and item.environment != "UNKNOWN" and item.mobility != "UNKNOWN"
    )


def has_valid_metadata(item: RevisionItineraryItem) -> bool:
    if not has_metadata(item):
        return False
    try:
        validate_metadata_values(
            item.category or "",
            item.tags,
            item.environment,
            item.mobility,
            item.intensity,
        )
    except ItineraryProviderResponseError:
        return False
    return True


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
    for item, previous in zip(revised.items, current.items, strict=True):
        if semantic_item_fingerprint(item) != semantic_item_fingerprint(previous):
            if item.category is not None:
                validate_metadata_values(
                    item.category,
                    item.tags,
                    item.environment,
                    item.mobility,
                    item.intensity,
                )
            if not has_valid_metadata(item):
                raise ItineraryProviderResponseError(
                    "Changed semantic items must include trusted semantic metadata"
                )
    if not any(
        semantic_item_fingerprint(item) != semantic_item_fingerprint(previous)
        for item, previous in zip(revised.items, current.items, strict=False)
    ) and len(revised.items) == len(current.items):
        raise ItineraryProviderResponseError("A semantic revision must change itinerary content")


def _item_text(item: RevisionItineraryItem) -> str:
    return " ".join([item.title, *item.tags]).casefold()


def _matching_items(
    items: list[RevisionItineraryItem], terms: tuple[str, ...]
) -> list[RevisionItineraryItem]:
    return [item for item in items if any(term.casefold() in _item_text(item) for term in terms)]


def _goal_count(items: list[RevisionItineraryItem], terms: tuple[str, ...]) -> int:
    count = 0
    for item in items:
        text = _item_text(item)
        if any(term.casefold() in text for term in terms):
            count += 1
            continue
        if ("outdoor" in terms and item.environment == "OUTDOOR") or (
            "indoor" in terms and item.environment == "INDOOR"
        ):
            count += 1
    return count


def validate_explicit_constraints(
    revised: GeneratedRevisionDraft,
    current: RevisionCurrentDraft,
    intent: RevisionIntent,
) -> None:
    if intent.explicit_preserve_all:
        require_unchanged_items(
            current.items,
            revised.items,
            "The request explicitly required unrelated itinerary content to remain unchanged",
        )
    for term in intent.explicit_preserve_terms:
        previous = _matching_items(current.items, (term,))
        if not previous:
            raise ItineraryProviderResponseError(
                f"The requested item to preserve was not found: {term}"
            )
        if not any(
            semantic_item_fingerprint(item) == semantic_item_fingerprint(previous_item)
            for item in revised.items
            for previous_item in previous
        ):
            raise ItineraryProviderResponseError(
                f"The explicitly preserved item was changed or removed: {term}"
            )
    for term in intent.forbidden_terms:
        if _matching_items(revised.items, (term,)):
            raise ItineraryProviderResponseError(
                f"The revision left a prohibited itinerary item in place: {term}"
            )


def validate_intent_goal(
    revised: GeneratedRevisionDraft,
    current: RevisionCurrentDraft,
    intent: RevisionIntent,
) -> None:
    instruction = " ".join([intent.target_text or "", intent.replacement_text or ""]).casefold()
    scope_current = [
        item
        for item in current.items
        if intent.target_day is None or item.day_number == intent.target_day
    ]
    scope_revised = [
        item
        for item in revised.items
        if intent.target_day is None or item.day_number == intent.target_day
    ]
    if (
        intent.budget_target_cents is not None
        and revised.estimated_total_cost_cents > intent.budget_target_cents
    ):
        raise ItineraryProviderResponseError(
            "The revised itinerary does not meet the requested budget"
        )

    goal_terms: tuple[str, ...] | None = None
    if "seafood" in instruction:
        goal_terms = ("seafood", "fish", "oyster", "lobster", "sushi")
    elif "local food" in instruction or "local cuisine" in instruction:
        goal_terms = ("local", "food", "cuisine", "market")
    elif "photography" in instruction or "photograph" in instruction:
        goal_terms = ("photography", "photograph", "photo", "scenic", "viewpoint")
    elif "outdoor" in instruction or "outdoors" in instruction:
        goal_terms = ("outdoor", "nature", "park", "beach", "trail")
    elif "indoor" in instruction:
        goal_terms = ("indoor", "museum", "gallery", "aquarium", "theater")
    elif "romantic" in instruction:
        goal_terms = ("romantic", "couple", "sunset", "candle", "date")
    elif "free time" in instruction or "downtime" in instruction or "leisure" in instruction:
        goal_terms = (
            "free time",
            "free_time",
            "leisure",
            "rest",
            "break",
            "downtime",
            "unstructured",
        )
    elif ("relax" in instruction or "quiet" in instruction) and not any(
        item.intensity == "LOW" for item in scope_revised
    ):
        raise ItineraryProviderResponseError(
            "The revised itinerary does not provide a relaxing pace"
        )

    if goal_terms is not None:
        before = _goal_count(scope_current, goal_terms)
        after = _goal_count(scope_revised, goal_terms)
        if "free time" in instruction and after <= before:
            changed_relaxed_item = any(
                semantic_item_fingerprint(item) != semantic_item_fingerprint(previous)
                and item.intensity == "LOW"
                and item.mobility in {"LOW", "UNKNOWN"}
                for item, previous in zip(scope_revised, scope_current, strict=True)
            )
            if changed_relaxed_item:
                return
        if after <= before:
            raise ItineraryProviderResponseError(
                "The revised itinerary does not satisfy the requested focus"
            )


def validate_cost_only_revision(
    revised: GeneratedRevisionDraft,
    current: RevisionCurrentDraft,
    *,
    target_day: int | None,
    target_category: str | None,
    budget_target_cents: int | None = None,
) -> None:
    for item, previous in zip(revised.items, current.items, strict=True):
        if semantic_metadata_fingerprint(item) != semantic_metadata_fingerprint(previous):
            if item.category is not None:
                validate_metadata_values(
                    item.category,
                    item.tags,
                    item.environment,
                    item.mobility,
                    item.intensity,
                )
            if not has_valid_metadata(item):
                raise ItineraryProviderResponseError(
                    "Changed semantic items must include trusted semantic metadata"
                )
    current_total = current.estimated_total_cost_cents
    revised_total = revised.estimated_total_cost_cents
    if (
        target_category is None
        and target_day is None
        and budget_target_cents is None
        and revised_total >= current_total
    ):
        raise ItineraryProviderResponseError(
            "A cost-only revision must strictly reduce the requested cost"
        )
    if target_category is not None:
        current_category_total = sum(
            item.estimated_cost_cents for item in current.items if item.category == target_category
        )
        revised_category_total = sum(
            item.estimated_cost_cents for item in revised.items if item.category == target_category
        )
        if revised_category_total >= current_category_total:
            raise ItineraryProviderResponseError(
                f"The requested {target_category} cost must be lower after the revision"
            )
    if target_day is not None:
        current_day_total = sum(
            item.estimated_cost_cents for item in current.items if item.day_number == target_day
        )
        revised_day_total = sum(
            item.estimated_cost_cents for item in revised.items if item.day_number == target_day
        )
        if revised_day_total >= current_day_total:
            raise ItineraryProviderResponseError(
                "A cheaper-day revision must strictly reduce that day's estimated cost"
            )


def validate_cost_only_metadata(
    revised: GeneratedRevisionDraft,
    current: RevisionCurrentDraft,
) -> None:
    del revised, current


def preserve_current_metadata_for_cost_only_revision(
    revised: GeneratedRevisionDraft,
    current: RevisionCurrentDraft,
) -> GeneratedRevisionDraft:
    """Keep persisted, metadata-free itineraries metadata-free after a cost revision."""
    if len(revised.items) != len(current.items):
        return revised
    if [(item.day_number, item.position) for item in revised.items] != [
        (item.day_number, item.position) for item in current.items
    ]:
        return revised
    if any(has_metadata(item) for item in current.items):
        return revised

    return revised.model_copy(
        update={
            "items": [
                item.model_copy(
                    update={
                        "category": previous.category,
                        "tags": previous.tags,
                        "environment": previous.environment,
                        "mobility": previous.mobility,
                        "intensity": previous.intensity,
                    }
                )
                for item, previous in zip(revised.items, current.items, strict=True)
            ]
        }
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

    draft_data = {
        "items": [
            ItineraryItemInput(
                day_number=item.day_number,
                position=item.position,
                title=canonical_title(item),
                estimated_cost=cents_to_float(item.estimated_cost_cents),
                metadata=metadata_from_revision_item(item),
            )
            for item in draft.items
        ],
        "estimated_total_cost": cents_to_float(draft.estimated_total_cost_cents),
    }
    if is_issued_trusted_draft(current):
        return issue_trusted_draft(**draft_data)
    return ItineraryDraft(**draft_data)


def semantic_metadata_fingerprint(item: RevisionItineraryItem) -> tuple[Any, ...]:
    return (
        item.category,
        tuple(item.tags),
        item.environment,
        item.mobility,
        item.intensity,
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
        semantic_metadata_fingerprint(item),
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

    if intent.kind in {RevisionIntentKind.CHEAPER_DAY, RevisionIntentKind.COST_ONLY}:
        validate_cost_only_revision(
            revised,
            current,
            target_day=intent.target_day,
            target_category=intent.target_text,
            budget_target_cents=intent.budget_target_cents,
        )
    else:
        validate_semantic_revision(revised, current)
    if intent.target_day is not None:
        require_unchanged_items(
            [item for item in current.items if item.day_number != intent.target_day],
            [item for item in revised.items if item.day_number != intent.target_day],
            "A day-specific revision must preserve all semantic metadata exactly; preserve all unrelated days",
        )
    validate_explicit_constraints(revised, current, intent)
    validate_intent_goal(revised, current, intent)
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
    if tuple(semantic_item_fingerprint(item) for item in current) != tuple(
        semantic_item_fingerprint(item) for item in revised
    ):
        raise ItineraryProviderResponseError(message)
