from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import (
    get_current_trip_member,
    require_trip_owner,
)
from app.models.member import TripMember
from app.schemas.member import (
    TripMemberCreate,
    TripMemberResponse,
    TripMemberUpdate,
)
from app.services import member as member_service

router = APIRouter(
    tags=["Trip Members"],
)


@router.post(
    "/trips/{trip_id}/members",
    response_model=TripMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_trip_member(
    trip_id: UUID,
    member_data: TripMemberCreate,
    owner_membership: TripMember = Depends(require_trip_owner),
    db: Session = Depends(get_db),
):
    try:
        return member_service.add_trip_member(
            db=db,
            trip_id=trip_id,
            user_id=member_data.user_id,
            role=member_data.role,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error


@router.get(
    "/trips/{trip_id}/members",
    response_model=list[TripMemberResponse],
)
def list_trip_members(
    trip_id: UUID,
    current_membership: TripMember = Depends(get_current_trip_member),
    db: Session = Depends(get_db),
):
    return member_service.list_trip_members(
        db=db,
        trip_id=trip_id,
    )


@router.patch(
    "/trips/{trip_id}/members/{member_id}",
    response_model=TripMemberResponse,
)
def update_trip_member(
    trip_id: UUID,
    member_id: UUID,
    member_data: TripMemberUpdate,
    owner_membership: TripMember = Depends(require_trip_owner),
    db: Session = Depends(get_db),
):
    member = member_service.get_trip_member(
        db=db,
        member_id=member_id,
    )

    if member is None or member.trip_id != trip_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Trip member not found",
        )

    return member_service.update_trip_member(
        db=db,
        member=member,
        role=member_data.role,
    )


@router.delete(
    "/trips/{trip_id}/members/{member_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_trip_member(
    trip_id: UUID,
    member_id: UUID,
    owner_membership: TripMember = Depends(require_trip_owner),
    db: Session = Depends(get_db),
):
    member = member_service.get_trip_member(
        db=db,
        member_id=member_id,
    )

    if member is None or member.trip_id != trip_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Trip member not found",
        )

    member_service.remove_trip_member(
        db=db,
        member=member,
    )
