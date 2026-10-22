import json
import os
from collections.abc import Generator
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import app


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def request_and_log(client: TestClient, method: str, path: str, **kwargs: Any):
    response = client.request(method, path, **kwargs)
    try:
        payload = response.json()
        if isinstance(payload, dict):
            payload = {
                key: "<redacted>" if key in {"access_token", "token"} else value
                for key, value in payload.items()
            }
        output = json.dumps(payload, default=str)
    except ValueError:
        output = response.text
    print(f"{method} {path} -> {response.status_code}\n  {output}", flush=True)
    return response


def register_and_login(client: TestClient, email: str, display_name: str) -> str:
    registered = request_and_log(
        client,
        "POST",
        "/api/auth/register",
        json={
            "email": email,
            "display_name": display_name,
            "password": "password123",
        },
    )
    assert registered.status_code == 201, registered.text
    logged_in = request_and_log(
        client,
        "POST",
        "/api/auth/login",
        json={"email": email, "password": "password123"},
    )
    assert logged_in.status_code == 200, logged_in.text
    return logged_in.json()["access_token"]


def invite_and_accept(
    client: TestClient,
    trip_id: str,
    owner_token: str,
    invitee_email: str,
    invitee_token: str,
) -> None:
    invitation = request_and_log(
        client,
        "POST",
        f"/api/trips/{trip_id}/invitations",
        headers=auth_header(owner_token),
        json={"invitee_email": invitee_email},
    )
    assert invitation.status_code == 201, invitation.text
    accepted = request_and_log(
        client,
        "POST",
        f"/api/invitations/{invitation.json()['token']}/accept",
        headers=auth_header(invitee_token),
    )
    assert accepted.status_code == 201, accepted.text


def submit_logged_inputs(
    client: TestClient,
    trip_id: str,
    token: str,
    budget: int,
    interests: list[str],
) -> None:
    start_date = date.today() + timedelta(days=3)
    end_date = start_date + timedelta(days=2)
    headers = auth_header(token)
    for path, body in (
        (
            f"/api/trips/{trip_id}/availability/me",
            {
                "ranges": [
                    {
                        "start_date": start_date.isoformat(),
                        "end_date": end_date.isoformat(),
                    }
                ]
            },
        ),
        (
            f"/api/trips/{trip_id}/budget/me",
            {"max_amount": budget, "currency": "USD"},
        ),
        (
            f"/api/trips/{trip_id}/preferences/me",
            {"interests": interests, "private_notes": "live test note"},
        ),
    ):
        response = request_and_log(client, "PUT", path, headers=headers, json=body)
        assert response.status_code == 200, response.text


@pytest.fixture()
def live_ai_client() -> Generator[TestClient, None, None]:
    if os.getenv("OPENAI_LIVE_TEST") != "1":
        pytest.skip("Set OPENAI_LIVE_TEST=1 to run the live AI integration test")
    if not os.getenv("OPENAI_API_KEY", "").strip():
        pytest.skip("Set OPENAI_API_KEY to run the live AI integration test")

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
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def test_live_openai_itinerary_and_revision_cycle(live_ai_client: TestClient) -> None:
    owner_token = register_and_login(live_ai_client, "live-owner@example.com", "Live Owner")
    member_token = register_and_login(live_ai_client, "live-member@example.com", "Live Member")

    created = request_and_log(
        live_ai_client,
        "POST",
        "/api/trips",
        headers=auth_header(owner_token),
        json={"name": "Live AI Integration Trip"},
    )
    assert created.status_code == 201, created.text
    trip_id = created.json()["id"]

    invite_and_accept(
        live_ai_client,
        trip_id,
        owner_token,
        "live-member@example.com",
        member_token,
    )
    submit_logged_inputs(live_ai_client, trip_id, owner_token, 1200, ["food", "nature"])
    submit_logged_inputs(live_ai_client, trip_id, member_token, 1000, ["food", "culture"])

    candidates = request_and_log(
        live_ai_client,
        "POST",
        f"/api/trips/{trip_id}/destination_candidates/generate",
        headers=auth_header(owner_token),
    )
    assert candidates.status_code == 202, candidates.text
    candidate_id = candidates.json()[0]["id"]

    started = request_and_log(
        live_ai_client,
        "POST",
        f"/api/trips/{trip_id}/voting/start",
        headers=auth_header(owner_token),
    )
    assert started.status_code == 200, started.text

    for token in (owner_token, member_token):
        vote = request_and_log(
            live_ai_client,
            "PUT",
            f"/api/trips/{trip_id}/votes/me",
            headers=auth_header(token),
            json={"candidate_id": candidate_id},
        )
        assert vote.status_code == 200, vote.text

    closed = request_and_log(
        live_ai_client,
        "POST",
        f"/api/trips/{trip_id}/votes/close",
        headers=auth_header(owner_token),
    )
    assert closed.status_code == 200, closed.text

    initial = request_and_log(
        live_ai_client,
        "GET",
        f"/api/trips/{trip_id}/itinerary",
        headers=auth_header(owner_token),
    )
    assert initial.status_code == 200, initial.text
    initial_body = initial.json()
    assert initial_body["version"] == 1
    assert initial_body["days"]
    assert initial_body["estimated_total_cost"]["max_amount"] <= 1000

    revision = request_and_log(
        live_ai_client,
        "POST",
        f"/api/trips/{trip_id}/itinerary/revisions",
        headers=auth_header(member_token),
        json={"instruction": "Make day 1 cheaper"},
    )
    assert revision.status_code == 202, revision.text
    assert revision.json()["requested_version"] == 1

    revised = request_and_log(
        live_ai_client,
        "GET",
        f"/api/trips/{trip_id}/itinerary",
        headers=auth_header(owner_token),
    )
    assert revised.status_code == 200, revised.text
    revised_body = revised.json()
    assert revised_body["version"] == 2
    assert revised_body["days"]
    assert revised_body["estimated_total_cost"]["max_amount"] <= 1000
