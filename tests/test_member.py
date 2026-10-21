from collections.abc import Generator
from typing import Annotated
from uuid import UUID, uuid4

import pytest
from fastapi import Header
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, configure_mappers, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.dependencies import get_current_member_id
from app.main import app
from app.models.destination import DestinationVotingState
from app.models.member import MemberPreference, TripMember
from app.models.trip import Trip
from app.models.user import User


@pytest.fixture()
def member_api() -> Generator[tuple[TestClient, sessionmaker[Session], dict[str, UUID]], None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    sessions = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)

    ids = {
        "trip": uuid4(),
        "other_trip": uuid4(),
        "owner_user": uuid4(),
        "member_user": uuid4(),
        "outsider_user": uuid4(),
        "owner_member": uuid4(),
        "member_member": uuid4(),
        "other_member": uuid4(),
    }
    with sessions() as db:
        db.add_all(
            [
                User(
                    id=ids["owner_user"],
                    email="owner@example.com",
                    display_name="Owner",
                    password_hash="not-a-real-password-hash",
                ),
                User(
                    id=ids["member_user"],
                    email="member@example.com",
                    display_name="Member",
                    password_hash="not-a-real-password-hash",
                ),
                User(
                    id=ids["outsider_user"],
                    email="outsider@example.com",
                    display_name="Outsider",
                    password_hash="not-a-real-password-hash",
                ),
                Trip(id=ids["trip"], name="Shared trip"),
                Trip(id=ids["other_trip"], name="Other trip"),
                TripMember(
                    id=ids["owner_member"],
                    trip_id=ids["trip"],
                    user_id=ids["owner_user"],
                    role="OWNER",
                ),
                TripMember(
                    id=ids["member_member"],
                    trip_id=ids["trip"],
                    user_id=ids["member_user"],
                    role="MEMBER",
                ),
                TripMember(
                    id=ids["other_member"],
                    trip_id=ids["other_trip"],
                    user_id=ids["outsider_user"],
                    role="OWNER",
                ),
            ]
        )
        db.commit()

    def override_get_db() -> Generator[Session, None, None]:
        with sessions() as db:
            yield db

    async def override_current_user(
        user_id: Annotated[str | None, Header(alias="X-Test-User-Id")] = None,
    ) -> UUID:
        return UUID(user_id) if user_id else ids["owner_user"]

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_member_id] = override_current_user
    with TestClient(app) as client:
        yield client, sessions, ids

    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_member_mappers_and_tables_match_mvp_scope() -> None:
    configure_mappers()

    assert "trip_members" in Base.metadata.tables
    assert "member_preferences" in Base.metadata.tables
    assert {"member_availability", "member_budgets"} <= set(Base.metadata.tables)


def test_one_preference_per_member_is_enforced(
    member_api: tuple[TestClient, sessionmaker[Session], dict[str, UUID]],
) -> None:
    _, sessions, ids = member_api
    with sessions() as db:
        db.add_all(
            [
                MemberPreference(trip_member_id=ids["member_member"], interests=["food"]),
                MemberPreference(trip_member_id=ids["member_member"], interests=["nature"]),
            ]
        )
        with pytest.raises(IntegrityError):
            db.commit()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/trips/{trip}/members"),
        ("delete", "/api/trips/{trip}/members/{member}"),
        ("get", "/api/trips/{trip}/preferences/me"),
        ("put", "/api/trips/{trip}/preferences/me"),
        ("get", "/api/trips/{trip}/availability/me"),
        ("put", "/api/trips/{trip}/availability/me"),
        ("get", "/api/trips/{trip}/budget/me"),
        ("put", "/api/trips/{trip}/budget/me"),
    ],
)
def test_member_routes_preserve_authentication_503_boundary(
    member_api: tuple[TestClient, sessionmaker[Session], dict[str, UUID]],
    method: str,
    path: str,
) -> None:
    client, _, ids = member_api
    override = app.dependency_overrides.pop(get_current_member_id)
    try:
        response = client.request(
            method,
            path.format(trip=ids["trip"], member=ids["member_member"]),
            json=(
                {"interests": []}
                if "preferences" in path
                else {"ranges": [{"start_date": "2027-01-01", "end_date": "2027-01-02"}]}
                if "availability" in path
                else {"max_amount": 100, "currency": "USD"}
            ) if method == "put" else None,
        )
    finally:
        app.dependency_overrides[get_current_member_id] = override

    assert response.status_code == 503


def test_member_add_route_is_not_exposed(
    member_api: tuple[TestClient, sessionmaker[Session], dict[str, UUID]],
) -> None:
    client, _, ids = member_api
    response = client.post(
        f"/api/trips/{ids['trip']}/members",
        json={"user_id": str(ids["outsider_user"]), "role": "OWNER"},
    )

    assert response.status_code == 405


def test_membership_authorization_and_private_preferences(
    member_api: tuple[TestClient, sessionmaker[Session], dict[str, UUID]],
) -> None:
    client, _, ids = member_api
    outsider_headers = {"X-Test-User-Id": str(ids["outsider_user"])}
    assert client.get(f"/api/trips/{ids['trip']}/members", headers=outsider_headers).status_code == 403

    member_headers = {"X-Test-User-Id": str(ids["member_user"])}
    saved = client.put(
        f"/api/trips/{ids['trip']}/preferences/me",
        headers=member_headers,
        json={"interests": ["Food", "food"], "private_notes": "Private idea"},
    )
    assert saved.status_code == 200
    assert saved.json()["interests"] == ["food"]

    availability = client.put(
        f"/api/trips/{ids['trip']}/availability/me",
        headers=member_headers,
        json={"ranges": [{"start_date": "2027-06-10", "end_date": "2027-06-17"}]},
    )
    assert availability.status_code == 200
    assert availability.json()["ranges"][0]["start_date"] == "2027-06-10"

    budget = client.put(
        f"/api/trips/{ids['trip']}/budget/me",
        headers=member_headers,
        json={"max_amount": 950, "currency": "usd"},
    )
    assert budget.status_code == 200
    assert budget.json()["currency"] == "USD"

    owner_preferences = client.get(f"/api/trips/{ids['trip']}/preferences/me")
    assert owner_preferences.status_code == 404

    members = client.get(f"/api/trips/{ids['trip']}/members")
    assert members.status_code == 200
    assert len(members.json()) == 2
    assert "private_notes" not in str(members.json())
    assert "password_hash" not in str(members.json())


def test_removal_rejects_cross_trip_target_and_voting_freeze(
    member_api: tuple[TestClient, sessionmaker[Session], dict[str, UUID]],
) -> None:
    client, sessions, ids = member_api
    cross_trip = client.delete(
        f"/api/trips/{ids['trip']}/members/{ids['other_member']}"
    )
    assert cross_trip.status_code == 404

    with sessions() as db:
        db.add(
            DestinationVotingState(
                trip_id=ids["trip"],
                status="OPEN",
                frozen_member_ids=[str(ids["owner_member"]), str(ids["member_member"])],
                frozen_candidate_ids=[str(uuid4())],
            )
        )
        db.commit()

    frozen = client.delete(
        f"/api/trips/{ids['trip']}/members/{ids['member_member']}"
    )
    assert frozen.status_code == 409

    for path, payload in (
        (
            "availability/me",
            {"ranges": [{"start_date": "2027-01-01", "end_date": "2027-01-02"}]},
        ),
        ("budget/me", {"max_amount": 100, "currency": "USD"}),
        ("preferences/me", {"interests": ["food"], "private_notes": "changed"}),
    ):
        response = client.put(
            f"/api/trips/{ids['trip']}/{path}",
            headers={"X-Test-User-Id": str(ids["owner_user"])},
            json=payload,
        )
        assert response.status_code == 409
