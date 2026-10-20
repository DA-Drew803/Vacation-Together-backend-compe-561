import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from openai import OpenAIError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.schemas.itinerary import Budget, EligibleMember, ItineraryDraft, TripContext
from app.services import itinerary as itinerary_service
from app.services import openai_itinerary as provider_module
from app.services.openai_itinerary import (
    DEFAULT_OPENAI_MODEL,
    MAX_INTERESTS,
    OPENAI_MAX_RETRIES,
    ItineraryProviderConfigurationError,
    ItineraryProviderError,
    ItineraryProviderInputError,
    ItineraryProviderResponseError,
    ItineraryProviderTimeoutError,
    OpenAIItineraryProvider,
)


class FakeResponses:
    def __init__(self, output: Any = None, error: Exception | None = None) -> None:
        self.output = output
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(output_parsed=self.output)


class FakeOpenAIClient:
    def __init__(self, output: Any = None, error: Exception | None = None) -> None:
        self.responses = FakeResponses(output, error)


def context() -> TripContext:
    return TripContext(
        trip_id="a799c3a9-0a0d-4af3-a083-5ea4bb1d7cc9",
        selected_destination_id="f042cc31-a9f1-44c9-ab64-f35791de2833",
        destination_name="San Diego",
        start_date="2027-06-10",
        end_date="2027-06-12",
        budget=Budget(max_amount=300, currency="USD"),
        eligible_members=[
            EligibleMember(member_id="member-1", display_name="Member One"),
            EligibleMember(member_id="member-2", display_name="Member Two"),
        ],
        interests=["food", "museums"],
    )


def valid_output() -> dict[str, Any]:
    return {
        "cost_basis": "PER_PERSON",
        "destination_name": "San Diego",
        "start_date": "2027-06-10",
        "end_date": "2027-06-12",
        "currency": "USD",
        "items": [
            {
                "day_number": 1,
                "position": 1,
                "category": "flights",
                "title": "Round-trip airfare estimate",
                "estimated_cost_cents": 5_000,
            },
            {
                "day_number": 1,
                "position": 2,
                "category": "lodging",
                "title": "Two nights of lodging",
                "estimated_cost_cents": 10_000,
            },
            {
                "day_number": 2,
                "position": 1,
                "category": "food",
                "title": "Daily meals",
                "estimated_cost_cents": 3_000,
            },
            {
                "day_number": 2,
                "position": 2,
                "category": "local_transport",
                "title": "Transit pass",
                "estimated_cost_cents": 1_500,
            },
            {
                "day_number": 3,
                "position": 1,
                "category": "activities",
                "title": "Museum admission",
                "estimated_cost_cents": 4_500,
            },
        ],
        "estimated_total_cost_cents": 24_000,
    }


def provider_with(output: Any = None, error: Exception | None = None):
    client = FakeOpenAIClient(output, error)
    provider = OpenAIItineraryProvider(client=client, model="test-model", timeout_seconds=12)
    return provider, client


def test_successful_generation_covers_budget_categories_and_preserves_privacy() -> None:
    provider, client = provider_with(valid_output())
    private_context = context().model_copy(
        update={
            "interests": [
                "  Food   tours  ",
                "FOOD TOURS",
                "Museums\x00",
                *[f"interest {index}" for index in range(30)],
            ]
        }
    )

    draft = provider.generate(private_context)

    assert isinstance(draft, ItineraryDraft)
    assert [item.day_number for item in draft.items] == [1, 1, 2, 2, 3]
    assert [item.position for item in draft.items] == [1, 2, 1, 2, 1]
    assert [item.title for item in draft.items] == [
        "Round-trip airfare estimate",
        "Two nights of lodging",
        "Daily meals",
        "Transit pass",
        "Museum admission",
    ]
    assert draft.estimated_total_cost == 240.0
    assert sum(Decimal(str(item.estimated_cost)) for item in draft.items) == Decimal("240.0")

    call = client.responses.calls[0]
    assert call["model"] == "test-model"
    assert call["timeout"] == 12
    assert call["store"] is False
    assert call["text_format"].__name__ == "GeneratedItineraryDraft"
    request_context = json.loads(call["input"][1]["content"])
    assert request_context["selected_destination"] == "San Diego"
    assert request_context["travel_dates"] == {
        "start_date": "2027-06-10",
        "end_date": "2027-06-12",
        "number_of_days": 3,
    }
    assert request_context["limiting_per_person_budget"] == {
        "amount_cents": 30_000,
        "currency": "USD",
    }
    assert request_context["interests"][:2] == ["Food tours", "Museums"]
    assert len(request_context["interests"]) == MAX_INTERESTS
    serialized_request = call["input"][1]["content"]
    assert private_context.trip_id not in serialized_request
    assert private_context.selected_destination_id not in serialized_request
    assert "member-1" not in serialized_request
    assert "Member One" not in serialized_request


@pytest.mark.parametrize(
    ("output", "message"),
    [
        ({"items": []}, "incomplete or invalid"),
        (
            {
                **valid_output(),
                "destination_name": "A different destination",
            },
            "incomplete or invalid",
        ),
        (
            {
                **valid_output(),
                "items": [
                    *valid_output()["items"][:-1],
                    {
                        **valid_output()["items"][-1],
                        "category": "food",
                    },
                ],
            },
            "must account for flights, lodging, food, local transport, and activities",
        ),
        (
            {
                **valid_output(),
                "estimated_total_cost_cents": 24_001,
            },
            "equal the sum",
        ),
        (
            {
                **valid_output(),
                "items": [
                    {**item, "estimated_cost_cents": 7_000} for item in valid_output()["items"]
                ],
                "estimated_total_cost_cents": 35_000,
            },
            "exceeds the limiting per-person budget",
        ),
        (
            {
                **valid_output(),
                "items": [
                    {**item, "day_number": min(item["day_number"], 2)}
                    for item in valid_output()["items"]
                ],
            },
            "every travel day",
        ),
        (
            {
                **valid_output(),
                "items": [
                    *valid_output()["items"][:1],
                    {**valid_output()["items"][1], "position": 3},
                    *valid_output()["items"][2:],
                ],
            },
            "positions must be sequential",
        ),
        (
            {
                **valid_output(),
                "items": [
                    {**valid_output()["items"][0], "estimated_cost_cents": 10.5},
                    *valid_output()["items"][1:],
                ],
            },
            "incomplete or invalid",
        ),
    ],
)
def test_invalid_or_unsafe_output_is_rejected(output: Any, message: str) -> None:
    provider, _ = provider_with(output)

    with pytest.raises(ItineraryProviderResponseError, match=message):
        provider.generate(context())


def test_budget_requires_two_decimal_precision_before_provider_call() -> None:
    provider, client = provider_with(valid_output())
    invalid_context = context().model_copy(
        update={"budget": Budget(max_amount=300.001, currency="USD")}
    )

    with pytest.raises(ItineraryProviderInputError, match="two decimal places"):
        provider.generate(invalid_context)

    assert client.responses.calls == []


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"destination_name": "x" * 121}, "Destination name"),
        ({"eligible_members": []}, "Eligible group size"),
    ],
)
def test_other_model_inputs_are_bounded_before_provider_call(
    updates: dict[str, Any],
    message: str,
) -> None:
    provider, client = provider_with(valid_output())

    with pytest.raises(ItineraryProviderInputError, match=message):
        provider.generate(context().model_copy(update=updates))

    assert client.responses.calls == []


def test_missing_api_key_is_rejected_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)

    with pytest.raises(ItineraryProviderConfigurationError, match="OPENAI_API_KEY"):
        OpenAIItineraryProvider.from_env()


def test_client_configuration_disables_retries_and_bounds_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_openai(**kwargs: Any) -> FakeOpenAIClient:
        captured.update(kwargs)
        return FakeOpenAIClient(valid_output())

    monkeypatch.setattr(provider_module, "OpenAI", fake_openai)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "15")

    provider = OpenAIItineraryProvider.from_env()

    assert provider.model == "test-model"
    assert captured == {
        "api_key": "test-only-key",
        "max_retries": OPENAI_MAX_RETRIES,
        "timeout": 15.0,
    }
    assert OPENAI_MAX_RETRIES == 0


@pytest.mark.parametrize("timeout", ["0", "61", "not-a-number"])
def test_invalid_timeout_configuration_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    timeout: str,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-key")
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", timeout)

    with pytest.raises(ItineraryProviderConfigurationError, match="OPENAI_TIMEOUT_SECONDS"):
        OpenAIItineraryProvider.from_env()


def test_timeout_is_reported_without_exposing_provider_details() -> None:
    provider, _ = provider_with(error=TimeoutError("socket details"))

    with pytest.raises(ItineraryProviderTimeoutError, match="timed out") as raised:
        provider.generate(context())

    assert "socket details" not in str(raised.value)


def test_provider_failure_is_wrapped_without_exposing_details() -> None:
    provider, _ = provider_with(error=OpenAIError("sensitive upstream details"))

    with pytest.raises(ItineraryProviderError, match="generation failed") as raised:
        provider.generate(context())

    assert "sensitive upstream details" not in str(raised.value)


def test_provider_amounts_persist_with_exact_numeric_sum() -> None:
    provider, _ = provider_with(valid_output())
    draft = provider.generate(context())
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(bind=engine, class_=Session)
    Base.metadata.create_all(bind=engine)

    try:
        with session_factory() as db:
            itinerary = itinerary_service.generate_itinerary_for_selected_destination(
                db,
                context(),
                draft,
            )
            persisted_item_total = sum(
                (item.estimated_cost for item in itinerary.items),
                start=Decimal("0.00"),
            )
            assert itinerary.estimated_total_cost == Decimal("240.00")
            assert persisted_item_total == itinerary.estimated_total_cost
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_existing_itinerary_short_circuits_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = object()
    monkeypatch.setattr(itinerary_service, "get_itinerary", lambda db, trip_id: existing)

    class NeverCalledProvider:
        def generate(self, trip_context: TripContext) -> ItineraryDraft:
            del trip_context
            raise AssertionError("provider must not run for an existing itinerary")

    result = itinerary_service.generate_and_persist_initial_itinerary(
        object(),  # type: ignore[arg-type]
        context(),
        NeverCalledProvider(),
    )

    assert result is existing
