from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_member_id
from app.models.user import User
from app.schemas.member import (
    AvailabilityRead,
    AvailabilityUpdate,
    BudgetRead,
    BudgetUpdate,
    MemberRead,
    PreferenceRead,
    PreferenceUpdate,
    TripMemberCreate,
    TripMemberResponse,
)
from app.services import member as member_service

router = APIRouter(prefix="/trips/{trip_id}", tags=["members"])

SessionDep = Annotated[Session, Depends(get_db)]
CurrentUserDep = Annotated[User, Depends(get_current_member_id)]


# Add an existing user to a trip.
@router.post(
    "/members",
    response_model=TripMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_user_to_trip(
    trip_id: UUID,
    data: TripMemberCreate,
    db: SessionDep,
) -> TripMemberResponse:
    try:
        member = member_service.add_trip_member(
            db=db,
            trip_id=trip_id,
            user_id=data.user_id,
            role=data.role,
        )
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error

    return TripMemberResponse.model_validate(member)


# Return the active members for a trip.
@router.get("/members", response_model=list[MemberRead])
def get_members(
    trip_id: UUID,
    db: SessionDep,
    current_user: CurrentUserDep,
) -> list[MemberRead]:
    try:
        rows = member_service.list_members(db, trip_id, current_user.id)
    except member_service.MemberForbiddenError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error

    return [
        MemberRead(
            id=member.id,
            display_name=user.display_name,
            role=member.role,
            status=member.status,
            voting_eligible=member.voting_eligible,
        )
        for member, user in rows
    ]


# Remove a member from a trip.
@router.delete("/members/{member_id}")
def remove_member(
    trip_id: UUID,
    member_id: UUID,
    db: SessionDep,
    current_user: CurrentUserDep,
) -> dict[str, str]:
    try:
        member_service.remove_member(db, trip_id, member_id, current_user.id)
    except member_service.MemberForbiddenError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except member_service.MemberNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except member_service.MemberConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return {"detail": "Member removed"}


# Return the current member's availability.
@router.get("/availability/me", response_model=AvailabilityRead)
def get_my_availability(
    trip_id: UUID,
    db: SessionDep,
    current_user: CurrentUserDep,
) -> AvailabilityRead:
    try:
        member, ranges = member_service.get_availability(db, trip_id, current_user.id)
    except member_service.MemberForbiddenError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    return AvailabilityRead(member_id=member.id, ranges=ranges)


# Update the current member's availability.
@router.put("/availability/me", response_model=AvailabilityRead)
def update_my_availability(
    trip_id: UUID,
    data: AvailabilityUpdate,
    db: SessionDep,
    current_user: CurrentUserDep,
) -> AvailabilityRead:
    try:
        member, ranges = member_service.replace_availability(
            db,
            trip_id,
            current_user.id,
            data,
        )
    except member_service.MemberForbiddenError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except member_service.MemberInvalidError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    return AvailabilityRead(member_id=member.id, ranges=ranges)


# Return the current member's budget.
@router.get("/budget/me", response_model=BudgetRead)
def get_my_budget(
    trip_id: UUID,
    db: SessionDep,
    current_user: CurrentUserDep,
) -> BudgetRead:
    try:
        member, budget = member_service.get_budget(db, trip_id, current_user.id)
    except member_service.MemberForbiddenError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except member_service.MemberConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return BudgetRead(
        member_id=member.id,
        max_amount=budget.max_amount,
        currency=budget.currency,
    )


# Update the current member's budget.
@router.put("/budget/me", response_model=BudgetRead)
def update_my_budget(
    trip_id: UUID,
    data: BudgetUpdate,
    db: SessionDep,
    current_user: CurrentUserDep,
) -> BudgetRead:
    try:
        member, budget = member_service.set_budget(db, trip_id, current_user.id, data)
    except member_service.MemberForbiddenError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except member_service.MemberInvalidError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    return BudgetRead(
        member_id=member.id,
        max_amount=budget.max_amount,
        currency=budget.currency,
    )


# Return the current member's preferences.
@router.get("/preferences/me", response_model=PreferenceRead)
def get_my_preferences(
    trip_id: UUID,
    db: SessionDep,
    current_user: CurrentUserDep,
) -> PreferenceRead:
    try:
        member, preference = member_service.get_preferences(db, trip_id, current_user.id)
    except member_service.MemberForbiddenError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except member_service.MemberConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return PreferenceRead(
        member_id=member.id,
        interests=preference.interests,
        private_notes=preference.private_notes,
    )


# Update the current member's preferences.
@router.put("/preferences/me", response_model=PreferenceRead)
def update_my_preferences(
    trip_id: UUID,
    data: PreferenceUpdate,
    db: SessionDep,
    current_user: CurrentUserDep,
) -> PreferenceRead:
    try:
        member, preference = member_service.set_preferences(
            db,
            trip_id,
            current_user.id,
            data,
        )
    except member_service.MemberForbiddenError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    return PreferenceRead(
        member_id=member.id,
        interests=preference.interests,
        private_notes=preference.private_notes,
    )
