from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from os import getenv
from typing import Annotated
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from fastapi import Header, Path
from fastapi.testclient import TestClient
from sqlalchemy import Text, Uuid, create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.dependencies import get_current_member_id, get_trip_context
from app.main import app
from app.models.itinerary import (
    Itinerary,
    ItineraryApproval,
    ItineraryItem,
    ItineraryRevision,
)
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


def uuid_for(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"vacation-together:{label}"))


DESTINATION_ID = uuid_for("destination-test")
MEMBER_1 = uuid_for("member-1")
MEMBER_2 = uuid_for("member-2")
MEMBER_3 = uuid_for("member-3")
MEMBER_INELIGIBLE = uuid_for("member-ineligible")
OUTSIDER = uuid_for("outsider")


def trip_context(trip_id: str) -> TripContext:
    return TripContext(
        trip_id=trip_id,
        selected_destination_id=DESTINATION_ID,
        destination_name="Test City",
        start_date="2027-06-10",
        end_date="2027-06-17",
        budget=Budget(max_amount=950, currency="USD"),
        eligible_members=[
            EligibleMember(member_id=MEMBER_1, display_name="Member One"),
            EligibleMember(member_id=MEMBER_2, display_name="Member Two"),
            EligibleMember(member_id=MEMBER_3, display_name="Member Three"),
            EligibleMember(
                member_id=MEMBER_INELIGIBLE,
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

    async def override_trip_context(trip_id: Annotated[UUID, Path()]) -> TripContext:
        return trip_context(trip_id)

    async def override_current_member(
        test_member_id: Annotated[
            str | None,
            Header(alias="X-Test-Member-Id"),
        ] = None,
    ) -> UUID:
        return UUID(test_member_id or MEMBER_1)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_trip_context] = override_trip_context
    app.dependency_overrides[get_current_member_id] = override_current_member
    test_client = TestClient(app)

    yield Harness(test_client, testing_session, trip_context(uuid_for("unused")), is_postgres)

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


def test_uml_owned_column_types_and_openapi_contract() -> None:
    uuid_columns = (
        Itinerary.__table__.c.id,
        Itinerary.__table__.c.trip_id,
        Itinerary.__table__.c.destination_id,
        ItineraryItem.__table__.c.id,
        ItineraryItem.__table__.c.itinerary_id,
        ItineraryRevision.__table__.c.id,
        ItineraryRevision.__table__.c.itinerary_id,
        ItineraryRevision.__table__.c.requested_by_member_id,
        ItineraryApproval.__table__.c.itinerary_id,
        ItineraryApproval.__table__.c.trip_member_id,
    )
    assert all(isinstance(column.type, Uuid) for column in uuid_columns)
    assert isinstance(ItineraryItem.__table__.c.title.type, Text)
    assert Itinerary.__table__.c.estimated_total_cost.type.python_type is Decimal
    assert ItineraryItem.__table__.c.estimated_cost.type.python_type is Decimal
    external_references = (
        Itinerary.__table__.c.trip_id,
        Itinerary.__table__.c.destination_id,
        ItineraryRevision.__table__.c.requested_by_member_id,
        ItineraryApproval.__table__.c.trip_member_id,
    )
    assert all(not column.foreign_keys for column in external_references)
    assert set(ItineraryApproval.__table__.primary_key.columns) == {
        ItineraryApproval.__table__.c.itinerary_id,
        ItineraryApproval.__table__.c.trip_member_id,
    }

    openapi = app.openapi()
    schemas = openapi["components"]["schemas"]
    assert schemas["Itinerary"]["properties"]["id"]["type"] == "string"
    assert schemas["Itinerary"]["properties"]["id"]["format"] == "uuid"
    assert schemas["Itinerary"]["properties"]["trip_id"]["format"] == "uuid"
    assert schemas["Itinerary"]["properties"]["destination_id"]["format"] == "uuid"
    assert schemas["Budget"]["properties"]["max_amount"]["type"] == "number"
    assert schemas["Itinerary"]["properties"]["status"]["enum"] == [
        "GENERATING",
        "DRAFT",
        "REVISING",
        "FINALIZED",
        "FAILED",
    ]
    assert schemas["Revision"]["properties"]["status"]["enum"] == [
        "PENDING",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "NO_CHANGE",
    ]
    assert schemas["Revision"]["properties"]["id"]["format"] == "uuid"

    operations = (
        ("/api/trips/{trip_id}/itinerary", "get"),
        ("/api/trips/{trip_id}/itinerary/revisions", "post"),
        ("/api/trips/{trip_id}/itinerary/approval", "get"),
        ("/api/trips/{trip_id}/itinerary/approval/me", "put"),
    )
    for path, method in operations:
        trip_id_parameter = openapi["paths"][path][method]["parameters"][0]
        assert trip_id_parameter["name"] == "trip_id"
        assert trip_id_parameter["schema"]["format"] == "uuid"
        responses = openapi["paths"][path][method]["responses"]
        for status_code in ("400", "401", "403", "409"):
            schema = responses[status_code]["content"]["application/json"]["schema"]
            assert schema["$ref"] == "#/components/schemas/Error"


def test_get_missing_itinerary_has_no_generation_side_effect(harness: Harness) -> None:
    with harness.session_factory() as db:
        before = db.scalar(select(func.count()).select_from(Itinerary))

    response = harness.client.get(f"/api/trips/{uuid_for('missing')}/itinerary")

    assert response.status_code == 409
    assert response.json() == {"detail": "Itinerary has not been generated"}
    with harness.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Itinerary)) == before
        assert db.scalar(select(func.count()).select_from(ItineraryItem)) == 0


def test_route_rejects_non_uuid_trip_id(harness: Harness) -> None:
    response = harness.client.get("/api/trips/not-a-uuid/itinerary")

    assert response.status_code == 422


def test_initial_generation_persists_items_and_prevents_duplicate(harness: Harness) -> None:
    trip_id = uuid_for("trip-generation")
    with harness.session_factory() as db:
        first = itinerary_service.generate_itinerary_for_selected_destination(
            db,
            trip_context(trip_id),
            draft(),
        )
        first_id = first.id
        assert isinstance(first.id, UUID)
        assert isinstance(first.estimated_total_cost, Decimal)
        assert isinstance(first.items[0].estimated_cost, Decimal)

    with harness.session_factory() as db:
        second = itinerary_service.generate_itinerary_for_selected_destination(
            db,
            trip_context(trip_id),
            draft(changed=True),
        )
        assert second.id == first_id
        assert second.version == 1
        assert second.status == "DRAFT"
        assert len(second.items) == 2
        assert db.scalar(select(func.count()).select_from(Itinerary)) == 1
        assert db.scalar(select(func.count()).select_from(ItineraryItem)) == 2

    response = harness.client.get(f"/api/trips/{trip_id}/itinerary")
    assert response.status_code == 200
    assert response.json()["days"][0]["activities"][0]["title"] == "Arrival walk"


def test_changed_revision_increments_once_and_resets_approvals(harness: Harness) -> None:
    trip_id = uuid_for("trip-revision")
    seed_itinerary(harness, trip_id)
    assert approve(harness, trip_id, MEMBER_1).status_code == 200

    started = harness.client.post(
        f"/api/trips/{trip_id}/itinerary/revisions",
        headers={"X-Test-Member-Id": MEMBER_2},
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

    current = harness.client.get(f"/api/trips/{trip_id}/itinerary").json()
    assert current["version"] == 2
    assert current["days"][0]["activities"][0]["title"] == "Arrival walk updated"
    progress = harness.client.get(f"/api/trips/{trip_id}/itinerary/approval").json()
    assert progress["approved_count"] == 0


def test_no_change_revision_preserves_version_content_and_approvals(harness: Harness) -> None:
    trip_id = uuid_for("trip-no-change")
    seed_itinerary(harness, trip_id)
    approve(harness, trip_id, MEMBER_1)
    original = harness.client.get(f"/api/trips/{trip_id}/itinerary").json()
    started = harness.client.post(
        f"/api/trips/{trip_id}/itinerary/revisions",
        json={"instruction": "Keep the current plan"},
    ).json()

    with harness.session_factory() as db:
        completed = itinerary_service.complete_revision(db, started["id"], draft())
        assert completed.status == "NO_CHANGE"

    current = harness.client.get(f"/api/trips/{trip_id}/itinerary").json()
    assert current["version"] == 1
    assert current["days"] == original["days"]
    progress = harness.client.get(f"/api/trips/{trip_id}/itinerary/approval").json()
    assert progress["approved_count"] == 1


def test_failed_revision_preserves_version_content_and_approvals(harness: Harness) -> None:
    trip_id = uuid_for("trip-failed")
    seed_itinerary(harness, trip_id)
    approve(harness, trip_id, MEMBER_1)
    started = harness.client.post(
        f"/api/trips/{trip_id}/itinerary/revisions",
        json={"instruction": "Try a different plan"},
    ).json()

    with harness.session_factory() as db:
        failed = itinerary_service.fail_revision(db, started["id"])
        assert failed.status == "FAILED"

    current = harness.client.get(f"/api/trips/{trip_id}/itinerary").json()
    assert current["version"] == 1
    assert current["days"][0]["activities"][0]["title"] == "Arrival walk"
    progress = harness.client.get(f"/api/trips/{trip_id}/itinerary/approval").json()
    assert progress["approved_count"] == 1


def test_second_active_revision_is_rejected(harness: Harness) -> None:
    trip_id = uuid_for("trip-active")
    seed_itinerary(harness, trip_id)
    first = harness.client.post(
        f"/api/trips/{trip_id}/itinerary/revisions",
        json={"instruction": "First request"},
    )
    second = harness.client.post(
        f"/api/trips/{trip_id}/itinerary/revisions",
        json={"instruction": "Second request"},
    )

    assert first.status_code == 202
    assert second.status_code == 409


def test_empty_revision_instruction_is_rejected(harness: Harness) -> None:
    trip_id = uuid_for("trip-empty")
    seed_itinerary(harness, trip_id)
    response = harness.client.post(
        f"/api/trips/{trip_id}/itinerary/revisions",
        json={"instruction": "   "},
    )
    assert response.status_code == 400


def test_noneligible_member_cannot_request_revision(harness: Harness) -> None:
    trip_id = uuid_for("trip-ineligible-revision")
    seed_itinerary(harness, trip_id)

    response = harness.client.post(
        f"/api/trips/{trip_id}/itinerary/revisions",
        headers={"X-Test-Member-Id": MEMBER_INELIGIBLE},
        json={"instruction": "Replace the museum"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Only eligible trip members may request revisions"}
    with harness.session_factory() as db:
        itinerary = itinerary_service.require_itinerary(db, trip_id)
        assert itinerary.status == "DRAFT"
        assert db.scalar(select(func.count()).select_from(ItineraryRevision)) == 0


def test_stale_revision_cannot_overwrite_newer_version(harness: Harness) -> None:
    trip_id = uuid_for("trip-stale-result")
    seed_itinerary(harness, trip_id)
    started = harness.client.post(
        f"/api/trips/{trip_id}/itinerary/revisions",
        json={"instruction": "Older request"},
    ).json()

    with harness.session_factory() as db:
        itinerary = itinerary_service.require_itinerary(db, trip_id, lock=True)
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

    current = harness.client.get(f"/api/trips/{trip_id}/itinerary").json()
    assert current["version"] == 2
    assert current["days"][0]["activities"][0]["title"] == "Newer content"


def test_duplicate_approval_counts_once_and_partial_does_not_finalize(
    harness: Harness,
) -> None:
    trip_id = uuid_for("trip-duplicate-approval")
    seed_itinerary(harness, trip_id)
    assert approve(harness, trip_id, MEMBER_1).status_code == 200
    assert approve(harness, trip_id, MEMBER_1).status_code == 200

    progress = harness.client.get(f"/api/trips/{trip_id}/itinerary/approval").json()
    assert progress == {
        "version": 1,
        "approved_count": 1,
        "eligible_count": 3,
        "finalized": False,
    }


def test_unanimous_approval_finalizes_and_blocks_revision(harness: Harness) -> None:
    trip_id = uuid_for("trip-final")
    seed_itinerary(harness, trip_id)
    for member_id in (MEMBER_1, MEMBER_2, MEMBER_3):
        assert approve(harness, trip_id, member_id).status_code == 200

    progress = harness.client.get(f"/api/trips/{trip_id}/itinerary/approval").json()
    assert progress["approved_count"] == 3
    assert progress["finalized"] is True
    assert harness.client.get(f"/api/trips/{trip_id}/itinerary").json()["status"] == "FINALIZED"
    blocked = harness.client.post(
        f"/api/trips/{trip_id}/itinerary/revisions",
        json={"instruction": "Add another activity"},
    )
    assert blocked.status_code == 409


def test_two_of_three_approvals_remain_draft(harness: Harness) -> None:
    trip_id = uuid_for("trip-two-of-three")
    seed_itinerary(harness, trip_id)
    assert approve(harness, trip_id, MEMBER_1).status_code == 200
    assert approve(harness, trip_id, MEMBER_2).status_code == 200

    progress = harness.client.get(f"/api/trips/{trip_id}/itinerary/approval").json()
    assert progress == {
        "version": 1,
        "approved_count": 2,
        "eligible_count": 3,
        "finalized": False,
    }
    assert harness.client.get(f"/api/trips/{trip_id}/itinerary").json()["status"] == "DRAFT"


def test_stale_nonmember_and_running_revision_approvals_are_rejected(
    harness: Harness,
) -> None:
    trip_id = uuid_for("trip-approval-errors")
    seed_itinerary(harness, trip_id)
    stale = approve(harness, trip_id, MEMBER_1, version=2)
    outsider = approve(harness, trip_id, OUTSIDER)
    assert stale.status_code == 409
    assert outsider.status_code == 409

    harness.client.post(
        f"/api/trips/{trip_id}/itinerary/revisions",
        json={"instruction": "Start revision"},
    )
    running = approve(harness, trip_id, MEMBER_2)
    assert running.status_code == 409


def test_production_identity_dependency_cannot_be_bypassed_by_header(
    harness: Harness,
) -> None:
    trip_id = uuid_for("trip-auth-boundary")
    seed_itinerary(harness, trip_id)
    override = app.dependency_overrides.pop(get_current_member_id)
    try:
        response = harness.client.put(
            f"/api/trips/{trip_id}/itinerary/approval/me",
            headers={"X-Member-Id": MEMBER_1},
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
                trip_context(uuid_for("trip-concurrent-generation")),
                draft(),
            )
            return str(itinerary.id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        generated_ids = list(executor.map(lambda _: generate(), range(2)))
    assert len(set(generated_ids)) == 1
    with harness.session_factory() as db:
        generation_count = db.scalar(
            select(func.count())
            .select_from(Itinerary)
            .where(Itinerary.trip_id == UUID(uuid_for("trip-concurrent-generation")))
        )
        assert generation_count == 1

    revision_trip_id = uuid_for("trip-concurrent-revision")
    seed_itinerary(harness, revision_trip_id)

    def start_revision(member_id: UUID) -> str:
        with harness.session_factory() as db:
            try:
                itinerary_service.request_revision(
                    db,
                    trip_context(revision_trip_id),
                    member_id,
                    RevisionInput(instruction=f"Request from {member_id}"),
                )
            except ItineraryConflictError:
                return "conflict"
            return "started"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(start_revision, (UUID(MEMBER_1), UUID(MEMBER_2))))
    assert sorted(outcomes) == ["conflict", "started"]

    approval_trip_id = uuid_for("trip-concurrent-approval")
    seed_itinerary(harness, approval_trip_id)

    def approve_member(member_id: UUID) -> None:
        with harness.session_factory() as db:
            itinerary_service.approve_current_version(
                db,
                trip_context(approval_trip_id),
                member_id,
                ApprovalInput(version=1, approved=True),
            )

    with ThreadPoolExecutor(max_workers=3) as executor:
        list(
            executor.map(
                approve_member,
                (UUID(MEMBER_1), UUID(MEMBER_2), UUID(MEMBER_3)),
            )
        )

    with harness.session_factory() as db:
        itinerary = itinerary_service.require_itinerary(db, approval_trip_id)
        progress = itinerary_service.get_approval_progress(
            db,
            trip_context(approval_trip_id),
        )
        assert itinerary.status == "FINALIZED"
        assert progress.approved_count == 3
        assert db.scalar(select(func.count()).select_from(ItineraryApproval)) == 3
