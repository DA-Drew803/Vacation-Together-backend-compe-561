from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.destination import DestinationVotingState
from app.models.member import MemberAvailability, MemberBudget, MemberPreference, TripMember
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
            TripMember.status == "ACTIVE",
        )
    )


def get_trip_member_by_user(
    db: Session,
    trip_id: UUID,
    user_id: UUID,
) -> TripMember | None:
    """Return a member record for a user, including inactive records."""
    return db.scalar(
        select(TripMember).where(
            TripMember.trip_id == trip_id,
            TripMember.user_id == user_id,
        )
    )


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
                TripMember.status == "ACTIVE",
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
            TripMember.status == "ACTIVE",
        )
    )


def get_preference(db: Session, trip_member_id: UUID) -> MemberPreference | None:
    return db.get(MemberPreference, trip_member_id)


def save_preference(db: Session, preference: MemberPreference) -> None:
    db.add(preference)


def list_availability(db: Session, trip_member_id: UUID) -> list[MemberAvailability]:
    return list(
        db.scalars(
            select(MemberAvailability)
            .where(MemberAvailability.trip_member_id == trip_member_id)
            .order_by(MemberAvailability.start_date, MemberAvailability.end_date)
        ).all()
    )


def delete_availability(db: Session, ranges: list[MemberAvailability]) -> None:
    for availability in ranges:
        db.delete(availability)


def add_availability(db: Session, ranges: list[MemberAvailability]) -> None:
    db.add_all(ranges)


def get_budget(db: Session, trip_member_id: UUID) -> MemberBudget | None:
    return db.get(MemberBudget, trip_member_id)


def save_budget(db: Session, budget: MemberBudget) -> None:
    db.add(budget)


def create_trip_member(
    db: Session,
    trip_id: UUID,
    user_id: UUID,
    role: str,
) -> TripMember:
    member = TripMember(
        trip_id=trip_id,
        user_id=user_id,
        role=role,
        status="ACTIVE",
        voting_eligible=True,
    )
    db.add(member)
    return member


def save_trip_member(db: Session, member: TripMember) -> TripMember:
    db.commit()
    db.refresh(member)
    return member


def delete_trip_member(db: Session, member: TripMember) -> None:
    db.delete(member)
    db.commit()
