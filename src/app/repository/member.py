from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.member import TripMember


def get_trip_member(db: Session, member_id: UUID) -> TripMember | None:
    statement = select(TripMember).where(TripMember.id == member_id)
    return db.scalar(statement)


def get_trip_member_by_user(
    db: Session,
    trip_id: UUID,
    user_id: UUID,
) -> TripMember | None:
    statement = select(TripMember).where(
        TripMember.trip_id == trip_id,
        TripMember.user_id == user_id,
    )
    return db.scalar(statement)


def list_trip_members(db: Session, trip_id: UUID) -> list[TripMember]:
    statement = (
        select(TripMember).where(TripMember.trip_id == trip_id).order_by(TripMember.joined_at)
    )
    return list(db.scalars(statement).all())


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
