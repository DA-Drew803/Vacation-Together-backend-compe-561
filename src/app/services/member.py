from uuid import UUID

from sqlalchemy.orm import Session

from app.models.member import TripMember
from app.repository import member as member_repository


def get_trip_member(db: Session, member_id: UUID) -> TripMember | None:
    return member_repository.get_trip_member(db, member_id)


def get_trip_member_by_user(
    db: Session,
    trip_id: UUID,
    user_id: UUID,
) -> TripMember | None:
    return member_repository.get_trip_member_by_user(db, trip_id, user_id)


def list_trip_members(db: Session, trip_id: UUID) -> list[TripMember]:
    return member_repository.list_trip_members(db, trip_id)


def add_trip_member(
    db: Session,
    trip_id: UUID,
    user_id: UUID,
    role: str = "MEMBER",
) -> TripMember:
    existing_member = member_repository.get_trip_member_by_user(db, trip_id, user_id)
    if existing_member is not None:
        raise ValueError("This user is already a member of the trip")

    member = member_repository.create_trip_member(db, trip_id, user_id, role)
    return member_repository.save_trip_member(db, member)


def update_trip_member(
    db: Session,
    member: TripMember,
    role: str,
) -> TripMember:
    member.role = role
    return member_repository.save_trip_member(db, member)


def remove_trip_member(db: Session, member: TripMember) -> None:
    member_repository.delete_trip_member(db, member)
