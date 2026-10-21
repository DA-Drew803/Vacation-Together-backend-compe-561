from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.dependencies import get_itinerary_provider
from app.main import app
from app.schemas.itinerary import ItineraryDraft, ItineraryItemInput
from app.services import destination as destination_service


class StubItineraryProvider:
    def generate(self, context) -> ItineraryDraft:
        return ItineraryDraft(
            items=[
                ItineraryItemInput(
                    day_number=1,
                    position=1,
                    title=f"Explore {context.destination_name}",
                    estimated_cost=100,
                ),
                ItineraryItemInput(
                    day_number=2,
                    position=1,
                    title="Group activity",
                    estimated_cost=75,
                ),
            ],
            estimated_total_cost=175,
        )

    def revise(self, context, current, current_version, instruction) -> ItineraryDraft:
        del context, current_version, instruction
        return ItineraryDraft(
            items=[
                item.model_copy(
                    update={
                        "title": "Budget-friendly city walk",
                        "estimated_cost": 50,
                    }
                )
                if item.day_number == 1
                else item
                for item in current.items
            ],
            estimated_total_cost=125,
        )


@pytest.fixture()
def mvp_client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_itinerary_provider] = lambda: StubItineraryProvider()
    monkeypatch.setattr(
        destination_service,
        "fetch_weather_snapshot",
        lambda *args, **kwargs: {
            "data_type": "unavailable",
            "source": "open-meteo",
            "days": [],
        },
    )
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def register(client: TestClient, email: str, name: str) -> str:
    response = client.post(
        "/api/auth/register",
        json={"email": email, "display_name": name, "password": "password123"},
    )
    assert response.status_code == 201, response.text
    return response.json()["access_token"]


def login(client: TestClient, email: str) -> str:
    response = client.post(
        "/api/auth/login",
        json={"email": email, "password": "password123"},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def submit_inputs(client: TestClient, trip_id: str, token: str, budget: int) -> None:
    headers = auth_header(token)
    availability = client.put(
        f"/api/trips/{trip_id}/availability/me",
        headers=headers,
        json={"ranges": [{"start_date": "2026-11-10", "end_date": "2026-11-12"}]},
    )
    assert availability.status_code == 200, availability.text
    saved_budget = client.put(
        f"/api/trips/{trip_id}/budget/me",
        headers=headers,
        json={"max_amount": budget, "currency": "USD"},
    )
    assert saved_budget.status_code == 200, saved_budget.text
    preferences = client.put(
        f"/api/trips/{trip_id}/preferences/me",
        headers=headers,
        json={"interests": ["food", "nature"], "private_notes": "Do not share this"},
    )
    assert preferences.status_code == 200, preferences.text


def test_complete_nine_stage_mvp_workflow(mvp_client: TestClient) -> None:
    register(mvp_client, "owner@example.com", "Owner")
    owner_token = login(mvp_client, "owner@example.com")
    created = mvp_client.post(
        "/api/trips", headers=auth_header(owner_token), json={"name": "Fall Break"}
    )
    assert created.status_code == 201, created.text
    trip_id = created.json()["id"]

    invitation = mvp_client.post(
        f"/api/trips/{trip_id}/invitations",
        headers=auth_header(owner_token),
        json={"invitee_email": "member@example.com"},
    )
    assert invitation.status_code == 201, invitation.text
    invitation_token = invitation.json()["token"]

    register(mvp_client, "member@example.com", "Member")
    member_token = login(mvp_client, "member@example.com")
    joined = mvp_client.post(
        f"/api/invitations/{invitation_token}/accept",
        headers=auth_header(member_token),
    )
    assert joined.status_code == 201, joined.text

    submit_inputs(mvp_client, trip_id, owner_token, 900)
    submit_inputs(mvp_client, trip_id, member_token, 700)
    group_match = mvp_client.get(
        f"/api/trips/{trip_id}/group_match", headers=auth_header(owner_token)
    )
    assert group_match.status_code == 200, group_match.text
    assert group_match.json()["group_max_budget"]["max_amount"] == "700.00"
    assert group_match.json()["interests"] == ["food", "nature"]
    assert "private_notes" not in group_match.text

    generated = mvp_client.post(
        f"/api/trips/{trip_id}/destination_candidates/generate",
        headers=auth_header(owner_token),
    )
    assert generated.status_code == 202, generated.text
    candidates = generated.json()
    assert candidates
    assert all(item["estimated_cost"]["amount"] <= 700 for item in candidates)
    candidate_id = candidates[0]["id"]

    started = mvp_client.post(
        f"/api/trips/{trip_id}/voting/start", headers=auth_header(owner_token)
    )
    assert started.status_code == 200, started.text
    for token in (owner_token, member_token):
        vote = mvp_client.put(
            f"/api/trips/{trip_id}/votes/me",
            headers=auth_header(token),
            json={"candidate_id": candidate_id},
        )
        assert vote.status_code == 200, vote.text

    closed = mvp_client.post(f"/api/trips/{trip_id}/votes/close", headers=auth_header(member_token))
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "CLOSED"

    itinerary = mvp_client.get(f"/api/trips/{trip_id}/itinerary", headers=auth_header(owner_token))
    assert itinerary.status_code == 200, itinerary.text
    assert itinerary.json()["version"] == 1
    assert itinerary.json()["status"] == "DRAFT"

    revised = mvp_client.post(
        f"/api/trips/{trip_id}/itinerary/revisions",
        headers=auth_header(member_token),
        json={"instruction": "Make day 1 cheaper"},
    )
    assert revised.status_code == 202, revised.text
    assert revised.json()["status"] == "SUCCEEDED"
    current = mvp_client.get(
        f"/api/trips/{trip_id}/itinerary", headers=auth_header(owner_token)
    ).json()
    assert current["version"] == 2

    for token in (owner_token, member_token):
        approved = mvp_client.put(
            f"/api/trips/{trip_id}/itinerary/approval/me",
            headers=auth_header(token),
            json={"version": 2, "approved": True},
        )
        assert approved.status_code == 200, approved.text
    progress = mvp_client.get(
        f"/api/trips/{trip_id}/itinerary/approval", headers=auth_header(owner_token)
    )
    assert progress.json() == {
        "version": 2,
        "approved_count": 2,
        "eligible_count": 2,
        "finalized": True,
    }
    trip = mvp_client.get(f"/api/trips/{trip_id}", headers=auth_header(owner_token))
    assert trip.json()["state"] == "FINALIZED"
