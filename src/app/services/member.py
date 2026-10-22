from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.member import MemberAvailability, MemberBudget, MemberPreference, TripMember
from app.models.user import User
from app.repository import member as member_repository

if TYPE_CHECKING:
    from app.schemas.member import AvailabilityUpdate, BudgetUpdate, PreferenceUpdate


class MemberServiceError(ValueError):
    """Base error for member business-rule failures."""


class MemberNotFoundError(MemberServiceError):
    pass


class MemberForbiddenError(MemberServiceError):
    pass


class MemberConflictError(MemberServiceError):
    pass


class MemberInvalidError(MemberServiceError):
    pass


# Check that a user is an active trip member.
def require_active_member(
    db: Session,
    trip_id: UUID,
    user_id: UUID,
) -> TripMember:
    member = member_repository.get_active_trip_member(db, trip_id, user_id)
    if member is None:
        raise MemberForbiddenError("You are not an active member of this trip")
    return member


def list_members(
    db: Session,
    trip_id: UUID,
    current_user_id: UUID,
) -> list[tuple[TripMember, User]]:
    require_active_member(db, trip_id, current_user_id)
    return member_repository.list_active_members(db, trip_id)


def get_shared_availability(
    db: Session,
    trip_id: UUID,
) -> tuple[date, date]:
    """Return the earliest date range shared by all eligible active members."""
    members = [
        member
        for member, _user in member_repository.list_active_members(db, trip_id)
        if member.voting_eligible
    ]
    if not members:
        raise MemberConflictError("No eligible active members have been added")

    first_ranges = member_repository.list_availability(db, members[0].id)
    if not first_ranges:
        raise MemberConflictError("All eligible members must provide availability")
    shared_ranges = [(item.start_date, item.end_date) for item in first_ranges]

    for member in members[1:]:
        member_ranges = member_repository.list_availability(db, member.id)
        intersections: list[tuple[date, date]] = []
        for shared_range in shared_ranges:
            for member_range in member_ranges:
                start_date = max(shared_range[0], member_range.start_date)
                end_date = min(shared_range[1], member_range.end_date)
                if start_date <= end_date:
                    intersections.append((start_date, end_date))
        if not intersections:
            raise MemberConflictError("Eligible members do not share available dates")
        shared_ranges = intersections

    shared_start, shared_end = min(shared_ranges, key=lambda item: item[0])
    if shared_start > shared_end:
        raise MemberConflictError("Eligible members do not share available dates")
    return shared_start, shared_end


def get_group_match(db: Session, trip_id: UUID) -> dict:
    """Build the privacy-safe group match required by destination matching."""
    members = [
        member
        for member, _user in member_repository.list_active_members(db, trip_id)
        if member.voting_eligible
    ]
    if not members:
        raise MemberConflictError("No eligible active members have been added")

    shared_start, shared_end = get_shared_availability(db, trip_id)
    budgets: list[MemberBudget] = []
    interests: set[str] = set()
    for member in members:
        budget = member_repository.get_budget(db, member.id)
        preference = member_repository.get_preference(db, member.id)
        if budget is None or preference is None:
            raise MemberConflictError("All eligible members must provide budget and preferences")
        budgets.append(budget)
        interests.update(item.strip().lower() for item in preference.interests if item.strip())

    currencies = {budget.currency.upper() for budget in budgets}
    if len(currencies) != 1:
        raise MemberConflictError("All eligible member budgets must use the same currency")
    limiting_budget = min(budgets, key=lambda item: item.max_amount)
    return {
        "start_date": shared_start,
        "end_date": shared_end,
        "budget_amount": limiting_budget.max_amount,
        "currency": limiting_budget.currency.upper(),
        "interests": sorted(interests),
        "member_ids": [member.id for member in members],
    }


# Remove a member before voting begins.
def remove_member(
    db: Session,
    trip_id: UUID,
    member_id: UUID,
    current_user_id: UUID,
) -> None:
    requester = require_active_member(db, trip_id, current_user_id)
    if requester.role != "OWNER":
        raise MemberForbiddenError("Only the trip owner can remove members")

    trip = member_repository.get_trip(db, trip_id)
    if trip is None:
        raise MemberNotFoundError("Trip not found")
    if trip.voting_started_at is not None or member_repository.get_voting_state(db, trip_id):
        raise MemberConflictError("Members cannot be removed after voting has started")

    member = member_repository.get_active_member_by_id(db, trip_id, member_id)
    if member is None:
        raise MemberNotFoundError("Member not found")
    if member.id == requester.id:
        raise MemberConflictError("The trip owner cannot remove themselves")

    member.status = "REMOVED"
    member.voting_eligible = False
    db.commit()


def ensure_member_inputs_editable(db: Session, trip_id: UUID) -> None:
    trip = member_repository.get_trip(db, trip_id)
    if trip is None:
        raise MemberNotFoundError("Trip not found")
    if trip.voting_started_at is not None or member_repository.get_voting_state(db, trip_id):
        raise MemberConflictError("Member inputs cannot be changed after voting has started")


def get_availability(
    db: Session,
    trip_id: UUID,
    current_user_id: UUID,
) -> tuple[TripMember, list[MemberAvailability]]:
    member = require_active_member(db, trip_id, current_user_id)
    return member, member_repository.list_availability(db, member.id)


def replace_availability(
    db: Session,
    trip_id: UUID,
    current_user_id: UUID,
    data: AvailabilityUpdate,
) -> tuple[TripMember, list[MemberAvailability]]:
    member = require_active_member(db, trip_id, current_user_id)
    ensure_member_inputs_editable(db, trip_id)
    existing_ranges = member_repository.list_availability(db, member.id)
    member_repository.delete_availability(db, existing_ranges)
    new_ranges = [
        MemberAvailability(
            trip_member_id=member.id,
            start_date=date_range.start_date,
            end_date=date_range.end_date,
        )
        for date_range in data.ranges
    ]
    member_repository.add_availability(db, new_ranges)
    db.commit()
    for date_range in new_ranges:
        db.refresh(date_range)
    return member, new_ranges


def get_budget(
    db: Session,
    trip_id: UUID,
    current_user_id: UUID,
) -> tuple[TripMember, MemberBudget]:
    member = require_active_member(db, trip_id, current_user_id)
    budget = member_repository.get_budget(db, member.id)
    if budget is None:
        raise MemberConflictError("Budget has not been provided")
    return member, budget


def set_budget(
    db: Session,
    trip_id: UUID,
    current_user_id: UUID,
    data: BudgetUpdate,
) -> tuple[TripMember, MemberBudget]:
    member = require_active_member(db, trip_id, current_user_id)
    ensure_member_inputs_editable(db, trip_id)
    currency = data.currency.strip().upper()
    budget = member_repository.get_budget(db, member.id)
    if budget is None:
        budget = MemberBudget(
            trip_member_id=member.id, max_amount=data.max_amount, currency=currency
        )
    else:
        budget.max_amount = data.max_amount
        budget.currency = currency
    member_repository.save_budget(db, budget)
    db.commit()
    db.refresh(budget)
    return member, budget


def get_preferences(
    db: Session,
    trip_id: UUID,
    current_user_id: UUID,
) -> tuple[TripMember, MemberPreference]:
    member = require_active_member(db, trip_id, current_user_id)
    preference = member_repository.get_preference(db, member.id)
    if preference is None:
        raise MemberNotFoundError("Preferences have not been provided")
    return member, preference


# Create or update the current member's preferences.
def set_preferences(
    db: Session,
    trip_id: UUID,
    current_user_id: UUID,
    data: PreferenceUpdate,
) -> tuple[TripMember, MemberPreference]:
    member = require_active_member(db, trip_id, current_user_id)
    ensure_member_inputs_editable(db, trip_id)
    interests = sorted({item.strip().lower() for item in data.interests if item.strip()})

    preference = member_repository.get_preference(db, member.id)
    if preference is None:
        preference = MemberPreference(
            trip_member_id=member.id,
            interests=interests,
            private_notes=data.private_notes,
        )
    else:
        preference.interests = interests
        preference.private_notes = data.private_notes

    member_repository.save_preference(db, preference)
    db.commit()
    db.refresh(preference)
    return member, preference


def get_trip_member(
    db: Session,
    member_id: UUID,
) -> TripMember | None:
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


def list_trip_members(
    db: Session,
    trip_id: UUID,
) -> list[TripMember]:
    statement = (
        select(TripMember).where(TripMember.trip_id == trip_id).order_by(TripMember.joined_at)
    )

    return list(db.scalars(statement).all())


def add_trip_member(
    db: Session,
    trip_id: UUID,
    user_id: UUID,
    role: str = "MEMBER",
) -> TripMember:
    if role != "MEMBER":
        raise ValueError("New members must be assigned the MEMBER role")

    existing_member = get_trip_member_by_user(
        db=db,
        trip_id=trip_id,
        user_id=user_id,
    )

    if existing_member is not None:
        raise ValueError("This user is already a member of the trip")

    member = TripMember(
        trip_id=trip_id,
        user_id=user_id,
        role=role,
    )

    return member_repository.save_trip_member(db, member)


def update_trip_member(
    db: Session,
    member: TripMember,
    role: str,
) -> TripMember:
    if role not in {"OWNER", "MEMBER"}:
        raise ValueError("Role must be OWNER or MEMBER")
    member.role = role

    return member_repository.save_trip_member(db, member)


def remove_trip_member(
    db: Session,
    member: TripMember,
) -> None:
    member_repository.delete_trip_member(db, member)
