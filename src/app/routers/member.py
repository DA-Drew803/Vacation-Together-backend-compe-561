from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from app.core.database import get_session
from app.core.dependencies import get_current_user
from app.models.member import (
    MemberAvailability,
    MemberBudget,
    MemberPreference,
)
from app.models.trip import Trip, TripMember
from app.models.user import User
from app.schemas.member import (
    AvailabilityRead,
    AvailabilityUpdate,
    BudgetRead,
    BudgetUpdate,
    MemberRead,
    PreferenceRead,
    PreferenceUpdate,
)

router = APIRouter(prefix="/trips/{trip_id}", tags=["members"])

SessionDep = Annotated[Session, Depends(get_session)]
CurrentUserDep = Annotated[User, Depends(get_current_user)]


def get_current_trip_member(
    session: Session,
    trip_id: UUID,
    current_user_id: UUID,
) -> TripMember:
    member = session.exec(
        select(TripMember).where(
            TripMember.trip_id == trip_id,
            TripMember.user_id == current_user_id,
            TripMember.status == "ACTIVE",
        )
    ).first()
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not an active member of this trip",
        )
    return member


@router.get("/members", response_model=list[MemberRead])
def get_members(
    trip_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> list[MemberRead]:
    get_current_trip_member(session, trip_id, current_user.id)

    rows = session.exec(
        select(TripMember, User)
        .join(User, User.id == TripMember.user_id)
        .where(
            TripMember.trip_id == trip_id,
            TripMember.status == "ACTIVE",
        )
        .order_by(User.display_name)
    ).all()

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


@router.delete("/members/{member_id}")
def remove_member(
    trip_id: UUID,
    member_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> dict[str, str]:
    requester = get_current_trip_member(session, trip_id, current_user.id)
    if requester.role != "OWNER":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the trip owner can remove members",
        )

    trip = session.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    if trip.voting_started_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Members cannot be removed after voting has started",
        )

    member = session.exec(
        select(TripMember).where(
            TripMember.id == member_id,
            TripMember.trip_id == trip_id,
            TripMember.status == "ACTIVE",
        )
    ).first()
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    if member.id == requester.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The trip owner cannot remove themselves",
        )

    member.status = "REMOVED"
    member.voting_eligible = False
    session.add(member)
    session.commit()
    return {"detail": "Member removed"}


@router.get("/availability/me", response_model=AvailabilityRead)
def get_my_availability(
    trip_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> AvailabilityRead:
    member = get_current_trip_member(session, trip_id, current_user.id)
    ranges = session.exec(
        select(MemberAvailability)
        .where(MemberAvailability.trip_member_id == member.id)
        .order_by(MemberAvailability.start_date, MemberAvailability.end_date)
    ).all()
    return AvailabilityRead(member_id=member.id, ranges=ranges)


@router.put("/availability/me", response_model=AvailabilityRead)
def update_my_availability(
    trip_id: UUID,
    data: AvailabilityUpdate,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> AvailabilityRead:
    member = get_current_trip_member(session, trip_id, current_user.id)
    existing_ranges = session.exec(
        select(MemberAvailability).where(
            MemberAvailability.trip_member_id == member.id
        )
    ).all()
    for existing_range in existing_ranges:
        session.delete(existing_range)

    new_ranges = [
        MemberAvailability(
            trip_member_id=member.id,
            start_date=date_range.start_date,
            end_date=date_range.end_date,
        )
        for date_range in data.ranges
    ]
    session.add_all(new_ranges)
    session.commit()
    for date_range in new_ranges:
        session.refresh(date_range)

    return AvailabilityRead(member_id=member.id, ranges=new_ranges)


@router.get("/budget/me", response_model=BudgetRead)
def get_my_budget(
    trip_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> BudgetRead:
    member = get_current_trip_member(session, trip_id, current_user.id)
    budget = session.get(MemberBudget, member.id)
    if budget is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Budget has not been provided",
        )
    return BudgetRead(
        member_id=member.id,
        max_amount=budget.max_amount,
        currency=budget.currency,
    )


@router.put("/budget/me", response_model=BudgetRead)
def update_my_budget(
    trip_id: UUID,
    data: BudgetUpdate,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> BudgetRead:
    member = get_current_trip_member(session, trip_id, current_user.id)
    budget = session.get(MemberBudget, member.id)
    if budget is None:
        budget = MemberBudget(
            trip_member_id=member.id,
            max_amount=data.max_amount,
            currency=data.currency.upper(),
        )
    else:
        budget.max_amount = data.max_amount
        budget.currency = data.currency.upper()

    session.add(budget)
    session.commit()
    session.refresh(budget)
    return BudgetRead(
        member_id=member.id,
        max_amount=budget.max_amount,
        currency=budget.currency,
    )


@router.get("/preferences/me", response_model=PreferenceRead)
def get_my_preferences(
    trip_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> PreferenceRead:
    member = get_current_trip_member(session, trip_id, current_user.id)
    preference = session.get(MemberPreference, member.id)
    if preference is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Preferences have not been provided",
        )
    return PreferenceRead(
        member_id=member.id,
        interests=preference.interests,
        private_notes=preference.private_notes,
    )


@router.put("/preferences/me", response_model=PreferenceRead)
def update_my_preferences(
    trip_id: UUID,
    data: PreferenceUpdate,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> PreferenceRead:
    member = get_current_trip_member(session, trip_id, current_user.id)
    preference = session.get(MemberPreference, member.id)
    if preference is None:
        preference = MemberPreference(
            trip_member_id=member.id,
            interests=data.interests,
            private_notes=data.private_notes,
        )
    else:
        preference.interests = data.interests
        preference.private_notes = data.private_notes

    session.add(preference)
    session.commit()
    session.refresh(preference)
    return PreferenceRead(
        member_id=member.id,
        interests=preference.interests,
        private_notes=preference.private_notes,
    )
