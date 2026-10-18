import json
from collections.abc import Generator
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from openai import OpenAIError
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.itinerary import ItineraryApproval, ItineraryRevision
from app.schemas.itinerary import (
    ApprovalInput,
    Budget,
    EligibleMember,
    ItineraryDraft,
    ItineraryItemInput,
    RevisionInput,
    TripContext,
)
from app.services import itinerary as itinerary_service
from app.services.openai_itinerary import (
    ItineraryProviderError,
    ItineraryProviderResponseError,
    ItineraryProviderTimeoutError,
    ItineraryRevisionUnsupportedError,
    OpenAIItineraryProvider,
)


def uuid_for(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"vacation-together-revision:{label}"))


MEMBER_1 = uuid_for("member-1")
MEMBER_2 = uuid_for("member-2")


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


def trip_context(trip_id: str | None = None) -> TripContext:
    return TripContext(
        trip_id=trip_id or uuid_for("trip"),
        selected_destination_id=uuid_for("destination"),
        destination_name="San Diego",
        start_date="2027-06-10",
        end_date="2027-06-12",
        budget=Budget(max_amount=300, currency="USD"),
        eligible_members=[
            EligibleMember(member_id=MEMBER_1, display_name="Member One"),
            EligibleMember(member_id=MEMBER_2, display_name="Member Two"),
        ],
        interests=["food", "museums"],
    )


def current_draft() -> ItineraryDraft:
    return ItineraryDraft(
        items=[
            ItineraryItemInput(
                day_number=1,
                position=1,
                title="Flights: Round-trip airfare estimate",
                estimated_cost=50,
            ),
            ItineraryItemInput(
                day_number=1,
                position=2,
                title="Lodging: Two nights of lodging",
                estimated_cost=100,
            ),
            ItineraryItemInput(
                day_number=2,
                position=1,
                title="Food: Daily meals",
                estimated_cost=30,
            ),
            ItineraryItemInput(
                day_number=2,
                position=2,
                title="Local Transport: Transit pass",
                estimated_cost=15,
            ),
            ItineraryItemInput(
                day_number=3,
                position=1,
                title="Activities: Museum admission",
                estimated_cost=45,
            ),
        ],
        estimated_total_cost=240,
    )


def revision_output(
    *,
    outcome: str = "APPLIED",
    target: str = "cheaper",
) -> dict[str, Any]:
    output = {
        "outcome": outcome,
        "outcome_reason": "The requested change was applied safely.",
        "cost_basis": "PER_PERSON",
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
                "title": "Budget-friendly daily meals" if target == "cheaper" else "Daily meals",
                "estimated_cost_cents": 2_000 if target == "cheaper" else 3_000,
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
                "title": {
                    "activity": "Central park visit",
                    "walking": "Accessible harbor tour",
                }.get(target, "Museum admission"),
                "estimated_cost_cents": 4_500,
            },
        ],
        "estimated_total_cost_cents": 23_000 if target == "cheaper" else 24_000,
    }
    if outcome != "APPLIED":
        output["outcome_reason"] = "The request cannot be applied safely."
    return output


def provider_with(output: Any = None, error: Exception | None = None):
    client = FakeOpenAIClient(output, error)
    provider = OpenAIItineraryProvider(client=client, model="test-model", timeout_seconds=12)
    return provider, client


@pytest.mark.parametrize(
    ("instruction", "target", "expected_title"),
    [
        ("Make Day 2 cheaper.", "cheaper", "Food: Budget-friendly daily meals"),
        ("Replace the museum with a park.", "activity", "Activities: Central park visit"),
    ],
)
def test_revision_provider_applies_supported_changes(
    instruction: str,
    target: str,
    expected_title: str,
) -> None:
    provider, _ = provider_with(revision_output(target=target))

    revised = provider.revise(trip_context(), current_draft(), 1, instruction)

    assert any(item.title.startswith(expected_title) for item in revised.items)
    assert revised.estimated_total_cost <= 240


def test_walking_reduction_is_rejected_without_measurable_fields() -> None:
    provider, client = provider_with(revision_output(target="walking"))

    with pytest.raises(ItineraryRevisionUnsupportedError, match="cannot be verified"):
        provider.revise(trip_context(), current_draft(), 1, "Reduce walking on Day 3")

    assert client.responses.calls == []


@pytest.mark.parametrize(
    "day_expression",
    ["day 2", "day number 2", "the second day"],
)
def test_supported_day_expressions_are_validated_as_a_cheaper_day_request(
    day_expression: str,
) -> None:
    provider, _ = provider_with(revision_output())

    revised = provider.revise(
        trip_context(),
        current_draft(),
        1,
        f"Make {day_expression} cheaper",
    )

    assert revised.estimated_total_cost == 230


@pytest.mark.parametrize(
    "instruction",
    [
        "Make day 2 cheaper and move the trip to Paris",
        "Replace the museum with a park and change the destination",
        "Make day 2 cheaper and replace the museum",
        "Replace the museum and change trip dates",
        "Match our interests and book a hotel",
        "Make day 2 cheaper; replace the museum with a park",
        "Make day 2 cheaper. Replace the museum with a park",
    ],
)
def test_mixed_or_trailing_clauses_are_rejected_before_provider_call(
    instruction: str,
) -> None:
    provider, client = provider_with(revision_output())

    with pytest.raises(ItineraryRevisionUnsupportedError, match="not supported"):
        provider.revise(trip_context(), current_draft(), 1, instruction)

    assert client.responses.calls == []


def test_revision_request_preserves_context_privacy_and_provider_controls() -> None:
    provider, client = provider_with(revision_output())
    context = trip_context()

    provider.revise(context, current_draft(), 3, "  Make Day 2   cheaper.  ")

    call = client.responses.calls[0]
    payload = json.loads(call["input"][1]["content"])
    assert call["store"] is False
    assert call["timeout"] == 12
    assert payload["selected_destination"] == context.destination_name
    assert payload["travel_dates"] == {
        "start_date": context.start_date,
        "end_date": context.end_date,
        "number_of_days": 3,
    }
    assert payload["current_itinerary_version"] == 3
    assert payload["revision_instruction"] == "Make Day 2 cheaper."
    serialized = call["input"][1]["content"]
    assert context.trip_id not in serialized
    assert context.selected_destination_id not in serialized
    assert MEMBER_1 not in serialized


def test_day_specific_revision_must_preserve_unrelated_days() -> None:
    output = revision_output()
    output["items"][0]["title"] = "Different flight"
    provider, _ = provider_with(output)

    with pytest.raises(ItineraryProviderResponseError, match="preserve all unrelated days"):
        provider.revise(trip_context(), current_draft(), 1, "Make Day 2 cheaper")


def test_applied_cheaper_day_must_strictly_decrease_target_day_cost() -> None:
    output = revision_output()
    output["items"][2]["estimated_cost_cents"] = 3_000
    output["estimated_total_cost_cents"] = 24_000
    provider, _ = provider_with(output)

    with pytest.raises(ItineraryProviderResponseError, match="strictly reduce"):
        provider.revise(trip_context(), current_draft(), 1, "Make Day 2 cheaper")


def test_applied_unchanged_content_maps_to_no_change() -> None:
    provider, _ = provider_with(revision_output(target="unchanged"))

    revised = provider.revise(trip_context(), current_draft(), 1, "Make Day 2 cheaper")

    assert revised == current_draft()


def test_activity_replacement_must_change_the_targeted_activity() -> None:
    output = revision_output(target="unchanged")
    output["items"][4]["estimated_cost_cents"] = 4_400
    output["estimated_total_cost_cents"] = 23_900
    provider, _ = provider_with(output)

    with pytest.raises(ItineraryProviderResponseError, match="was not replaced"):
        provider.revise(
            trip_context(),
            current_draft(),
            1,
            "Replace the museum with a park",
        )


def test_activity_replacement_rejects_unrequested_output() -> None:
    output = revision_output(target="activity")
    output["items"][4]["title"] = "Beach kayaking"
    provider, _ = provider_with(output)

    with pytest.raises(ItineraryProviderResponseError, match="requested activity"):
        provider.revise(
            trip_context(),
            current_draft(),
            1,
            "Replace the museum with a park",
        )


def test_activity_target_uses_phrase_boundaries() -> None:
    draft = current_draft().model_copy(deep=True)
    draft.items[4].title = "Activities: Cart racing"
    provider, client = provider_with(revision_output(target="activity"))

    with pytest.raises(ItineraryRevisionUnsupportedError, match="exactly one"):
        provider.revise(trip_context(), draft, 1, "Replace art with a park")

    assert client.responses.calls == []


def test_activity_replacement_rejects_missing_or_ambiguous_target() -> None:
    provider, client = provider_with(revision_output(target="activity"))
    with pytest.raises(ItineraryRevisionUnsupportedError, match="exactly one"):
        provider.revise(trip_context(), current_draft(), 1, "Replace the zoo with a park")
    assert client.responses.calls == []

    ambiguous = current_draft().model_copy(deep=True)
    ambiguous.items.append(
        ItineraryItemInput(
            day_number=3,
            position=2,
            title="Activities: Museum garden",
            estimated_cost=0,
        )
    )
    second_provider, second_client = provider_with(revision_output(target="activity"))
    with pytest.raises(ItineraryRevisionUnsupportedError, match="exactly one"):
        second_provider.revise(
            trip_context(),
            ambiguous,
            1,
            "Replace the museum with a park",
        )
    assert second_client.responses.calls == []


def test_activity_replacement_rejects_ambiguous_requested_replacement() -> None:
    provider, client = provider_with(revision_output(target="activity"))

    with pytest.raises(ItineraryRevisionUnsupportedError, match="clear replacement intent"):
        provider.revise(
            trip_context(),
            current_draft(),
            1,
            "Replace the museum with another activity",
        )

    assert client.responses.calls == []


def test_activity_replacement_preserves_unrelated_activity() -> None:
    draft = current_draft().model_copy(deep=True)
    draft.items.append(
        ItineraryItemInput(
            day_number=2,
            position=3,
            title="Activities: Harbor tour",
            estimated_cost=0,
        )
    )
    output = revision_output(target="activity")
    output["items"].append(
        {
            "day_number": 2,
            "position": 3,
            "category": "activities",
            "title": "Changed harbor tour",
            "estimated_cost_cents": 0,
        }
    )
    provider, _ = provider_with(output)

    with pytest.raises(ItineraryProviderResponseError, match="preserve all unrelated"):
        provider.revise(
            trip_context(),
            draft,
            1,
            "Replace the museum with a park",
        )


def test_activity_replacement_rejects_unrelated_day_change() -> None:
    output = revision_output(target="activity")
    output["items"][0]["title"] = "Different flight"
    provider, _ = provider_with(output)

    with pytest.raises(ItineraryProviderResponseError, match="preserve all unrelated"):
        provider.revise(
            trip_context(),
            current_draft(),
            1,
            "Replace the museum with a park",
        )


@pytest.mark.parametrize(
    "instruction",
    [
        "Adjust activities to better match group interests",
        "Match activities to our interests",
        "Tailor activities to group interests",
        "Align activities with group interests",
    ],
)
def test_interest_revisions_are_disabled_before_provider_call(instruction: str) -> None:
    provider, client = provider_with(revision_output(target="activity"))

    with pytest.raises(ItineraryRevisionUnsupportedError, match="structured activity tags"):
        provider.revise(trip_context(), current_draft(), 1, instruction)

    assert client.responses.calls == []


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda output: output.update(estimated_total_cost_cents=30_100), "equal the sum"),
        (
            lambda output: output["items"].__setitem__(
                4, {**output["items"][4], "category": "food"}
            ),
            "must account for flights",
        ),
    ],
)
def test_revision_rejects_invalid_budget_or_category_output(mutator: Any, message: str) -> None:
    output = revision_output()
    mutator(output)
    provider, _ = provider_with(output)

    with pytest.raises(ItineraryProviderResponseError, match=message):
        provider.revise(trip_context(), current_draft(), 1, "Make Day 2 cheaper")


def test_malformed_revision_output_is_rejected() -> None:
    provider, _ = provider_with({"outcome": "APPLIED", "items": []})

    with pytest.raises(ItineraryProviderResponseError, match="incomplete or invalid"):
        provider.revise(trip_context(), current_draft(), 1, "Make Day 2 cheaper")


def test_unsupported_revision_is_safe_and_does_not_return_content() -> None:
    provider, client = provider_with(revision_output(outcome="UNSUPPORTED", target="unchanged"))

    with pytest.raises(ItineraryRevisionUnsupportedError, match="not supported"):
        provider.revise(trip_context(), current_draft(), 1, "Move the trip to another city")

    assert client.responses.calls == []


@pytest.mark.parametrize(
    ("error", "expected_exception", "message"),
    [
        (TimeoutError("socket details"), ItineraryProviderTimeoutError, "timed out"),
        (OpenAIError("upstream details"), ItineraryProviderError, "revision failed"),
    ],
)
def test_revision_provider_errors_are_wrapped_safely(
    error: Exception,
    expected_exception: type[Exception],
    message: str,
) -> None:
    provider, _ = provider_with(error=error)

    with pytest.raises(expected_exception, match=message) as raised:
        provider.revise(trip_context(), current_draft(), 1, "Make Day 2 cheaper")

    assert str(error) not in str(raised.value)


@pytest.fixture()
def session_factory() -> Generator[sessionmaker[Session], None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, class_=Session)
    Base.metadata.create_all(bind=engine)
    yield factory
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


class StubRevisionProvider:
    def __init__(
        self,
        db: Session,
        result: ItineraryDraft | None = None,
        error: Exception | None = None,
    ) -> None:
        self.db = db
        self.result = result
        self.error = error
        self.called_outside_transaction = False

    def revise(
        self,
        context: TripContext,
        current: ItineraryDraft,
        current_version: int,
        instruction: str,
    ) -> ItineraryDraft:
        del context, current_version, instruction
        self.called_outside_transaction = not self.db.in_transaction()
        if self.error is not None:
            raise self.error
        return self.result or current


class FailedTransactionProvider:
    def __init__(self, db: Session) -> None:
        self.db = db

    def revise(
        self,
        context: TripContext,
        current: ItineraryDraft,
        current_version: int,
        instruction: str,
    ) -> ItineraryDraft:
        del current_version, instruction
        itinerary = itinerary_service.require_itinerary(self.db, context.trip_id)
        self.db.add(
            ItineraryApproval(
                itinerary_id=itinerary.id,
                trip_member_id=UUID(MEMBER_1),
                version=1,
                approved=True,
            )
        )
        self.db.flush()
        return current


def seed_itinerary(db: Session, context: TripContext) -> None:
    itinerary_service.generate_itinerary_for_selected_destination(db, context, current_draft())


def approve_first_member(db: Session, context: TripContext) -> None:
    itinerary_service.approve_current_version(
        db,
        context,
        MEMBER_1,
        ApprovalInput(version=1, approved=True),
    )


def changed_draft() -> ItineraryDraft:
    result = current_draft().model_copy(deep=True)
    result.items[2].title = "Food: Budget-friendly daily meals"
    result.items[2].estimated_cost = 20
    result.estimated_total_cost = 230
    return result


@pytest.mark.parametrize(
    "instruction",
    [
        "Make day 2 cheaper and move the trip to Paris",
        "Replace the museum with a park and change the destination",
        "Make day 2 cheaper and replace the museum",
        "Replace the museum and change trip dates",
        "Match our interests and book a hotel",
        "Make day 2 cheaper, then replace the museum with a park",
    ],
)
def test_rejected_mixed_intent_preserves_itinerary_version_and_approval(
    instruction: str,
    session_factory: sessionmaker[Session],
) -> None:
    context = trip_context(uuid_for("rejected-mixed-intent"))
    provider, client = provider_with(revision_output())
    with session_factory() as db:
        seed_itinerary(db, context)
        approve_first_member(db, context)

        with pytest.raises(ItineraryRevisionUnsupportedError, match="not supported"):
            itinerary_service.execute_ai_revision(
                db,
                context,
                MEMBER_2,
                RevisionInput(instruction=instruction),
                provider,
            )

        itinerary = itinerary_service.require_itinerary(db, context.trip_id)
        revision = db.scalar(select(ItineraryRevision))
        approvals = db.scalars(select(ItineraryApproval)).all()
        assert client.responses.calls == []
        assert revision is not None and revision.status == "FAILED"
        assert itinerary.status == "DRAFT"
        assert itinerary.version == 1
        assert itinerary.estimated_total_cost == Decimal("240.00")
        assert len(approvals) == 1


def test_disabled_interest_revision_preserves_itinerary_and_approval(
    session_factory: sessionmaker[Session],
) -> None:
    context = trip_context(uuid_for("disabled-interest-revision"))
    provider, client = provider_with(revision_output(target="activity"))
    with session_factory() as db:
        seed_itinerary(db, context)
        approve_first_member(db, context)

        with pytest.raises(ItineraryRevisionUnsupportedError, match="structured activity tags"):
            itinerary_service.execute_ai_revision(
                db,
                context,
                MEMBER_2,
                RevisionInput(
                    instruction="Adjust activities to better match group interests"
                ),
                provider,
            )

        itinerary = itinerary_service.require_itinerary(db, context.trip_id)
        revision = db.scalar(select(ItineraryRevision))
        approvals = db.scalars(select(ItineraryApproval)).all()
        assert client.responses.calls == []
        assert revision is not None and revision.status == "FAILED"
        assert itinerary.status == "DRAFT"
        assert itinerary.version == 1
        assert itinerary.estimated_total_cost == Decimal("240.00")
        assert len(approvals) == 1


def test_orchestration_completes_changed_revision_and_resets_approvals(
    session_factory: sessionmaker[Session],
) -> None:
    context = trip_context(uuid_for("successful-orchestration"))
    with session_factory() as db:
        seed_itinerary(db, context)
        approve_first_member(db, context)
        provider = StubRevisionProvider(db, result=changed_draft())

        revision = itinerary_service.execute_ai_revision(
            db,
            context,
            MEMBER_2,
            RevisionInput(instruction="Make Day 2 cheaper"),
            provider,
        )

        itinerary = itinerary_service.require_itinerary(db, context.trip_id)
        assert provider.called_outside_transaction is True
        assert revision.status == "SUCCEEDED"
        assert itinerary.version == 2
        assert itinerary.estimated_total_cost == Decimal("230.00")
        assert db.scalars(select(ItineraryApproval)).all() == []


def test_orchestration_no_change_preserves_version_and_approval(
    session_factory: sessionmaker[Session],
) -> None:
    context = trip_context(uuid_for("no-change-orchestration"))
    with session_factory() as db:
        seed_itinerary(db, context)
        approve_first_member(db, context)

        revision = itinerary_service.execute_ai_revision(
            db,
            context,
            MEMBER_2,
            RevisionInput(instruction="Keep the current plan"),
            StubRevisionProvider(db),
        )

        itinerary = itinerary_service.require_itinerary(db, context.trip_id)
        approvals = db.scalars(select(ItineraryApproval)).all()
        assert revision.status == "NO_CHANGE"
        assert itinerary.version == 1
        assert len(approvals) == 1


def test_orchestration_failure_preserves_content_version_and_approval(
    session_factory: sessionmaker[Session],
) -> None:
    context = trip_context(uuid_for("failed-orchestration"))
    with session_factory() as db:
        seed_itinerary(db, context)
        approve_first_member(db, context)
        original = deepcopy(current_draft().model_dump())

        with pytest.raises(ItineraryProviderError, match="safe failure"):
            itinerary_service.execute_ai_revision(
                db,
                context,
                MEMBER_2,
                RevisionInput(instruction="Try an unsupported change"),
                StubRevisionProvider(db, error=ItineraryProviderError("safe failure")),
            )

        itinerary = itinerary_service.require_itinerary(db, context.trip_id)
        persisted = ItineraryDraft(
            items=[
                ItineraryItemInput(
                    day_number=item.day_number,
                    position=item.position,
                    title=item.title,
                    estimated_cost=float(item.estimated_cost),
                )
                for item in itinerary.items
            ],
            estimated_total_cost=float(itinerary.estimated_total_cost),
        )
        approvals = db.scalars(select(ItineraryApproval)).all()
        assert itinerary.status == "DRAFT"
        assert itinerary.version == 1
        assert persisted.model_dump() == original
        assert len(approvals) == 1


def test_unexpected_provider_exception_cleans_up_revision_lifecycle(
    session_factory: sessionmaker[Session],
) -> None:
    context = trip_context(uuid_for("runtime-error-orchestration"))
    with session_factory() as db:
        seed_itinerary(db, context)
        approve_first_member(db, context)

        with pytest.raises(RuntimeError, match="unexpected provider failure"):
            itinerary_service.execute_ai_revision(
                db,
                context,
                MEMBER_2,
                RevisionInput(instruction="Make Day 2 cheaper"),
                StubRevisionProvider(db, error=RuntimeError("unexpected provider failure")),
            )

        itinerary = itinerary_service.require_itinerary(db, context.trip_id)
        revision = db.scalar(select(ItineraryRevision))
        approvals = db.scalars(select(ItineraryApproval)).all()
        assert revision is not None and revision.status == "FAILED"
        assert itinerary.status == "DRAFT"
        assert itinerary.version == 1
        assert len(approvals) == 1


def test_current_payload_conversion_failure_cleans_up_revision(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = trip_context(uuid_for("conversion-error-orchestration"))
    with session_factory() as db:
        seed_itinerary(db, context)
        approve_first_member(db, context)
        provider = StubRevisionProvider(db, result=changed_draft())

        def fail_conversion(value: Any) -> float:
            del value
            raise RuntimeError("payload conversion failed")

        monkeypatch.setattr(itinerary_service, "_money", fail_conversion)
        with pytest.raises(RuntimeError, match="payload conversion failed"):
            itinerary_service.execute_ai_revision(
                db,
                context,
                MEMBER_2,
                RevisionInput(instruction="Make Day 2 cheaper"),
                provider,
            )

        itinerary = itinerary_service.require_itinerary(db, context.trip_id)
        revision = db.scalar(select(ItineraryRevision))
        approvals = db.scalars(select(ItineraryApproval)).all()
        assert provider.called_outside_transaction is False
        assert revision is not None and revision.status == "FAILED"
        assert itinerary.status == "DRAFT"
        assert itinerary.version == 1
        assert len(approvals) == 1


def test_failed_database_transaction_is_rolled_back_before_cleanup(
    session_factory: sessionmaker[Session],
) -> None:
    context = trip_context(uuid_for("failed-transaction-orchestration"))
    with session_factory() as db:
        seed_itinerary(db, context)
        approve_first_member(db, context)

        with pytest.raises(IntegrityError):
            itinerary_service.execute_ai_revision(
                db,
                context,
                MEMBER_2,
                RevisionInput(instruction="Make Day 2 cheaper"),
                FailedTransactionProvider(db),
            )

        itinerary = itinerary_service.require_itinerary(db, context.trip_id)
        revision = db.scalar(select(ItineraryRevision))
        approvals = db.scalars(select(ItineraryApproval)).all()
        assert revision is not None and revision.status == "FAILED"
        assert itinerary.status == "DRAFT"
        assert itinerary.version == 1
        assert len(approvals) == 1


def test_cleanup_failure_does_not_mask_original_exception(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    context = trip_context(uuid_for("cleanup-failure"))
    with session_factory() as db:
        seed_itinerary(db, context)

        def fail_cleanup(session: Session, revision_id: str) -> None:
            del session, revision_id
            raise RuntimeError("cleanup database unavailable")

        monkeypatch.setattr(itinerary_service, "fail_revision", fail_cleanup)
        with pytest.raises(ValueError, match="original provider failure"):
            itinerary_service.execute_ai_revision(
                db,
                context,
                MEMBER_2,
                RevisionInput(instruction="Make day 2 cheaper"),
                StubRevisionProvider(db, error=ValueError("original provider failure")),
            )

        revision = db.scalar(select(ItineraryRevision))
        assert revision is not None and revision.status == "RUNNING"
        assert "Failed to clean up itinerary revision" in caplog.text


def test_rollback_failure_does_not_mask_original_exception(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    context = trip_context(uuid_for("rollback-failure"))
    with session_factory() as db:
        seed_itinerary(db, context)
        original_rollback = db.rollback

        def fail_rollback() -> None:
            raise RuntimeError("rollback unavailable")

        monkeypatch.setattr(db, "rollback", fail_rollback)
        with pytest.raises(ValueError, match="original provider failure"):
            itinerary_service.execute_ai_revision(
                db,
                context,
                MEMBER_2,
                RevisionInput(instruction="Make day 2 cheaper"),
                StubRevisionProvider(db, error=ValueError("original provider failure")),
            )

        assert "Failed to roll back before cleaning up revision" in caplog.text
        monkeypatch.setattr(db, "rollback", original_rollback)
        original_rollback()


def test_stale_version_during_provider_call_cannot_overwrite_itinerary(
    session_factory: sessionmaker[Session],
) -> None:
    context = trip_context(uuid_for("stale-during-provider"))
    with session_factory() as db:
        seed_itinerary(db, context)
        approve_first_member(db, context)

        class StaleVersionProvider:
            def revise(
                self,
                provider_context: TripContext,
                current: ItineraryDraft,
                current_version: int,
                instruction: str,
            ) -> ItineraryDraft:
                del current, current_version, instruction
                itinerary = itinerary_service.require_itinerary(
                    db,
                    provider_context.trip_id,
                )
                itinerary.version = 2
                db.commit()
                return changed_draft()

        with pytest.raises(itinerary_service.ItineraryConflictError, match="stale"):
            itinerary_service.execute_ai_revision(
                db,
                context,
                MEMBER_2,
                RevisionInput(instruction="Make day 2 cheaper"),
                StaleVersionProvider(),
            )

        itinerary = itinerary_service.require_itinerary(db, context.trip_id)
        revision = db.scalar(select(ItineraryRevision))
        approvals = db.scalars(select(ItineraryApproval)).all()
        assert revision is not None and revision.status == "FAILED"
        assert itinerary.status == "DRAFT"
        assert itinerary.version == 2
        assert itinerary.estimated_total_cost == Decimal("240.00")
        assert len(approvals) == 1


def test_request_revision_does_not_refresh_after_commit(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = trip_context(uuid_for("request-without-refresh"))
    with session_factory() as db:
        seed_itinerary(db, context)
        original_refresh = db.refresh

        def fail_refresh(instance: Any, *args: Any, **kwargs: Any) -> None:
            del instance, args, kwargs
            raise RuntimeError("request refresh failed")

        monkeypatch.setattr(db, "refresh", fail_refresh)
        revision = itinerary_service.request_revision(
            db,
            context,
            MEMBER_2,
            RevisionInput(instruction="Make Day 2 cheaper"),
        )

        assert revision.id is not None
        assert revision.status == "RUNNING"
        assert revision.base_version == 1
        monkeypatch.setattr(db, "refresh", original_refresh)
        failed = itinerary_service.fail_revision(db, str(revision.id))
        assert failed.status == "FAILED"


def test_existing_active_revision_is_not_cleaned_up_by_another_request(
    session_factory: sessionmaker[Session],
) -> None:
    context = trip_context(uuid_for("unrelated-active-revision"))
    with session_factory() as db:
        seed_itinerary(db, context)
        existing = itinerary_service.request_revision(
            db,
            context,
            MEMBER_1,
            RevisionInput(instruction="Make Day 2 cheaper"),
        )
        provider = StubRevisionProvider(db, result=changed_draft())

        with pytest.raises(itinerary_service.ItineraryConflictError, match="already running"):
            itinerary_service.execute_ai_revision(
                db,
                context,
                MEMBER_2,
                RevisionInput(instruction="Make Day 2 cheaper"),
                provider,
            )

        stored = db.get(ItineraryRevision, existing.id)
        assert provider.called_outside_transaction is False
        assert stored is not None and stored.status == "RUNNING"
        assert stored.requested_by_member_id == UUID(MEMBER_1)


def test_exception_after_successful_commit_does_not_mark_revision_failed(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = trip_context(uuid_for("exception-after-success"))
    with session_factory() as db:
        seed_itinerary(db, context)
        approve_first_member(db, context)
        original_refresh = db.refresh
        revision_refreshes = 0

        def fail_second_revision_refresh(instance: Any, *args: Any, **kwargs: Any) -> None:
            nonlocal revision_refreshes
            if isinstance(instance, ItineraryRevision):
                revision_refreshes += 1
                if revision_refreshes == 1:
                    raise RuntimeError("post-commit refresh failed")
            original_refresh(instance, *args, **kwargs)

        monkeypatch.setattr(db, "refresh", fail_second_revision_refresh)
        with pytest.raises(RuntimeError, match="post-commit refresh failed"):
            itinerary_service.execute_ai_revision(
                db,
                context,
                MEMBER_2,
                RevisionInput(instruction="Make day 2 cheaper"),
                StubRevisionProvider(db, result=changed_draft()),
            )

        itinerary = itinerary_service.require_itinerary(db, context.trip_id)
        revision = db.scalar(select(ItineraryRevision))
        approvals = db.scalars(select(ItineraryApproval)).all()
        assert revision is not None and revision.status == "SUCCEEDED"
        assert itinerary.status == "DRAFT"
        assert itinerary.version == 2
        assert itinerary.estimated_total_cost == Decimal("230.00")
        assert approvals == []


def test_orchestration_rejects_finalized_itinerary_before_provider_call(
    session_factory: sessionmaker[Session],
) -> None:
    context = trip_context(uuid_for("finalized-orchestration"))
    with session_factory() as db:
        seed_itinerary(db, context)
        approve_first_member(db, context)
        itinerary_service.approve_current_version(
            db,
            context,
            MEMBER_2,
            ApprovalInput(version=1, approved=True),
        )
        provider = StubRevisionProvider(db, result=changed_draft())

        with pytest.raises(itinerary_service.ItineraryConflictError, match="Finalized"):
            itinerary_service.execute_ai_revision(
                db,
                context,
                MEMBER_1,
                RevisionInput(instruction="Change the activity"),
                provider,
            )

        assert provider.called_outside_transaction is False
