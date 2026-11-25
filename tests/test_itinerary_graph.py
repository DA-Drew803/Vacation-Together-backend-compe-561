from __future__ import annotations

import pytest

from app.schemas.itinerary import (
    Budget,
    EligibleMember,
    ItineraryDraft,
    ItineraryItemInput,
    TripContext,
)
from app.services.openai_itinerary import (
    ItineraryProviderResponseError,
    ItineraryRevisionPatchError,
)
from app.workflows.itinerary_graph import (
    run_itinerary_generation_graph,
    run_itinerary_revision_graph,
)


def context() -> TripContext:
    return TripContext(
        trip_id="trip-graph",
        selected_destination_id="destination-graph",
        destination_name="Demo City",
        start_date="2026-06-01",
        end_date="2026-06-03",
        budget=Budget(max_amount=500, currency="USD"),
        eligible_members=[EligibleMember(member_id="member-graph", display_name="Graph Tester")],
    )


def draft(title: str = "Museum visit") -> ItineraryDraft:
    return ItineraryDraft(
        items=[ItineraryItemInput(day_number=1, position=1, title=title, estimated_cost=40)],
        estimated_total_cost=40,
    )


class FlakyGenerationProvider:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, _context: TripContext) -> ItineraryDraft:
        self.calls += 1
        if self.calls == 1:
            raise ItineraryProviderResponseError("malformed model output")
        return draft()


class AlwaysFailingGenerationProvider:
    def generate(self, _context: TripContext) -> ItineraryDraft:
        raise ItineraryProviderResponseError("provider output remained invalid")


class RevisionProvider:
    def __init__(self) -> None:
        self.interpreted_intent = None

    def revise(
        self,
        _context: TripContext,
        _current: ItineraryDraft,
        _current_version: int,
        _instruction: str,
        *,
        interpreted_intent=None,
    ) -> ItineraryDraft:
        self.interpreted_intent = interpreted_intent
        return draft("Accessible museum visit")


class RepairingRevisionProvider:
    def __init__(self, *, always_invalid: bool = False) -> None:
        self.calls = 0
        self.feedback: list[str | None] = []
        self.always_invalid = always_invalid

    def revise(
        self,
        _context: TripContext,
        _current: ItineraryDraft,
        _current_version: int,
        _instruction: str,
        *,
        interpreted_intent=None,
        repair_feedback: str | None = None,
    ) -> ItineraryDraft:
        self.calls += 1
        self.feedback.append(repair_feedback)
        if self.always_invalid or self.calls == 1:
            raise ItineraryRevisionPatchError(
                "A category-specific cost revision changed an unrelated category"
            )
        return draft("Repaired activity")


def test_generation_graph_retries_bounded_provider_failure() -> None:
    provider = FlakyGenerationProvider()

    result = run_itinerary_generation_graph(context(), provider)

    assert result.items[0].title == "Museum visit"
    assert provider.calls == 2


def test_generation_graph_fails_after_retry_exhaustion() -> None:
    with pytest.raises(ItineraryProviderResponseError, match="provider output"):
        run_itinerary_generation_graph(context(), AlwaysFailingGenerationProvider())


def test_revision_graph_returns_provider_result_without_persisting_it() -> None:
    provider = RevisionProvider()
    result = run_itinerary_revision_graph(
        context(),
        draft(),
        1,
        "I am tired but still want something relaxing on Day 1",
        provider,
    )

    assert result.items[0].title == "Accessible museum visit"
    assert provider.interpreted_intent.target_day == 1
    assert "low physical effort" in provider.interpreted_intent.situational_constraints


def test_revision_graph_repairs_a_scope_violation_without_regenerating_full_itinerary() -> None:
    provider = RepairingRevisionProvider()

    result = run_itinerary_revision_graph(
        context(),
        draft(),
        1,
        "Reduce food costs",
        provider,
    )

    assert result.items[0].title == "Repaired activity"
    assert provider.calls == 2
    assert provider.feedback == [
        None,
        "A category-specific cost revision changed an unrelated category",
    ]


def test_revision_graph_stops_after_two_repair_attempts() -> None:
    provider = RepairingRevisionProvider(always_invalid=True)

    with pytest.raises(ItineraryRevisionPatchError, match="unrelated category"):
        run_itinerary_revision_graph(context(), draft(), 1, "Reduce food costs", provider)

    assert provider.calls == 3
    assert provider.feedback[0] is None
    assert provider.feedback[1:] == [
        "A category-specific cost revision changed an unrelated category",
        "A category-specific cost revision changed an unrelated category",
    ]
