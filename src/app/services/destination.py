import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from app.models.destination import (
    Destination,
    DestinationCandidate,
    DestinationVote,
)


class DestinationInvalidError(ValueError):
    pass

def _uuid(value: str | uuid.UUID, field_name: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value

    try:
        return uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise DestinationInvalidError(
            f"{field_name} must be a valid UUID"
        ) from None
    
def get_candidates_for_trip(
    db: Session,
    trip_id: str | uuid.UUID,
) -> list[DestinationCandidate]:
    statement = (
        select(DestinationCandidate)
        .where(
            DestinationCandidate.trip_id == _uuid(trip_id, "trip_id")
        )
        .options(
            selectinload(DestinationCandidate.destination)
        )
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
    return {
        "candidates": [
            serialize_candidate(candidate)
            for candidate in candidates
        ]
    }

def get_votes_for_member(
    db: Session,
    trip_member_id: str | uuid.UUID,
) -> list[DestinationVote]:
    statement = select(DestinationVote).where(
        DestinationVote.trip_member_id
        == _uuid(trip_member_id, "trip_member_id")
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
    trip_uuid = _uuid(trip_id, "trip_id")
    member_uuid = _uuid(trip_member_id, "trip_member_id")

    if len(candidate_ids) != len(set(candidate_ids)):
        raise DestinationInvalidError(
            "Ballot contains duplicate destination candidates"
        )
    
    candidates = list(
        db.scalars(
            select(DestinationCandidate).where(
                DestinationCandidate.id.in_(candidate_ids),
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
            DestinationVote.trip_member_id == member_uuid
        )
    )

    votes = [
        DestinationVote(
            trip_member_id=member_uuid,
            destination_candidate_id=candidate_id,
        )
        for candidate_id in candidate_ids
    ]

    db.add_all(votes)
    db.commit()

    return votes

def get_destination_by_name(
    db: Session,
    name: str,
) -> Destination | None:
    statement = select(Destination).where(
        Destination.name == name
    )

    return db.scalar(statement)

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
    trip_uuid = _uuid(trip_id, "trip_id")

    destination_name = destination_name.strip()
    if not destination_name:
        raise DestinationInvalidError(
            "Destination name must not be blank"
        )

    if not 0 <= match_score <= 1:
        raise DestinationInvalidError(
            "Match score must be between 0 and 1"
        )
    
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
