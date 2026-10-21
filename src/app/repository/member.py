from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

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
                TripMember.status == "ACTIVE",
            )
            .order_by(User.display_name)
        ).all()
    )


# Find a trip by its ID.
def get_trip(db: Session, trip_id: UUID) -> Trip | None:
    return db.get(Trip, trip_id)


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


# Get a member's availability ranges.
def list_availability(
    db: Session,
    trip_member_id: UUID,
) -> list[MemberAvailability]:
    return list(
        db.scalars(
            select(MemberAvailability)
            .where(MemberAvailability.trip_member_id == trip_member_id)
            .order_by(MemberAvailability.start_date, MemberAvailability.end_date)
        ).all()
    )


# Delete availability ranges.
def delete_availability(db: Session, ranges: list[MemberAvailability]) -> None:
    for availability in ranges:
        db.delete(availability)


# Add availability ranges.
def add_availability(db: Session, ranges: list[MemberAvailability]) -> None:
    db.add_all(ranges)


# Get a member's budget.
def get_budget(db: Session, trip_member_id: UUID) -> MemberBudget | None:
    return db.get(MemberBudget, trip_member_id)


# Save a member's budget.
def save_budget(db: Session, budget: MemberBudget) -> None:
    db.add(budget)


# Get a member's preferences.
def get_preference(db: Session, trip_member_id: UUID) -> MemberPreference | None:
    return db.get(MemberPreference, trip_member_id)


# Save a member's preferences.
def save_preference(db: Session, preference: MemberPreference) -> None:
    db.add(preference)
