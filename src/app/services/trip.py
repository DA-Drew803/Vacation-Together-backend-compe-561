from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.trip import Trip
from app.models.member import TripMember



def get_trip(
    db: Session,
    trip_id: UUID,
) -> Trip | None:
    statement = select(Trip).where(Trip.id == trip_id)
    return db.scalar(statement)


def list_trips(db: Session) -> list[Trip]:
    statement = select(Trip).order_by(Trip.name)
    return list(db.scalars(statement).all())


def create_trip(
    db: Session,
    name: str,
    creator_id: UUID,
) -> Trip:
    try:
        trip = Trip(
            name=name,
            state="PLANNING",
        )

        db.add(trip)
        db.flush()

        owner_membership = TripMember(
            trip_id=trip.id,
            user_id=creator_id,
            role="OWNER",
        )

        db.add(owner_membership)
        db.commit()
        db.refresh(trip)

        return trip

    except Exception:
        db.rollback()
        raise


def update_trip(
    db: Session,
    trip: Trip,
    name: str | None = None,
    state: str | None = None,
    selected_destination_id: UUID | None = None,
) -> Trip:
    if name is not None:
        trip.name = name

    if state is not None:
        trip.state = state

    if selected_destination_id is not None:
        trip.selected_destination_id = selected_destination_id

    db.commit()
    db.refresh(trip)

    return trip


def delete_trip(
    db: Session,
    trip: Trip,
) -> None:
    db.delete(trip)
    db.commit()

def list_trips_for_user(
    db: Session,
    user_id: UUID,
) -> list[Trip]:
    statement = (
        select(Trip)
        .join(
            TripMember,
            TripMember.trip_id == Trip.id,
        )
        .where(TripMember.user_id == user_id)
        .order_by(Trip.name)
    )

    return list(db.scalars(statement).all())