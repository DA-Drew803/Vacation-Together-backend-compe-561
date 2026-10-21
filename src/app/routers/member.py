from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_member_id
from app.schemas.member import (
    MemberRead,
    PreferenceRead,
    PreferenceUpdate,
    TripMemberListResponse,
)
from app.services import member as member_service

router = APIRouter(prefix="/trips/{trip_id}", tags=["members"])

SessionDep = Annotated[Session, Depends(get_db)]
CurrentUserIdDep = Annotated[UUID, Depends(get_current_member_id)]


# Return the active members for a trip.
@router.get("/members", response_model=TripMemberListResponse)
def get_members(
    trip_id: UUID,
    db: SessionDep,
    current_user_id: CurrentUserIdDep,
) -> TripMemberListResponse:
    try:
        rows = member_service.list_members(db, trip_id, current_user_id)
    except member_service.MemberForbiddenError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error

    return TripMemberListResponse(
        items=[
            MemberRead(
                id=member.id,
                user=user,
                role=member.role,
                status=member.status,
                joined_at=member.joined_at,
            )
            for member, user in rows
        ]
    )


# Remove a member from a trip.
@router.delete("/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    trip_id: UUID,
    member_id: UUID,
    db: SessionDep,
    current_user_id: CurrentUserIdDep,
) -> None:
    try:
        member_service.remove_member(db, trip_id, member_id, current_user_id)
    except member_service.MemberForbiddenError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except member_service.MemberNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except member_service.MemberConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


# Return the current member's preferences.
@router.get("/preferences/me", response_model=PreferenceRead)
def get_my_preferences(
    trip_id: UUID,
    db: SessionDep,
    current_user_id: CurrentUserIdDep,
) -> PreferenceRead:
    try:
        member, preference = member_service.get_preferences(db, trip_id, current_user_id)
    except member_service.MemberForbiddenError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except member_service.MemberNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    return PreferenceRead(
        trip_member_id=member.id,
        interests=preference.interests,
        free_text=preference.free_text,
        updated_at=preference.updated_at,
    )


# Update the current member's preferences.
@router.put("/preferences/me", response_model=PreferenceRead)
def update_my_preferences(
    trip_id: UUID,
    data: PreferenceUpdate,
    db: SessionDep,
    current_user_id: CurrentUserIdDep,
) -> PreferenceRead:
    try:
        member, preference = member_service.set_preferences(
            db,
            trip_id,
            current_user_id,
            data,
        )
    except member_service.MemberForbiddenError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    return PreferenceRead(
        trip_member_id=member.id,
        interests=preference.interests,
        free_text=preference.free_text,
        updated_at=preference.updated_at,
    )
