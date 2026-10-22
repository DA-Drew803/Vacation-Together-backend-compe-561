from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.member import TripMember
from app.models.trip import Trip
from app.repository import member as member_repository


def get_trip(db: Session, trip_id: UUID) -> Trip | None:
    statement = select(Trip).where(Trip.id == trip_id)
    return db.scalar(statement)


def list_trips(db: Session) -> list[Trip]:
    statement = select(Trip).order_by(Trip.name)
    return list(db.scalars(statement).all())


def create_trip(db: Session, name: str, creator_id: UUID) -> Trip:
    try:
        trip = Trip(name=name, state="PLANNING")
        db.add(trip)
        db.flush()
        member = member_repository.create_trip_member(db, trip.id, creator_id, "OWNER")
        db.flush()
        db.commit()
        db.refresh(trip)
        db.refresh(member)
        return trip
    except Exception:
        db.rollback()
        raise


def update_trip(
    db: Session,
    trip: Trip,
    name: str | None = None,
    state: str | None = None,
) -> Trip:
    if name is not None:
        trip.name = name
    if state is not None:
        trip.state = state
    db.commit()
    db.refresh(trip)
    return trip


def set_selected_dates(
    db: Session,
    trip: Trip,
    start_date: date,
    end_date: date,
) -> Trip:
    trip.selected_start_date = start_date
    trip.selected_end_date = end_date
    db.commit()
    db.refresh(trip)
    return trip


def delete_trip(db: Session, trip: Trip) -> None:
    db.delete(trip)
    db.commit()


def list_trips_for_user(db: Session, user_id: UUID) -> list[Trip]:
    statement = (
        select(Trip)
        .join(TripMember, TripMember.trip_id == Trip.id)
        .where(
            TripMember.user_id == user_id,
            TripMember.status == "ACTIVE",
        )
        .order_by(Trip.name)
    )
    return list(db.scalars(statement).all())
