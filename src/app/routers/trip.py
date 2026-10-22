from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import (
    get_current_trip_member,
    get_current_user,
    require_trip_owner,
)
from app.models.member import TripMember
from app.models.user import User
from app.schemas.trip import TripCreate, TripDatesUpdate, TripResponse, TripUpdate
from app.services import trip as trip_service

router = APIRouter(
    prefix="/trips",
    tags=["Trips"],
)


@router.post(
    "",
    response_model=TripResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_trip(
    trip_data: TripCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return trip_service.create_trip(
        db=db,
        name=trip_data.name,
        creator_id=current_user.id,
    )


@router.get(
    "",
    response_model=list[TripResponse],
)
def list_trips(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return trip_service.list_trips_for_user(
        db=db,
        user_id=current_user.id,
    )


@router.get(
    "/{trip_id}",
    response_model=TripResponse,
)
def get_trip(
    trip_id: UUID,
    current_membership: TripMember = Depends(get_current_trip_member),
    db: Session = Depends(get_db),
):
    trip = trip_service.get_trip(
        db=db,
        trip_id=trip_id,
    )

    if trip is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Trip not found",
        )

    return trip


@router.patch(
    "/{trip_id}",
    response_model=TripResponse,
)
def update_trip(
    trip_id: UUID,
    trip_data: TripUpdate,
    current_membership: TripMember = Depends(require_trip_owner),
    db: Session = Depends(get_db),
):
    trip = trip_service.get_trip(
        db=db,
        trip_id=trip_id,
    )

    if trip is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Trip not found",
        )

    return trip_service.update_trip(
        db=db,
        trip=trip,
        name=trip_data.name,
        state=trip_data.state,
    )


@router.put(
    "/{trip_id}/dates",
    response_model=TripResponse,
)
def select_trip_dates(
    trip_id: UUID,
    trip_dates: TripDatesUpdate,
    current_membership: TripMember = Depends(require_trip_owner),
    db: Session = Depends(get_db),
):
    del current_membership
    trip = trip_service.get_trip(db=db, trip_id=trip_id)
    if trip is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Trip not found",
        )

    return trip_service.set_selected_dates(
        db=db,
        trip=trip,
        start_date=trip_dates.selected_start_date,
        end_date=trip_dates.selected_end_date,
    )


@router.delete(
    "/{trip_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_trip(
    trip_id: UUID,
    current_membership: TripMember = Depends(require_trip_owner),
    db: Session = Depends(get_db),
):
    trip = trip_service.get_trip(
        db=db,
        trip_id=trip_id,
    )

    if trip is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Trip not found",
        )

    trip_service.delete_trip(
        db=db,
        trip=trip,
    )
