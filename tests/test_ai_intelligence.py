import json
import runpy
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.itinerary import (
    Budget,
    EligibleMember,
    FoodPreferences,
    ItineraryDraft,
    ItineraryItemInput,
    TripContext,
    WeatherDay,
)
from app.services import openai_itinerary as provider_module
from app.services.openai_itinerary import (
    ItineraryProviderInputError,
    ItineraryProviderResponseError,
    ItineraryRevisionUnsupportedError,
    OpenAIItineraryProvider,
)


class FakeResponses:
    def __init__(self, outputs: list[Any]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self.outputs.pop(0))


class FakeClient:
    def __init__(self, *outputs: Any) -> None:
        self.responses = FakeResponses(list(outputs))


def rich_context(*, weather: list[WeatherDay] | None = None) -> TripContext:
    return TripContext(
        trip_id="trip-1",
        selected_destination_id="destination-1",
        destination_name="San Francisco",
        start_date="2027-06-10",
        end_date="2027-06-12",
        budget=Budget(max_amount=1_400, currency="USD"),
        eligible_members=[
            EligibleMember(member_id="member-1", display_name="One"),
            EligibleMember(member_id="member-2", display_name="Two"),
        ],
        interests=["food", "museums", "nature"],
        food_preferences=FoodPreferences(
            cuisines=["Japanese"], dining_style="casual", dietary_preferences=[]
        ),
        travel_preferences=["local experiences"],
        pace="balanced",
        mobility_preference="low walking",
        weather=weather,
    )


def rich_output() -> dict[str, Any]:
    items = [
        (1, 1, "Flight estimate", "flights", 30_000, ["travel"]),
        (1, 2, "Hotel estimate", "lodging", 30_000, ["lodging"]),
        (2, 1, "Japanese lunch", "food", 8_000, ["food", "japanese"]),
        (2, 2, "Exploratorium", "activities", 12_000, ["museum", "science"]),
        (3, 1, "Golden Gate Park", "activities", 10_000, ["nature", "park"]),
        (3, 2, "Local transit", "local_transport", 2_000, ["transit"]),
    ]
    return {
        "cost_basis": "GROUP_PLANNING_CEILING",
        "destination_name": "San Francisco",
        "start_date": "2027-06-10",
        "end_date": "2027-06-12",
        "currency": "USD",
        "items": [
            {
                "day_number": day,
                "position": position,
                "title": title,
                "category": category,
                "estimated_cost_cents": cost,
                "tags": tags,
                "environment": "OUTDOOR" if "Park" in title else "INDOOR",
                "mobility": "LOW",
                "intensity": "LOW",
            }
            for day, position, title, category, cost, tags in items
        ],
        "estimated_total_cost_cents": sum(item[4] for item in items),
    }


def provider_with(*outputs: Any) -> tuple[OpenAIItineraryProvider, FakeClient]:
    client = FakeClient(*outputs)
    return OpenAIItineraryProvider(client=client, model="test-model"), client


def test_generation_sends_preferences_food_mobility_and_weather() -> None:
    weather = [
        WeatherDay(
            date="2027-06-11",
            condition="rain",
            temperature_high=62,
            temperature_low=51,
            precipitation_probability=80,
        )
    ]
    provider, client = provider_with(rich_output())

    draft = provider.generate(rich_context(weather=weather))

    payload = json.loads(client.responses.calls[0]["input"][1]["content"])
    assert payload["food_preferences"]["cuisines"] == ["Japanese"]
    assert payload["pace"] == "balanced"
    assert payload["mobility_preference"] == "low walking"
    assert payload["weather"][0]["condition"] == "rain"
    assert all(item.metadata is not None for item in draft.items)


def test_generation_without_weather_explicitly_says_weather_is_unavailable() -> None:
    provider, client = provider_with(rich_output())

    provider.generate(rich_context(weather=None))

    payload = json.loads(client.responses.calls[0]["input"][1]["content"])
    assert payload["weather"] is None
    assert "Do not assume" in payload["weather_note"]


def test_semantic_revision_uses_trusted_metadata_and_can_change_content() -> None:
    generated = rich_output()
    revised = rich_output()
    revised["items"][3]["title"] = "California Academy of Sciences"
    revised["items"][3]["tags"] = ["museum", "science", "nature"]
    revised["items"][3]["estimated_cost_cents"] -= 2_000
    revised["estimated_total_cost_cents"] -= 2_000
    revised["outcome"] = "APPLIED"
    revised["operation"] = "SEMANTIC"
    revised["outcome_reason"] = "Moved the activity indoors for rain."
    provider, client = provider_with(generated, revised)

    current = provider.generate(rich_context(weather=[]))
    result = provider.revise(
        rich_context(weather=[]),
        current,
        1,
        "Make Day 2 cheaper and better for the rain",
    )

    assert result.items[3].title == "California Academy of Sciences"
    assert len(client.responses.calls) == 2


def test_whole_trip_cost_revision_preserves_content_and_reduces_total() -> None:
    generated = rich_output()
    revised = rich_output()
    revised["items"][2]["estimated_cost_cents"] -= 1_000
    revised["estimated_total_cost_cents"] -= 1_000
    revised["outcome"] = "APPLIED"
    revised["outcome_reason"] = "Reduced food spending."
    revised["operation"] = "COST_ONLY"
    provider, _ = provider_with(generated, revised)

    current = provider.generate(rich_context())
    result = provider.revise(rich_context(), current, 1, "Make the whole trip cheaper")

    assert result.estimated_total_cost == 91_000 / 100
    assert [item.title for item in result.items] == [item.title for item in current.items]


def test_semantic_revision_is_rejected_after_metadata_free_round_trip() -> None:
    provider, client = provider_with(rich_output())
    metadata_free = ItineraryDraft(
        items=[
            ItineraryItemInput(day_number=1, position=1, title="Museum", estimated_cost=20),
            ItineraryItemInput(day_number=1, position=2, title="Lodging", estimated_cost=20),
            ItineraryItemInput(day_number=2, position=1, title="Food", estimated_cost=10),
            ItineraryItemInput(day_number=2, position=2, title="Activity", estimated_cost=10),
            ItineraryItemInput(day_number=3, position=1, title="Nature", estimated_cost=10),
        ],
        estimated_total_cost=70,
    )

    with pytest.raises(ItineraryRevisionUnsupportedError, match="trusted"):
        provider.revise(rich_context(), metadata_free, 1, "Give us more nature")

    assert client.responses.calls == []


def test_activity_replacement_with_rich_metadata_cannot_change_lodging() -> None:
    generated = rich_output()
    revised = rich_output()
    revised["items"][3]["title"] = "Japanese Tea Garden"
    revised["items"][3]["tags"] = ["nature", "garden"]
    revised["outcome"] = "APPLIED"
    revised["operation"] = "SEMANTIC"
    revised["outcome_reason"] = "Replaced the activity safely."
    revised["replacement_target"] = {
        "day_number": 2,
        "position": 2,
        "title": "Exploratorium",
        "requested_activity": "park",
    }
    provider, _ = provider_with(generated, revised)
    current = provider.generate(rich_context())

    result = provider.revise(rich_context(), current, 1, "Replace the museum with a park")

    assert result.items[3].title == "Japanese Tea Garden"

    unsafe = rich_output()
    unsafe["items"][3]["title"] = "Japanese Tea Garden"
    unsafe["items"][1]["title"] = "Different hotel"
    unsafe["outcome"] = "APPLIED"
    unsafe["operation"] = "SEMANTIC"
    unsafe["outcome_reason"] = "Unsafe change."
    unsafe["replacement_target"] = {
        "day_number": 2,
        "position": 2,
        "title": "Exploratorium",
        "requested_activity": "park",
    }
    provider, _ = provider_with(rich_output(), unsafe)
    current = provider.generate(rich_context())

    with pytest.raises(ItineraryProviderResponseError, match="outside the requested target"):
        provider.revise(rich_context(), current, 1, "Replace the museum with a park")


def test_invalid_semantic_metadata_is_rejected() -> None:
    output = rich_output()
    output["items"][0]["environment"] = "TELEPORT"
    provider, _ = provider_with(output)

    with pytest.raises(ItineraryProviderResponseError, match="environment"):
        provider.generate(rich_context())


def test_invalid_revision_category_metadata_is_rejected() -> None:
    generated = rich_output()
    revised = rich_output()
    revised["items"][3]["title"] = "Different activity"
    revised["items"][3]["category"] = "not-a-category"
    revised["outcome"] = "APPLIED"
    revised["operation"] = "SEMANTIC"
    revised["outcome_reason"] = "Invalid category metadata."
    provider, _ = provider_with(generated, revised)
    current = provider.generate(rich_context())

    with pytest.raises(ItineraryProviderResponseError, match="category"):
        provider.revise(rich_context(), current, 1, "Give us more nature")


def test_metadata_from_an_arbitrary_canonical_draft_does_not_authorize_semantic_revision() -> None:
    generated_provider, _ = provider_with(rich_output())
    generated = generated_provider.generate(rich_context())
    caller_draft = ItineraryDraft.model_validate(generated.model_dump())
    revision_provider, client = provider_with(rich_output())

    with pytest.raises(ItineraryRevisionUnsupportedError, match="provider-generated trusted draft"):
        revision_provider.revise(rich_context(), caller_draft, 1, "Give us more nature")

    assert client.responses.calls == []


def test_direct_trusted_draft_construction_cannot_obtain_provenance() -> None:
    trusted_type = provider_module.RichItineraryDraft

    with pytest.raises(TypeError, match="issued by the provider"):
        trusted_type(
            items=[
                ItineraryItemInput(
                    day_number=1,
                    position=1,
                    title="Museum",
                    estimated_cost=10,
                )
            ],
            estimated_total_cost=10,
        )

    generation_provider, _ = provider_with(rich_output())
    legitimate = generation_provider.generate(rich_context())
    forged = trusted_type.model_construct(
        items=legitimate.items,
        estimated_total_cost=legitimate.estimated_total_cost,
    )
    revision_provider, client = provider_with(rich_output())

    with pytest.raises(ItineraryRevisionUnsupportedError, match="provider-generated trusted draft"):
        revision_provider.revise(rich_context(), forged, 1, "Give us more nature")

    assert client.responses.calls == []


@pytest.mark.parametrize(
    "weather",
    [
        [
            WeatherDay(
                date="2027-06-11",
                condition="rain",
            ),
            WeatherDay(
                date="2027-06-11",
                condition="clear",
            ),
        ],
    ],
)
def test_duplicate_weather_dates_are_rejected(weather: list[WeatherDay]) -> None:
    with pytest.raises(ValidationError, match="unique"):
        rich_context(weather=weather)


def test_weather_outside_trip_range_is_rejected_before_provider_call() -> None:
    provider, client = provider_with(rich_output())
    context = rich_context(weather=[WeatherDay(date="2027-06-13", condition="clear")])

    with pytest.raises(ItineraryProviderInputError, match="within the trip date range"):
        provider.generate(context)

    assert client.responses.calls == []


@pytest.mark.parametrize(
    "weather_data",
    [
        {"date": "not-a-date", "condition": "clear"},
        {"date": "2027-06-11", "condition": "   "},
        {
            "date": "2027-06-11",
            "condition": "clear",
            "temperature_low": 70,
            "temperature_high": 60,
        },
    ],
)
def test_malformed_weather_is_rejected_by_the_context_schema(weather_data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        rich_context(weather=[WeatherDay.model_validate(weather_data)])


def test_generation_rejects_destination_change_deterministically() -> None:
    output = rich_output()
    output["destination_name"] = "Paris"
    provider, _ = provider_with(output)

    with pytest.raises(ItineraryProviderResponseError, match="destination cannot change"):
        provider.generate(rich_context())


def test_generation_rejects_one_cent_over_the_authoritative_ceiling() -> None:
    provider, client = provider_with(rich_output())
    context = rich_context().model_copy(update={"budget": Budget(max_amount=919, currency="USD")})

    with pytest.raises(ItineraryProviderResponseError, match="budget ceiling"):
        provider.generate(context)

    assert client.responses.calls


def test_generation_rejects_a_fake_low_reported_total() -> None:
    output = rich_output()
    output["items"][0]["estimated_cost_cents"] += 10_000
    provider, _ = provider_with(output)

    with pytest.raises(ItineraryProviderResponseError, match="equal the sum"):
        provider.generate(rich_context())


def test_generation_rejects_negative_item_cost() -> None:
    output = rich_output()
    output["items"][0]["estimated_cost_cents"] = -1
    provider, _ = provider_with(output)

    with pytest.raises(ItineraryProviderResponseError, match="incomplete or invalid"):
        provider.generate(rich_context())


def test_generation_rejects_currency_mismatch() -> None:
    output = rich_output()
    output["currency"] = "EUR"
    provider, _ = provider_with(output)

    with pytest.raises(ItineraryProviderResponseError, match="currency"):
        provider.generate(rich_context())


def test_revision_rejects_semantic_result_over_the_same_ceiling() -> None:
    generated = rich_output()
    revised = rich_output()
    revised["items"][0]["estimated_cost_cents"] += 60_000
    revised["estimated_total_cost_cents"] += 60_000
    revised["outcome"] = "APPLIED"
    revised["operation"] = "SEMANTIC"
    revised["outcome_reason"] = "Invalid over-budget revision."
    provider, _ = provider_with(generated, revised)
    current = provider.generate(rich_context())

    with pytest.raises(ItineraryProviderResponseError, match="budget ceiling"):
        provider.revise(rich_context(), current, 1, "Give us more nature")


def test_revision_rejects_date_change_deterministically() -> None:
    generated = rich_output()
    revised = rich_output()
    revised["start_date"] = "2027-06-11"
    revised["outcome"] = "APPLIED"
    revised["operation"] = "SEMANTIC"
    revised["outcome_reason"] = "Invalid date change."
    provider, _ = provider_with(generated, revised)
    current = provider.generate(rich_context())

    with pytest.raises(ItineraryProviderResponseError, match="start date cannot change"):
        provider.revise(rich_context(), current, 1, "Make the schedule more relaxing")


def test_activity_replacement_rejects_a_different_activity_target() -> None:
    generated = rich_output()
    revised = rich_output()
    revised["items"][3]["title"] = "Aquarium"
    revised["items"][4]["title"] = "New activity"
    revised["outcome"] = "APPLIED"
    revised["operation"] = "SEMANTIC"
    revised["outcome_reason"] = "Changed the wrong activity."
    revised["replacement_target"] = {
        "day_number": 2,
        "position": 2,
        "title": "Exploratorium",
        "requested_activity": "aquarium",
    }
    provider, _ = provider_with(generated, revised)
    current = provider.generate(rich_context())

    with pytest.raises(ItineraryProviderResponseError, match="outside the requested target"):
        provider.revise(rich_context(), current, 1, "Replace the museum with an aquarium")


def test_activity_replacement_rejects_a_meal_change() -> None:
    generated = rich_output()
    revised = rich_output()
    revised["items"][2]["title"] = "Aquarium cafe"
    revised["items"][3]["title"] = "Aquarium"
    revised["outcome"] = "APPLIED"
    revised["operation"] = "SEMANTIC"
    revised["outcome_reason"] = "Changed a meal instead."
    revised["replacement_target"] = {
        "day_number": 2,
        "position": 2,
        "title": "Exploratorium",
        "requested_activity": "aquarium",
    }
    provider, _ = provider_with(generated, revised)
    current = provider.generate(rich_context())

    with pytest.raises(ItineraryProviderResponseError, match="outside the requested target"):
        provider.revise(rich_context(), current, 1, "Replace the museum with an aquarium")


def test_sequential_successful_revisions_use_the_latest_draft() -> None:
    generated = rich_output()
    first = rich_output()
    first["items"][3]["title"] = "California Academy of Sciences"
    first["outcome"] = "APPLIED"
    first["operation"] = "SEMANTIC"
    first["outcome_reason"] = "First revision."
    second = first.copy()
    second["items"] = [dict(item) for item in first["items"]]
    second["items"][4]["title"] = "Presidio nature walk"
    second["outcome"] = "APPLIED"
    second["operation"] = "SEMANTIC"
    second["outcome_reason"] = "Second revision."
    provider, client = provider_with(generated, first, second)

    current = provider.generate(rich_context())
    current = provider.revise(rich_context(), current, 1, "Make Day 2 more suitable for rain")
    current = provider.revise(rich_context(), current, 1, "Give us more nature")

    second_payload = json.loads(client.responses.calls[2]["input"][1]["content"])
    assert second_payload["current_itinerary"]["items"][3]["title"] == (
        "California Academy of Sciences"
    )
    assert current.items[4].title == "Presidio nature walk"


def test_demo_revision_controller_preserves_state_for_blank_and_failed_requests() -> None:
    demo = runpy.run_path("scripts/demo_ai_itinerary.py")
    apply_revision = demo["apply_revision"]
    current = ItineraryDraft(
        items=[ItineraryItemInput(day_number=1, position=1, title="Museum", estimated_cost=10)],
        estimated_total_cost=10,
    )

    class FakeProvider:
        def __init__(self, result: ItineraryDraft | None = None, error: Exception | None = None):
            self.calls: list[str] = []
            self.result = result
            self.error = error

        def revise(
            self,
            context: TripContext,
            current: ItineraryDraft,
            current_version: int,
            instruction: str,
        ) -> ItineraryDraft:
            self.calls.append(instruction)
            if self.error is not None:
                raise self.error
            return self.result or current

    blank_provider = FakeProvider()
    blank_result, blank_error = apply_revision(blank_provider, rich_context(), current, "   ")
    assert blank_result == current
    assert blank_error is None
    assert blank_provider.calls == []

    failed_provider = FakeProvider(error=ItineraryProviderResponseError("rejected"))
    failed_result, failed_error = apply_revision(failed_provider, rich_context(), current, "unsafe")
    assert failed_result == current
    assert failed_error == "rejected"
    assert failed_provider.calls == ["unsafe"]
