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
from app.services.weather import fetch_weather_snapshot


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


def serialize_candidates(
    candidates: list[DestinationCandidate],
) -> dict:
    return {"candidates": [serialize_candidate(candidate) for candidate in candidates]}


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
    state = DestinationVotingState(
        trip_id=trip_uuid,
        status="OPEN",
        frozen_member_ids=[str(value) for value in member_ids],
        frozen_candidate_ids=[str(candidate.id) for candidate in candidates],
    )
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
    if len(final_tied) > 1:
        if selected_candidate_id is None:
            raise DestinationVotingTieError(
                "Voting is tied; a final tied candidate must be selected"
            )
        selected_uuid = parse_uuid(selected_candidate_id, "selected_candidate_id")
        tied_ids = {candidate.id for candidate in final_tied}
        if selected_uuid not in tied_ids:
            raise DestinationVotingTieError(
                "Selected candidate is not in the final tied candidate set"
            )
        top = next(candidate for candidate in final_tied if candidate.id == selected_uuid)
    else:
        if selected_candidate_id is not None:
            raise DestinationVotingConflictError(
                "Manual selection is allowed only for a complete final tie"
            )
        top = final_tied[0]
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
    db.commit()
    db.refresh(candidate)

    return candidate
