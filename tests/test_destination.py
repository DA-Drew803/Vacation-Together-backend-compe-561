import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.dependencies import get_current_member_id
from app.main import app
from app.models.destination import Destination
from app.services.destination import (
    DestinationInvalidError,
    create_candidate,
    get_candidates_for_trip,
    get_destination_by_name,
    update_ballot,
)


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )

    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        yield session

def test_create_candidate(db: Session) -> None:
    trip_id = uuid.uuid4()

    candidate = create_candidate(
        db=db,
        trip_id=trip_id,
        destination_name="Tokyo",
        match_score=0.91,
        match_explanation="Matches group interests",
        estimated_cost_per_person=950.00,
        currency="USD",
    )

    assert candidate.trip_id == trip_id
    assert float(candidate.match_score) == 0.91
    assert candidate.currency == "USD"
    assert candidate.destination_id is not None

def test_get_candidates_for_trip(db: Session) -> None:
    trip_id = uuid.uuid4()

    create_candidate(
        db=db,
        trip_id=trip_id,
        destination_name="Tokyo",
        match_score=0.91,
        match_explanation="Good match",
        estimated_cost_per_person=950.00,
        currency="USD",
    )

    candidates = get_candidates_for_trip(db, trip_id)

    assert len(candidates) == 1
    assert candidates[0].destination.name == "Tokyo"

def test_create_candidate_reuses_destination(db: Session) -> None:
    trip_one = uuid.uuid4()
    trip_two = uuid.uuid4()

    create_candidate(
        db=db,
        trip_id=trip_one,
        destination_name="Tokyo",
        match_score=0.90,
        match_explanation="Good match",
        estimated_cost_per_person=900.00,
        currency="USD",
    )

    create_candidate(
        db=db,
        trip_id=trip_two,
        destination_name="Tokyo",
        match_score=0.80,
        match_explanation="Another good match",
        estimated_cost_per_person=1000.00,
        currency="USD",
    )

    destination = get_destination_by_name(db, "Tokyo")

    assert destination is not None
    assert db.query(Destination).count() == 1

def test_create_candidate_rejects_invalid_score(db: Session) -> None:
    with pytest.raises(DestinationInvalidError):
        create_candidate(
            db=db,
            trip_id=uuid.uuid4(),
            destination_name="Tokyo",
            match_score=1.5,
            match_explanation="Invalid score",
            estimated_cost_per_person=950.00,
            currency="USD",
        )

def test_update_ballot_rejects_duplicate_candidates(db: Session) -> None:
    candidate_id = uuid.uuid4()

    with pytest.raises(DestinationInvalidError):
        update_ballot(
            db=db,
            trip_id=uuid.uuid4(),
            trip_member_id=uuid.uuid4(),
            candidate_ids=[
                candidate_id,
                candidate_id,
            ],
        )

def test_update_ballot_success(db: Session) -> None:
    trip_id = uuid.uuid4()
    member_id = uuid.uuid4()

    candidate = create_candidate(
        db=db,
        trip_id=trip_id,
        destination_name="Tokyo",
        match_score=0.91,
        match_explanation="Good match",
        estimated_cost_per_person=950.00,
        currency="USD",
    )

    votes = update_ballot(
        db=db,
        trip_id=trip_id,
        trip_member_id=member_id,
        candidate_ids=[candidate.id],
    )

    assert len(votes) == 1
    assert votes[0].trip_member_id == member_id
    assert votes[0].destination_candidate_id == candidate.id

def test_update_ballot_rejects_candidate_from_another_trip(
    db: Session,
) -> None:
    trip_one = uuid.uuid4()
    trip_two = uuid.uuid4()

    candidate = create_candidate(
        db=db,
        trip_id=trip_one,
        destination_name="Tokyo",
        match_score=0.91,
        match_explanation="Good match",
        estimated_cost_per_person=950.00,
        currency="USD",
    )

    with pytest.raises(DestinationInvalidError):
        update_ballot(
            db=db,
            trip_id=trip_two,
            trip_member_id=uuid.uuid4(),
            candidate_ids=[candidate.id],
        )

@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    Base.metadata.create_all(bind=engine)

    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async def override_current_member() -> str:
        return str(uuid.uuid4())
    app.dependency_overrides[get_current_member_id] = override_current_member

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()

def test_get_candidates_rejects_invalid_trip_id(
    client: TestClient,
) -> None:
    response = client.get(
        "/api/trips/not-a-uuid/destination_candidates"
    )

    assert response.status_code == 400

def test_get_candidates_returns_empty_list(
    client: TestClient,
) -> None:
    trip_id = uuid.uuid4()

    response = client.get(
        f"/api/trips/{trip_id}/destination_candidates"
    )

    assert response.status_code == 200
    assert response.json() == {
        "candidates": []
    }

def test_get_my_ballot_returns_empty_ballot(
    client: TestClient,
) -> None:
    trip_id = uuid.uuid4()

    response = client.get(
        f"/api/trips/{trip_id}/votes/me"
    )

    assert response.status_code == 200
    assert response.json() == {
        "selections": []
    }

    