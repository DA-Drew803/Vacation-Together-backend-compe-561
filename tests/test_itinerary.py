from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from os import getenv
from typing import Annotated

import pytest
from fastapi import Header, Path
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.dependencies import get_current_member_id, get_trip_context
from app.main import app
from app.models.itinerary import Itinerary, ItineraryApproval, ItineraryItem
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
from app.services.itinerary import ItineraryConflictError


@dataclass
class Harness:
    client: TestClient
    session_factory: sessionmaker[Session]
    context: TripContext
    is_postgres: bool


def trip_context(trip_id: str) -> TripContext:
    return TripContext(
        trip_id=trip_id,
        selected_destination_id="destination-test",
        destination_name="Test City",
        start_date="2027-06-10",
        end_date="2027-06-17",
        budget=Budget(max_amount=950, currency="USD"),
        eligible_members=[
            EligibleMember(member_id="member-1", display_name="Member One"),
            EligibleMember(member_id="member-2", display_name="Member Two"),
            EligibleMember(member_id="member-3", display_name="Member Three"),
            EligibleMember(
                member_id="member-ineligible",
                display_name="Ineligible Member",
                voting_eligible=False,
            ),
        ],
        interests=["food", "museums"],
    )


def draft(*, changed: bool = False) -> ItineraryDraft:
    suffix = " updated" if changed else ""
    return ItineraryDraft(
        items=[
            ItineraryItemInput(
                day_number=1,
                position=1,
                title=f"Arrival walk{suffix}",
                estimated_cost=25,
            ),
            ItineraryItemInput(
                day_number=2,
                position=1,
                title="Museum visit",
                estimated_cost=40,
            ),
        ],
        estimated_total_cost=70 if changed else 65,
    )


@pytest.fixture()
def harness() -> Generator[Harness, None, None]:
    test_database_url = getenv("TEST_DATABASE_URL")
    is_postgres = bool(test_database_url and test_database_url.startswith("postgresql"))
    if test_database_url:
        database_name = make_url(test_database_url).database or ""
        if not database_name.endswith("_test"):
            pytest.fail("TEST_DATABASE_URL must reference a dedicated database ending in '_test'")
        test_engine = create_engine(test_database_url, pool_pre_ping=True)
    else:
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

    testing_session = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=test_engine,
    )
    Base.metadata.create_all(bind=test_engine)

    def override_get_db() -> Generator[Session, None, None]:
        with testing_session() as db:
            yield db

    async def override_trip_context(trip_id: Annotated[str, Path()]) -> TripContext:
        return trip_context(trip_id)

    async def override_current_member(
        test_member_id: Annotated[
            str | None,
            Header(alias="X-Test-Member-Id"),
        ] = None,
    ) -> str:
        return test_member_id or "member-1"

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_trip_context] = override_trip_context
    app.dependency_overrides[get_current_member_id] = override_current_member
    test_client = TestClient(app)

    yield Harness(test_client, testing_session, trip_context("unused"), is_postgres)

    test_client.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=test_engine)
    test_engine.dispose()


def seed_itinerary(harness: Harness, trip_id: str) -> None:
    with harness.session_factory() as db:
        itinerary_service.generate_itinerary_for_selected_destination(
            db,
            trip_context(trip_id),
            draft(),
        )


def approve(harness: Harness, trip_id: str, member_id: str, version: int = 1):
    return harness.client.put(
        f"/api/trips/{trip_id}/itinerary/approval/me",
        headers={"X-Test-Member-Id": member_id},
        json={"version": version, "approved": True},
    )


def test_get_missing_itinerary_has_no_generation_side_effect(harness: Harness) -> None:
    with harness.session_factory() as db:
        before = db.scalar(select(func.count()).select_from(Itinerary))

    response = harness.client.get("/api/trips/missing/itinerary")

    assert response.status_code == 409
    assert response.json() == {"detail": "Itinerary has not been generated"}
    with harness.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Itinerary)) == before
        assert db.scalar(select(func.count()).select_from(ItineraryItem)) == 0


def test_initial_generation_persists_items_and_prevents_duplicate(harness: Harness) -> None:
    with harness.session_factory() as db:
        first = itinerary_service.generate_itinerary_for_selected_destination(
            db,
            trip_context("trip-generation"),
            draft(),
        )
        first_id = first.id

    with harness.session_factory() as db:
        second = itinerary_service.generate_itinerary_for_selected_destination(
            db,
            trip_context("trip-generation"),
            draft(changed=True),
        )
        assert second.id == first_id
        assert second.version == 1
        assert second.status == "DRAFT"
        assert len(second.items) == 2
        assert db.scalar(select(func.count()).select_from(Itinerary)) == 1
        assert db.scalar(select(func.count()).select_from(ItineraryItem)) == 2

    response = harness.client.get("/api/trips/trip-generation/itinerary")
    assert response.status_code == 200
    assert response.json()["days"][0]["activities"][0]["title"] == "Arrival walk"


def test_changed_revision_increments_once_and_resets_approvals(harness: Harness) -> None:
    seed_itinerary(harness, "trip-revision")
    assert approve(harness, "trip-revision", "member-1").status_code == 200

    started = harness.client.post(
        "/api/trips/trip-revision/itinerary/revisions",
        headers={"X-Test-Member-Id": "member-2"},
        json={"instruction": "Make day one cheaper"},
    )
    assert started.status_code == 202
    assert started.json()["status"] == "RUNNING"
    assert started.json()["base_version"] == 1

    with harness.session_factory() as db:
        completed = itinerary_service.complete_revision(
            db, started.json()["id"], draft(changed=True)
        )
        assert completed.status == "SUCCEEDED"

    current = harness.client.get("/api/trips/trip-revision/itinerary").json()
    assert current["version"] == 2
    assert current["days"][0]["activities"][0]["title"] == "Arrival walk updated"
    progress = harness.client.get("/api/trips/trip-revision/itinerary/approval").json()
    assert progress["approved_count"] == 0


def test_no_change_revision_preserves_version_content_and_approvals(harness: Harness) -> None:
    seed_itinerary(harness, "trip-no-change")
    approve(harness, "trip-no-change", "member-1")
    started = harness.client.post(
        "/api/trips/trip-no-change/itinerary/revisions",
        json={"instruction": "Keep the current plan"},
    ).json()

    with harness.session_factory() as db:
        completed = itinerary_service.complete_revision(db, started["id"], draft())
        assert completed.status == "NO_CHANGE"

    current = harness.client.get("/api/trips/trip-no-change/itinerary").json()
    assert current["version"] == 1
    progress = harness.client.get("/api/trips/trip-no-change/itinerary/approval").json()
    assert progress["approved_count"] == 1


def test_failed_revision_preserves_version_content_and_approvals(harness: Harness) -> None:
    seed_itinerary(harness, "trip-failed")
    approve(harness, "trip-failed", "member-1")
    started = harness.client.post(
        "/api/trips/trip-failed/itinerary/revisions",
        json={"instruction": "Try a different plan"},
    ).json()

    with harness.session_factory() as db:
        failed = itinerary_service.fail_revision(db, started["id"])
        assert failed.status == "FAILED"

    current = harness.client.get("/api/trips/trip-failed/itinerary").json()
    assert current["version"] == 1
    assert current["days"][0]["activities"][0]["title"] == "Arrival walk"
    progress = harness.client.get("/api/trips/trip-failed/itinerary/approval").json()
    assert progress["approved_count"] == 1


def test_second_active_revision_is_rejected(harness: Harness) -> None:
    seed_itinerary(harness, "trip-active")
    first = harness.client.post(
        "/api/trips/trip-active/itinerary/revisions",
        json={"instruction": "First request"},
    )
    second = harness.client.post(
        "/api/trips/trip-active/itinerary/revisions",
        json={"instruction": "Second request"},
    )

    assert first.status_code == 202
    assert second.status_code == 409


def test_empty_revision_instruction_is_rejected(harness: Harness) -> None:
    seed_itinerary(harness, "trip-empty")
    response = harness.client.post(
        "/api/trips/trip-empty/itinerary/revisions",
        json={"instruction": "   "},
    )
    assert response.status_code == 400


def test_stale_revision_cannot_overwrite_newer_version(harness: Harness) -> None:
    seed_itinerary(harness, "trip-stale-result")
    started = harness.client.post(
        "/api/trips/trip-stale-result/itinerary/revisions",
        json={"instruction": "Older request"},
    ).json()

    with harness.session_factory() as db:
        itinerary = itinerary_service.require_itinerary(db, "trip-stale-result", lock=True)
        itinerary.version = 2
        itinerary.items[0].title = "Newer content"
        db.commit()

    with (
        harness.session_factory() as db,
        pytest.raises(
            ItineraryConflictError,
            match="stale",
        ),
    ):
        itinerary_service.complete_revision(db, started["id"], draft(changed=True))

    current = harness.client.get("/api/trips/trip-stale-result/itinerary").json()
    assert current["version"] == 2
    assert current["days"][0]["activities"][0]["title"] == "Newer content"


def test_duplicate_approval_counts_once_and_partial_does_not_finalize(
    harness: Harness,
) -> None:
    seed_itinerary(harness, "trip-duplicate-approval")
    assert approve(harness, "trip-duplicate-approval", "member-1").status_code == 200
    assert approve(harness, "trip-duplicate-approval", "member-1").status_code == 200

    progress = harness.client.get("/api/trips/trip-duplicate-approval/itinerary/approval").json()
    assert progress == {
        "version": 1,
        "approved_count": 1,
        "eligible_count": 3,
        "finalized": False,
    }


def test_unanimous_approval_finalizes_and_blocks_revision(harness: Harness) -> None:
    seed_itinerary(harness, "trip-final")
    for member_id in ("member-1", "member-2", "member-3"):
        assert approve(harness, "trip-final", member_id).status_code == 200

    progress = harness.client.get("/api/trips/trip-final/itinerary/approval").json()
    assert progress["approved_count"] == 3
    assert progress["finalized"] is True
    assert harness.client.get("/api/trips/trip-final/itinerary").json()["status"] == "FINALIZED"
    blocked = harness.client.post(
        "/api/trips/trip-final/itinerary/revisions",
        json={"instruction": "Add another activity"},
    )
    assert blocked.status_code == 409


def test_stale_nonmember_and_running_revision_approvals_are_rejected(
    harness: Harness,
) -> None:
    seed_itinerary(harness, "trip-approval-errors")
    stale = approve(harness, "trip-approval-errors", "member-1", version=2)
    outsider = approve(harness, "trip-approval-errors", "outsider")
    assert stale.status_code == 409
    assert outsider.status_code == 409

    harness.client.post(
        "/api/trips/trip-approval-errors/itinerary/revisions",
        json={"instruction": "Start revision"},
    )
    running = approve(harness, "trip-approval-errors", "member-2")
    assert running.status_code == 409


def test_production_identity_dependency_cannot_be_bypassed_by_header(
    harness: Harness,
) -> None:
    seed_itinerary(harness, "trip-auth-boundary")
    override = app.dependency_overrides.pop(get_current_member_id)
    try:
        response = harness.client.put(
            "/api/trips/trip-auth-boundary/itinerary/approval/me",
            headers={"X-Member-Id": "member-1"},
            json={"version": 1, "approved": True},
        )
    finally:
        app.dependency_overrides[get_current_member_id] = override

    assert response.status_code == 503


def test_postgresql_concurrent_revisions_and_approvals(harness: Harness) -> None:
    if not harness.is_postgres:
        pytest.skip("Requires a dedicated PostgreSQL test database")

    def generate() -> str:
        with harness.session_factory() as db:
            itinerary = itinerary_service.generate_itinerary_for_selected_destination(
                db,
                trip_context("trip-concurrent-generation"),
                draft(),
            )
            return itinerary.id

    with ThreadPoolExecutor(max_workers=2) as executor:
        generated_ids = list(executor.map(lambda _: generate(), range(2)))
    assert len(set(generated_ids)) == 1
    with harness.session_factory() as db:
        generation_count = db.scalar(
            select(func.count())
            .select_from(Itinerary)
            .where(Itinerary.trip_id == "trip-concurrent-generation")
        )
        assert generation_count == 1

    seed_itinerary(harness, "trip-concurrent-revision")

    def start_revision(member_id: str) -> str:
        with harness.session_factory() as db:
            try:
                itinerary_service.request_revision(
                    db,
                    "trip-concurrent-revision",
                    member_id,
                    RevisionInput(instruction=f"Request from {member_id}"),
                )
            except ItineraryConflictError:
                return "conflict"
            return "started"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(start_revision, ("member-1", "member-2")))
    assert sorted(outcomes) == ["conflict", "started"]

    seed_itinerary(harness, "trip-concurrent-approval")

    def approve_member(member_id: str) -> None:
        with harness.session_factory() as db:
            itinerary_service.approve_current_version(
                db,
                trip_context("trip-concurrent-approval"),
                member_id,
                ApprovalInput(version=1, approved=True),
            )

    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(approve_member, ("member-1", "member-2", "member-3")))

    with harness.session_factory() as db:
        itinerary = itinerary_service.require_itinerary(db, "trip-concurrent-approval")
        progress = itinerary_service.get_approval_progress(
            db,
            trip_context("trip-concurrent-approval"),
        )
        assert itinerary.status == "FINALIZED"
        assert progress.approved_count == 3
        assert db.scalar(select(func.count()).select_from(ItineraryApproval)) == 3
