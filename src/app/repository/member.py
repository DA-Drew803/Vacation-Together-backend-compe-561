from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.destination import DestinationVotingState
from app.models.member import MemberPreference, TripMember
from app.models.trip import Trip
from app.models.user import User


# Find an active member for a trip.
def get_active_trip_member(
    db: Session,
    trip_id: UUID,
    user_id: UUID,
) -> TripMember | None:
    return db.scalar(
        select(TripMember).where(
            TripMember.trip_id == trip_id,
            TripMember.user_id == user_id,
            TripMember.status == "active",
        )
    )


# Get all active members in a trip.
def list_active_members(
    db: Session,
    trip_id: UUID,
) -> list[tuple[TripMember, User]]:
    return list(
        db.execute(
            select(TripMember, User)
            .join(User, User.id == TripMember.user_id)
            .where(
                TripMember.trip_id == trip_id,
                TripMember.status == "active",
            )
            .order_by(User.display_name)
        ).all()
    )


# Find a trip by its ID.
def get_trip(db: Session, trip_id: UUID) -> Trip | None:
    return db.get(Trip, trip_id)


def get_voting_state(db: Session, trip_id: UUID) -> DestinationVotingState | None:
    return db.get(DestinationVotingState, trip_id)


# Find an active member by their ID.
def get_active_member_by_id(
    db: Session,
    trip_id: UUID,
    member_id: UUID,
) -> TripMember | None:
    return db.scalar(
        select(TripMember).where(
            TripMember.id == member_id,
            TripMember.trip_id == trip_id,
            TripMember.status == "active",
        )
    )


# Get a member's preferences.
def get_preference(db: Session, trip_member_id: UUID) -> MemberPreference | None:
    return db.scalar(
        select(MemberPreference).where(MemberPreference.trip_member_id == trip_member_id)
    )


# Save a member's preferences.
def save_preference(db: Session, preference: MemberPreference) -> None:
    db.add(preference)
