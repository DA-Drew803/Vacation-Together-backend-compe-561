import uuid
from collections.abc import Generator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.dependencies import get_current_trip_member
from app.main import app
from app.models.destination import Destination, DestinationVotingState
from app.models.member import TripMember
from app.models.trip import Trip
from app.services import destination as destination_service
from app.services.destination import (
    DestinationInvalidError,
    DestinationVotingConflictError,
    DestinationVotingTieError,
    close_voting,
    create_candidate,
    enrich_candidate_weather,
    get_candidates_for_trip,
    get_destination_by_name,
    start_voting,
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


def add_trip(db: Session, trip_id: uuid.UUID) -> None:
    db.add(Trip(id=trip_id, name="Test trip"))
    db.commit()


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
    trip_id = uuid.uuid4()
    member_id = uuid.uuid4()
    candidate = create_candidate(db, trip_id, "Tokyo", 0.9, "match", 950, "USD")
    add_trip(db, trip_id)
    start_voting(db, trip_id, [member_id])

    with pytest.raises(DestinationInvalidError):
        update_ballot(
            db=db,
            trip_id=trip_id,
            trip_member_id=member_id,
            candidate_ids=[
                candidate.id,
                candidate.id,
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
    add_trip(db, trip_id)
    start_voting(db, trip_id, [member_id])

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

    create_candidate(
        db=db,
        trip_id=trip_two,
        destination_name="Paris",
        match_score=0.80,
        match_explanation="Other trip",
        estimated_cost_per_person=950.00,
        currency="USD",
    )
    add_trip(db, trip_two)
    start_voting(db, trip_two, [uuid.uuid4()])

    with pytest.raises(DestinationInvalidError):
        update_ballot(
            db=db,
            trip_id=trip_two,
            trip_member_id=uuid.uuid4(),
            candidate_ids=[candidate.id],
        )


def test_voting_freezes_candidates_and_selects_by_votes_then_score(db: Session) -> None:
    trip_id = uuid.uuid4()
    member_one, member_two = uuid.uuid4(), uuid.uuid4()
    first = create_candidate(db, trip_id, "Tokyo", 0.7, "match", 950, "USD")
    second = create_candidate(db, trip_id, "Paris", 0.9, "match", 950, "USD")
    add_trip(db, trip_id)
    start_voting(db, trip_id, [member_one, member_two])

    with pytest.raises(DestinationVotingConflictError):
        create_candidate(db, trip_id, "Rome", 1.0, "late", 950, "USD")

    update_ballot(db, trip_id, member_one, [first.id])
    update_ballot(db, trip_id, member_two, [second.id])
    state = close_voting(db, trip_id, member_one)

    assert state.status == "CLOSED"
    assert state.selected_candidate_id == second.id
    trip = db.get(Trip, trip_id)
    assert trip is not None
    assert trip.state == "CLOSED"
    assert trip.voting_started_at is not None
    assert trip.voting_closed_at is not None


def test_voting_requires_all_members_and_rejects_complete_tie(db: Session) -> None:
    trip_id = uuid.uuid4()
    member_one, member_two = uuid.uuid4(), uuid.uuid4()
    first = create_candidate(db, trip_id, "Tokyo", 0.9, "match", 950, "USD")
    second = create_candidate(db, trip_id, "Paris", 0.9, "match", 950, "USD")
    add_trip(db, trip_id)
    start_voting(db, trip_id, [member_one, member_two])
    update_ballot(db, trip_id, member_one, [first.id])

    with pytest.raises(DestinationVotingConflictError):
        close_voting(db, trip_id, member_one)

    update_ballot(db, trip_id, member_two, [second.id])
    with pytest.raises(DestinationVotingTieError):
        close_voting(db, trip_id, member_one)

    state = db.get(DestinationVotingState, trip_id)
    assert state is not None
    assert state.status == "OPEN"

    state = close_voting(db, trip_id, member_two, second.id)
    assert state.status == "CLOSED"
    assert state.selected_candidate_id == second.id
    trip = db.get(Trip, trip_id)
    assert trip is not None
    assert trip.selected_destination_id == second.destination_id


def test_unique_winner_closes_without_manual_selection(db: Session) -> None:
    trip_id = uuid.uuid4()
    member_one = uuid.uuid4()
    first = create_candidate(db, trip_id, "Tokyo", 0.9, "match", 950, "USD")
    create_candidate(db, trip_id, "Paris", 0.8, "match", 950, "USD")
    add_trip(db, trip_id)
    start_voting(db, trip_id, [member_one])
    update_ballot(db, trip_id, member_one, [first.id])

    state = close_voting(db, trip_id, member_one)
    assert state.status == "CLOSED"
    assert state.selected_candidate_id == first.id


def test_candidate_creation_enriches_weather_when_dates_are_supplied(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trip_id = uuid.uuid4()
    destination = Destination(
        name="Tokyo",
        latitude=35.68,
        longitude=139.65,
    )
    db.add(destination)
    db.commit()

    expected_snapshot = {
        "data_type": "forecast",
        "source": "open-meteo",
        "days": [],
    }

    def fake_fetch_weather_snapshot(*args, **kwargs) -> dict:
        return expected_snapshot

    monkeypatch.setattr(
        destination_service,
        "fetch_weather_snapshot",
        fake_fetch_weather_snapshot,
    )

    candidate = create_candidate(
        db,
        trip_id,
        "Tokyo",
        0.8,
        "another match",
        900,
        "USD",
        weather_start_date=date(2027, 1, 1),
        weather_end_date=date(2027, 1, 2),
    )

    assert candidate.weather_snapshot == expected_snapshot
    assert candidate.recommendation_data_status == "calculated"


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

    member_id = uuid.uuid4()

    member_trip_id = uuid.uuid4()
    member_user_id = uuid.uuid4()

    def override_current_trip_member() -> TripMember:
        return TripMember(
            id=member_id,
            trip_id=member_trip_id,
            user_id=member_user_id,
            role="MEMBER",
            status="ACTIVE",
        )

    app.dependency_overrides[get_current_trip_member] = override_current_trip_member

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def test_get_candidates_rejects_invalid_trip_id(
    client: TestClient,
) -> None:
    response = client.get("/api/trips/not-a-uuid/destination_candidates")

    assert response.status_code == 400


def test_get_candidates_returns_empty_list(
    client: TestClient,
) -> None:
    trip_id = uuid.uuid4()

    response = client.get(f"/api/trips/{trip_id}/destination_candidates")

    assert response.status_code == 200
    assert response.json() == {"candidates": []}


def test_get_my_ballot_returns_empty_ballot(
    client: TestClient,
) -> None:
    trip_id = uuid.uuid4()

    response = client.get(f"/api/trips/{trip_id}/votes/me")

    assert response.status_code == 200
    assert response.json() == {"selections": []}


def test_enrich_candidate_weather_without_coordinates(db: Session) -> None:
    trip_id = uuid.uuid4()

    candidate = create_candidate(
        db=db,
        trip_id=trip_id,
        destination_name="Tokyo",
        match_score=0.91,
        match_explanation="Good match",
        estimated_cost_per_person=950,
        currency="USD",
    )

    enriched = enrich_candidate_weather(
        db,
        candidate,
        date(2027, 1, 1),
        date(2027, 1, 2),
    )

    assert enriched.weather_snapshot == {
        "data_type": "unavailable",
        "source": "open-meteo",
        "days": [],
    }
    assert enriched.recommendation_data_status == "partial"


def test_enrich_candidate_weather_with_coordinates(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trip_id = uuid.uuid4()

    candidate = create_candidate(
        db=db,
        trip_id=trip_id,
        destination_name="Tokyo",
        match_score=0.91,
        match_explanation="Good match",
        estimated_cost_per_person=950,
        currency="USD",
    )

    candidate.destination.latitude = 35.68
    candidate.destination.longitude = 139.65
    db.commit()

    expected_snapshot = {
        "data_type": "forecast",
        "source": "open-meteo",
        "days": [
            {
                "date": "2027-01-01",
                "temperature_high": 12,
                "temperature_low": 4,
                "precipitation_probability": 20,
                "weather_code": 1,
            }
        ],
    }

    def fake_fetch_weather_snapshot(
        latitude: float,
        longitude: float,
        start_date: date,
        end_date: date,
    ) -> dict:
        assert latitude == 35.68
        assert longitude == 139.65
        assert start_date == date(2027, 1, 1)
        assert end_date == date(2027, 1, 2)

        return expected_snapshot

    monkeypatch.setattr(
        destination_service,
        "fetch_weather_snapshot",
        fake_fetch_weather_snapshot,
    )

    enriched = enrich_candidate_weather(
        db,
        candidate,
        date(2027, 1, 1),
        date(2027, 1, 2),
    )

    assert enriched.weather_snapshot == expected_snapshot
    assert enriched.recommendation_data_status == "calculated"
