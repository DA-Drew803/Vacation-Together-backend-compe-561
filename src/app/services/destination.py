import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from app.models.destination import (
    Destination,
    DestinationCandidate,
    DestinationVote,
    DestinationVotingState,
)
from app.models.trip import Trip
from app.services import member as member_service
from app.services.weather import fetch_weather_snapshot

CURATED_DESTINATIONS = (
    {
        "name": "San Diego",
        "location": "California, USA",
        "baseline_cost": Decimal("700.00"),
        "tags": ["beach", "food", "outdoors", "relaxation"],
        "country_code": "US",
        "latitude": Decimal("32.715736"),
        "longitude": Decimal("-117.161087"),
    },
    {
        "name": "Portland",
        "location": "Oregon, USA",
        "baseline_cost": Decimal("600.00"),
        "tags": ["food", "nature", "arts", "outdoors"],
        "country_code": "US",
        "latitude": Decimal("45.515232"),
        "longitude": Decimal("-122.678385"),
    },
    {
        "name": "Seattle",
        "location": "Washington, USA",
        "baseline_cost": Decimal("850.00"),
        "tags": ["food", "museums", "nature", "city"],
        "country_code": "US",
        "latitude": Decimal("47.606209"),
        "longitude": Decimal("-122.332069"),
    },
    {
        "name": "Las Vegas",
        "location": "Nevada, USA",
        "baseline_cost": Decimal("650.00"),
        "tags": ["food", "nightlife", "entertainment", "city"],
        "country_code": "US",
        "latitude": Decimal("36.169941"),
        "longitude": Decimal("-115.139832"),
    },
    {
        "name": "Denver",
        "location": "Colorado, USA",
        "baseline_cost": Decimal("800.00"),
        "tags": ["nature", "outdoors", "food", "adventure"],
        "country_code": "US",
        "latitude": Decimal("39.739236"),
        "longitude": Decimal("-104.990251"),
    },
)


class DestinationInvalidError(ValueError):
    pass


class DestinationVotingConflictError(DestinationInvalidError):
    pass


class DestinationVotingTieError(DestinationVotingConflictError):
    pass


def parse_uuid(value: str | uuid.UUID, field_name: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value

    try:
        return uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise DestinationInvalidError(f"{field_name} must be a valid UUID") from None


def get_candidates_for_trip(
    db: Session,
    trip_id: str | uuid.UUID,
) -> list[DestinationCandidate]:
    statement = (
        select(DestinationCandidate)
        .where(DestinationCandidate.trip_id == parse_uuid(trip_id, "trip_id"))
        .options(selectinload(DestinationCandidate.destination))
    )
    return list(db.scalars(statement).all())


def serialize_candidate(candidate: DestinationCandidate) -> dict:
    return {
        "id": candidate.id,
        "destination": {
            "id": candidate.destination.id,
            "name": candidate.destination.name,
        },
        "match_score": float(candidate.match_score),
        "match_explanation": candidate.match_explanation,
        "estimated_cost_per_person": {
            "amount": float(candidate.estimated_cost_per_person),
            "currency": candidate.currency,
        },
        "recommendation_data_status": candidate.recommendation_data_status,
        "weather": candidate.weather_snapshot,
    }


def serialize_candidate_contract(candidate: DestinationCandidate) -> dict:
    return {
        "id": candidate.id,
        "destination_id": candidate.destination_id,
        "name": candidate.destination.name,
        "match_score": float(candidate.match_score),
        "estimated_cost": {
            "amount": float(candidate.estimated_cost_per_person),
            "currency": candidate.currency,
        },
        "weather": candidate.weather_snapshot,
        "reason": candidate.match_explanation,
    }


def serialize_candidates(
    candidates: list[DestinationCandidate],
) -> dict:
    return {"candidates": [serialize_candidate(candidate) for candidate in candidates]}


def generate_candidates(db: Session, trip_id: uuid.UUID) -> list[DestinationCandidate]:
    if db.scalar(select(DestinationVotingState).where(DestinationVotingState.trip_id == trip_id)):
        raise DestinationVotingConflictError("Candidates cannot change after voting starts")
    match = member_service.get_group_match(db, trip_id)
    budget = Decimal(match["budget_amount"])
    interests = set(match["interests"])
    db.execute(delete(DestinationCandidate).where(DestinationCandidate.trip_id == trip_id))
    generated: list[DestinationCandidate] = []
    for item in CURATED_DESTINATIONS:
        if item["baseline_cost"] > budget:
            continue
        destination = db.scalar(select(Destination).where(Destination.name == item["name"]))
        if destination is None:
            destination = Destination(**item)
            db.add(destination)
            db.flush()
        overlap = interests.intersection(destination.tags)
        score = Decimal(len(overlap)) / Decimal(max(len(interests), 1))
        candidate = DestinationCandidate(
            trip_id=trip_id,
            destination_id=destination.id,
            match_score=score.quantize(Decimal("0.0001")),
            match_explanation=(
                "Matches group interests: " + ", ".join(sorted(overlap))
                if overlap
                else "Fits the group budget"
            ),
            estimated_cost_per_person=destination.baseline_cost,
            currency=match["currency"],
            recommendation_data_status="calculated",
        )
        db.add(candidate)
        db.flush()
        candidate.destination = destination
        enrich_candidate_weather(
            db, candidate, match["start_date"], match["end_date"], commit=False
        )
        generated.append(candidate)
    if not generated:
        db.rollback()
        raise DestinationVotingConflictError("No curated destinations fit the group budget")
    db.commit()
    return sorted(generated, key=lambda value: (-value.match_score, str(value.destination_id)))


def get_votes_for_member(
    db: Session,
    trip_id: str | uuid.UUID,
    trip_member_id: str | uuid.UUID,
) -> list[DestinationVote]:
    statement = select(DestinationVote).where(
        DestinationVote.trip_id == parse_uuid(trip_id, "trip_id"),
        DestinationVote.trip_member_id == parse_uuid(trip_member_id, "trip_member_id"),
    )

    return list(db.scalars(statement).all())


def serialize_ballot(
    votes: list[DestinationVote],
) -> dict:
    return {
        "selections": [
            {
                "destination_candidate_id": vote.destination_candidate_id,
            }
            for vote in votes
        ]
    }


def serialize_vote_contract(votes: list[DestinationVote], member_id: uuid.UUID) -> dict:
    return {
        "candidate_id": votes[0].destination_candidate_id if votes else None,
        "member_id": member_id,
    }


def get_voting_progress(db: Session, trip_id: uuid.UUID) -> dict:
    state = db.scalar(
        select(DestinationVotingState).where(DestinationVotingState.trip_id == trip_id)
    )
    if state is None:
        return {
            "status": "NOT_STARTED",
            "eligible_count": 0,
            "voted_count": 0,
            "selected_destination_id": None,
            "tallies": {},
        }
    votes = list(db.scalars(select(DestinationVote).where(DestinationVote.trip_id == trip_id)))
    tallies = {candidate_id: 0 for candidate_id in state.frozen_candidate_ids}
    for vote in votes:
        key = str(vote.destination_candidate_id)
        if key in tallies:
            tallies[key] += 1
    selected_destination_id = None
    if state.selected_candidate_id is not None:
        candidate = db.get(DestinationCandidate, state.selected_candidate_id)
        selected_destination_id = candidate.destination_id if candidate else None
    return {
        "status": state.status,
        "eligible_count": len(state.frozen_member_ids),
        "voted_count": len({str(vote.trip_member_id) for vote in votes}),
        "selected_destination_id": selected_destination_id,
        "tallies": tallies,
    }


def update_ballot(
    db: Session,
    trip_id: str | uuid.UUID,
    trip_member_id: str | uuid.UUID,
    candidate_ids: list[uuid.UUID],
) -> list[DestinationVote]:
    trip_uuid = parse_uuid(trip_id, "trip_id")
    member_uuid = parse_uuid(trip_member_id, "trip_member_id")

    state = db.scalar(
        select(DestinationVotingState)
        .where(DestinationVotingState.trip_id == trip_uuid)
        .with_for_update()
    )
    if state is None or state.status != "OPEN":
        raise DestinationVotingConflictError("Voting is not open for this trip")
    if str(member_uuid) not in state.frozen_member_ids:
        raise DestinationVotingConflictError("Member is not eligible to vote")

    if len(candidate_ids) != 1:
        raise DestinationInvalidError("A ballot must contain exactly one candidate")

    if len(candidate_ids) != len(set(candidate_ids)):
        raise DestinationInvalidError("Ballot contains duplicate destination candidates")

    candidate_uuid = parse_uuid(candidate_ids[0], "destination_candidate_id")
    if str(candidate_uuid) not in state.frozen_candidate_ids:
        raise DestinationVotingConflictError("Candidate was not frozen for this vote")

    candidates = list(
        db.scalars(
            select(DestinationCandidate).where(
                DestinationCandidate.id == candidate_uuid,
                DestinationCandidate.trip_id == trip_uuid,
            )
        ).all()
    )

    if len(candidates) != len(candidate_ids):
        raise DestinationInvalidError(
            "One or more destination candidates are not valid for this trip"
        )

    db.execute(
        delete(DestinationVote).where(
            DestinationVote.trip_id == trip_uuid,
            DestinationVote.trip_member_id == member_uuid,
        )
    )

    votes = [
        DestinationVote(
            trip_id=trip_uuid,
            trip_member_id=member_uuid,
            destination_candidate_id=candidate_uuid,
        )
        for candidate_id in candidate_ids
    ]

    db.add_all(votes)
    db.commit()

    return votes


def start_voting(
    db: Session,
    trip_id: str | uuid.UUID,
    eligible_member_ids: list[str | uuid.UUID],
) -> DestinationVotingState:
    """Freeze authoritative membership and candidates at voting start.

    The caller must obtain member IDs from persisted TripMember rows; this service
    deliberately has no client-facing endpoint for supplying them.
    """
    trip_uuid = parse_uuid(trip_id, "trip_id")
    member_ids = [parse_uuid(value, "trip_member_id") for value in eligible_member_ids]
    if not member_ids or len(member_ids) != len(set(member_ids)):
        raise DestinationVotingConflictError("Voting requires a non-empty member set")
    existing = db.scalar(
        select(DestinationVotingState).where(DestinationVotingState.trip_id == trip_uuid)
    )
    if existing is not None:
        raise DestinationVotingConflictError("Voting has already started")
    candidates = get_candidates_for_trip(db, trip_uuid)
    if not candidates:
        raise DestinationVotingConflictError("Voting requires destination candidates")
    trip = db.scalar(select(Trip).where(Trip.id == trip_uuid).with_for_update())
    if trip is None:
        raise DestinationVotingConflictError("Trip does not exist")
    if trip.state not in {"PLANNING", "VOTING"}:
        raise DestinationVotingConflictError("Voting cannot start for this trip")
    state = DestinationVotingState(
        trip_id=trip_uuid,
        status="OPEN",
        frozen_member_ids=[str(value) for value in member_ids],
        frozen_candidate_ids=[str(candidate.id) for candidate in candidates],
    )
    trip.state = "VOTING"
    trip.voting_started_at = datetime.now(UTC)
    db.add(state)
    db.commit()
    db.refresh(state)
    return state


def close_voting(
    db: Session,
    trip_id: str | uuid.UUID,
    closing_member_id: str | uuid.UUID,
    selected_candidate_id: str | uuid.UUID | None = None,
) -> DestinationVotingState:
    trip_uuid = parse_uuid(trip_id, "trip_id")
    member_uuid = parse_uuid(closing_member_id, "trip_member_id")
    state = db.scalar(
        select(DestinationVotingState)
        .where(DestinationVotingState.trip_id == trip_uuid)
        .with_for_update()
    )
    if state is None:
        raise DestinationVotingConflictError("Voting is not open for this trip")
    if str(member_uuid) not in state.frozen_member_ids:
        raise DestinationVotingConflictError("Member is not eligible to close voting")
    if state.status == "CLOSED":
        return state
    if state.status != "OPEN":
        raise DestinationVotingConflictError("Voting is not open for this trip")
    votes = list(
        db.scalars(
            select(DestinationVote).where(
                DestinationVote.trip_id == trip_uuid,
                DestinationVote.destination_candidate_id.in_(
                    [
                        parse_uuid(value, "destination_candidate_id")
                        for value in state.frozen_candidate_ids
                    ]
                ),
            )
        ).all()
    )
    voted_members = {str(vote.trip_member_id) for vote in votes}
    if voted_members != set(state.frozen_member_ids):
        raise DestinationVotingConflictError("Every eligible member must vote before closure")

    candidates = list(
        db.scalars(
            select(DestinationCandidate).where(
                DestinationCandidate.id.in_(
                    [
                        parse_uuid(value, "destination_candidate_id")
                        for value in state.frozen_candidate_ids
                    ]
                ),
                DestinationCandidate.trip_id == trip_uuid,
            )
        ).all()
    )
    vote_counts: dict[uuid.UUID, int] = {candidate.id: 0 for candidate in candidates}
    for vote in votes:
        vote_counts[vote.destination_candidate_id] += 1
    ranked = sorted(
        candidates,
        key=lambda candidate: (vote_counts[candidate.id], Decimal(candidate.match_score)),
        reverse=True,
    )
    top_vote_count = vote_counts[ranked[0].id]
    top_score = Decimal(ranked[0].match_score)
    final_tied = [
        candidate
        for candidate in ranked
        if vote_counts[candidate.id] == top_vote_count
        and Decimal(candidate.match_score) == top_score
    ]
    if selected_candidate_id is not None:
        raise DestinationVotingConflictError("Voting results cannot be manually overridden")
    top = min(final_tied, key=lambda candidate: str(candidate.destination_id))
    trip = db.scalar(select(Trip).where(Trip.id == trip_uuid).with_for_update())
    if trip is None:
        raise DestinationVotingConflictError("Trip does not exist")
    if trip.selected_destination_id is not None:
        if trip.selected_destination_id != top.destination_id:
            raise DestinationVotingConflictError("Trip already has a different destination")
        raise DestinationVotingConflictError("Trip already has a selected destination")
    state.status = "CLOSED"
    state.selected_candidate_id = top.id
    state.closed_by_member_id = member_uuid
    state.closed_at = datetime.now(UTC)
    trip.selected_destination_id = top.destination_id
    trip.voting_closed_at = state.closed_at
    if trip.state in {"PLANNING", "VOTING"}:
        trip.state = "CLOSED"
    db.commit()
    db.refresh(state)
    return state


def get_destination_by_name(
    db: Session,
    name: str,
) -> Destination | None:
    statement = select(Destination).where(Destination.name == name)

    return db.scalar(statement)


def enrich_candidate_weather(
    db: Session,
    candidate: DestinationCandidate,
    start_date: date,
    end_date: date,
    *,
    commit: bool = True,
) -> DestinationCandidate:
    destination = candidate.destination

    if destination.latitude is None or destination.longitude is None:
        candidate.weather_snapshot = {
            "data_type": "unavailable",
            "source": "open-meteo",
            "days": [],
        }
        candidate.recommendation_data_status = "partial"
    else:
        candidate.weather_snapshot = fetch_weather_snapshot(
            float(destination.latitude),
            float(destination.longitude),
            start_date,
            end_date,
        )

        if candidate.weather_snapshot["data_type"] == "unavailable":
            candidate.recommendation_data_status = "partial"
        else:
            candidate.recommendation_data_status = "calculated"

    if commit:
        db.commit()
        db.refresh(candidate)

    return candidate


def create_candidate(
    db: Session,
    trip_id: str | uuid.UUID,
    destination_name: str,
    match_score: float,
    match_explanation: str,
    estimated_cost_per_person: float,
    currency: str,
    weather_snapshot: dict | None = None,
    recommendation_data_status: str = "pending",
    weather_start_date: date | None = None,
    weather_end_date: date | None = None,
) -> DestinationCandidate:
    trip_uuid = parse_uuid(trip_id, "trip_id")

    state = db.scalar(
        select(DestinationVotingState).where(DestinationVotingState.trip_id == trip_uuid)
    )
    if state is not None:
        raise DestinationVotingConflictError("Candidates cannot change after voting starts")

    destination_name = destination_name.strip()
    if not destination_name:
        raise DestinationInvalidError("Destination name must not be blank")

    if not 0 <= match_score <= 1:
        raise DestinationInvalidError("Match score must be between 0 and 1")

    if estimated_cost_per_person < 0:
        raise DestinationInvalidError("Estimated cost must not be negative")
    if len(currency) != 3 or not currency.isalpha():
        raise DestinationInvalidError("Currency must be a three-letter code")

    destination = get_destination_by_name(
        db,
        destination_name,
    )

    if destination is None:
        destination = Destination(
            name=destination_name,
        )
    db.add(destination)
    db.flush()

    candidate = DestinationCandidate(
        trip_id=trip_uuid,
        destination_id=destination.id,
        match_score=match_score,
        match_explanation=match_explanation,
        estimated_cost_per_person=estimated_cost_per_person,
        currency=currency,
        weather_snapshot=weather_snapshot,
        recommendation_data_status=recommendation_data_status,
    )

    db.add(candidate)
    if (weather_start_date is None) != (weather_end_date is None):
        raise DestinationInvalidError("Both weather start and end dates are required")
    if weather_snapshot is None and weather_start_date is not None:
        db.flush()
        enrich_candidate_weather(
            db,
            candidate,
            weather_start_date,
            weather_end_date,
        )
        return candidate
    db.commit()
    db.refresh(candidate)

    return candidate
