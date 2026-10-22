from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_trip_member, require_trip_owner
from app.models.member import TripMember
from app.schemas.member import (
    AvailabilityRead,
    AvailabilityUpdate,
    BudgetRead,
    BudgetUpdate,
    DateRange,
    GroupAvailability,
    GroupBudget,
    GroupMatch,
    MemberRead,
    PreferenceRead,
    PreferenceUpdate,
)
from app.services import member as member_service

router = APIRouter(prefix="/trips/{trip_id}", tags=["members"])

SessionDep = Annotated[Session, Depends(get_db)]
CurrentMembershipDep = Annotated[TripMember, Depends(get_current_trip_member)]
OwnerMembershipDep = Annotated[TripMember, Depends(require_trip_owner)]


@router.get("/group_match", response_model=GroupMatch)
def get_group_match(
    trip_id: UUID,
    db: SessionDep,
    current_membership: CurrentMembershipDep,
) -> GroupMatch:
    del current_membership
    try:
        match = member_service.get_group_match(db, trip_id)
    except member_service.MemberConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return GroupMatch(
        common_date_ranges=GroupAvailability(
            ranges=[DateRange(start_date=match["start_date"], end_date=match["end_date"])],
        ),
        group_max_budget=GroupBudget(
            max_amount=match["budget_amount"],
            currency=match["currency"],
        ),
        interests=match["interests"],
    )


# Return the active members for a trip.
@router.get("/members", response_model=list[MemberRead])
def get_members(
    trip_id: UUID,
    db: SessionDep,
    current_membership: CurrentMembershipDep,
) -> list[MemberRead]:
    try:
        rows = member_service.list_members(db, trip_id, current_membership.user_id)
    except member_service.MemberForbiddenError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error

    return [
        MemberRead(
            id=member.id,
            user_id=member.user_id,
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
    owner_membership: OwnerMembershipDep,
) -> dict[str, str]:
    try:
        member_service.remove_member(db, trip_id, member_id, owner_membership.user_id)
    except member_service.MemberForbiddenError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except member_service.MemberNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except member_service.MemberConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return {"detail": "Member removed"}


@router.get("/availability/me", response_model=AvailabilityRead)
def get_my_availability(
    trip_id: UUID,
    db: SessionDep,
    current_membership: CurrentMembershipDep,
) -> AvailabilityRead:
    try:
        member, ranges = member_service.get_availability(db, trip_id, current_membership.user_id)
    except member_service.MemberForbiddenError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    return AvailabilityRead(
        member_id=member.id,
        ranges=[DateRange(start_date=item.start_date, end_date=item.end_date) for item in ranges],
    )


@router.put("/availability/me", response_model=AvailabilityRead)
def update_my_availability(
    trip_id: UUID,
    data: AvailabilityUpdate,
    db: SessionDep,
    current_membership: CurrentMembershipDep,
) -> AvailabilityRead:
    try:
        member, ranges = member_service.replace_availability(
            db, trip_id, current_membership.user_id, data
        )
    except member_service.MemberForbiddenError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except member_service.MemberNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except member_service.MemberConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return AvailabilityRead(
        member_id=member.id,
        ranges=[DateRange(start_date=item.start_date, end_date=item.end_date) for item in ranges],
    )


@router.get("/budget/me", response_model=BudgetRead)
def get_my_budget(
    trip_id: UUID,
    db: SessionDep,
    current_membership: CurrentMembershipDep,
) -> BudgetRead:
    try:
        member, budget = member_service.get_budget(db, trip_id, current_membership.user_id)
    except member_service.MemberForbiddenError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except member_service.MemberConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return BudgetRead(member_id=member.id, max_amount=budget.max_amount, currency=budget.currency)


@router.put("/budget/me", response_model=BudgetRead)
def update_my_budget(
    trip_id: UUID,
    data: BudgetUpdate,
    db: SessionDep,
    current_membership: CurrentMembershipDep,
) -> BudgetRead:
    try:
        member, budget = member_service.set_budget(db, trip_id, current_membership.user_id, data)
    except member_service.MemberForbiddenError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except member_service.MemberNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except member_service.MemberConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return BudgetRead(member_id=member.id, max_amount=budget.max_amount, currency=budget.currency)


# Return the current member's preferences.
@router.get("/preferences/me", response_model=PreferenceRead)
def get_my_preferences(
    trip_id: UUID,
    db: SessionDep,
    current_membership: CurrentMembershipDep,
) -> PreferenceRead:
    try:
        member, preference = member_service.get_preferences(db, trip_id, current_membership.user_id)
    except member_service.MemberForbiddenError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except member_service.MemberNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
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
    current_membership: CurrentMembershipDep,
) -> PreferenceRead:
    try:
        member, preference = member_service.set_preferences(
            db,
            trip_id,
            current_membership.user_id,
            data,
        )
    except member_service.MemberForbiddenError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except member_service.MemberNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except member_service.MemberConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return PreferenceRead(
        member_id=member.id,
        interests=preference.interests,
        private_notes=preference.private_notes,
    )
