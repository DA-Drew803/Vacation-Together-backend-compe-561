from collections import defaultdict
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models.itinerary import (
    Itinerary,
    ItineraryApproval,
    ItineraryItem,
    ItineraryRevision,
)
from app.schemas.itinerary import (
    ApprovalInput,
    ApprovalProgress,
    ItineraryDraft,
    ItineraryItemInput,
    RevisionInput,
    TripContext,
)


class ItineraryConflictError(ValueError):
    pass


class ItineraryInvalidError(ValueError):
    pass


def _money(value: float | Decimal) -> float:
    return float(value)


def _uuid(value: str | UUID, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ItineraryInvalidError(f"{field_name} must be a valid UUID") from None


def _decimal(value: float | Decimal) -> Decimal:
    return Decimal(str(value))


def serialize_itinerary(itinerary: Itinerary, currency: str) -> dict:
    days: dict[int, list[dict]] = defaultdict(list)
    for item in itinerary.items:
        days[item.day_number].append(
            {
                "id": str(item.id),
                "position": item.position,
                "title": item.title,
                "estimated_cost": _money(item.estimated_cost),
            }
        )

    return {
        "id": itinerary.id,
        "trip_id": itinerary.trip_id,
        "destination_id": itinerary.destination_id,
        "version": itinerary.version,
        "status": itinerary.status,
        "days": [
            {"day_number": day_number, "activities": activities}
            for day_number, activities in sorted(days.items())
        ],
        "estimated_total_cost": {
            "max_amount": _money(itinerary.estimated_total_cost),
            "currency": currency,
        },
    }


def serialize_revision(revision: ItineraryRevision) -> dict:
    return {
        "id": revision.id,
        "instruction": revision.instruction,
        "status": revision.status,
        "base_version": revision.base_version,
    }


def get_itinerary(db: Session, trip_id: UUID, *, lock: bool = False) -> Itinerary | None:
    statement = (
        select(Itinerary)
        .where(Itinerary.trip_id == _uuid(trip_id, "trip_id"))
        .options(selectinload(Itinerary.items))
    )
    if lock:
        statement = statement.with_for_update()
    return db.scalar(statement)


def require_itinerary(db: Session, trip_id: UUID, *, lock: bool = False) -> Itinerary:
    itinerary = get_itinerary(db, trip_id, lock=lock)
    if itinerary is None:
        raise ItineraryConflictError("Itinerary has not been generated")
    return itinerary


def _replace_items(
    itinerary: Itinerary,
    item_inputs: list[ItineraryItemInput],
) -> None:
    itinerary.items = [
        ItineraryItem(
            day_number=item.day_number,
            position=item.position,
            title=item.title,
            estimated_cost=_decimal(item.estimated_cost),
        )
        for item in item_inputs
    ]


def _item_fingerprint(items: list[ItineraryItem] | list[ItineraryItemInput]) -> tuple:
    return tuple(
        sorted(
            (
                item.day_number,
                item.position,
                item.title,
                Decimal(str(item.estimated_cost)).quantize(Decimal("0.01")),
            )
            for item in items
        )
    )


def generate_itinerary_for_selected_destination(
    db: Session,
    context: TripContext,
    draft: ItineraryDraft,
) -> Itinerary:
    """Persist validated provider output after voting selects a destination."""
    existing = get_itinerary(db, context.trip_id)
    if existing is not None:
        return existing
    if not context.selected_destination_id:
        raise ItineraryConflictError("Trip does not have a selected destination")

    itinerary = Itinerary(
        trip_id=_uuid(context.trip_id, "trip_id"),
        destination_id=_uuid(context.selected_destination_id, "selected_destination_id"),
        version=1,
        status="DRAFT",
        estimated_total_cost=_decimal(draft.estimated_total_cost),
        items=[
            ItineraryItem(
                day_number=item.day_number,
                position=item.position,
                title=item.title,
                estimated_cost=_decimal(item.estimated_cost),
            )
            for item in draft.items
        ],
    )

    try:
        with db.begin_nested():
            db.add(itinerary)
            db.flush()
    except IntegrityError:
        db.expire_all()
        existing = get_itinerary(db, context.trip_id)
        if existing is None:
            raise ItineraryConflictError("Itinerary generation conflicted") from None
        return existing

    db.commit()
    return require_itinerary(db, context.trip_id)


def request_revision(
    db: Session,
    context: TripContext,
    current_member_id: UUID,
    revision_input: RevisionInput,
) -> ItineraryRevision:
    instruction = revision_input.instruction.strip()
    if not instruction:
        raise ItineraryInvalidError("Revision instruction must not be blank")

    eligible_ids = {
        member.member_id for member in context.eligible_members if member.voting_eligible
    }
    if current_member_id not in eligible_ids:
        raise ItineraryConflictError("Only eligible trip members may request revisions")

    itinerary = require_itinerary(db, context.trip_id, lock=True)
    if itinerary.status == "FINALIZED":
        raise ItineraryConflictError("Finalized itinerary cannot be revised")
    if itinerary.status == "REVISING":
        raise ItineraryConflictError("A revision is already running")

    active_revision = db.scalar(
        select(ItineraryRevision).where(
            ItineraryRevision.itinerary_id == itinerary.id,
            ItineraryRevision.status.in_(("PENDING", "RUNNING")),
        )
    )
    if active_revision is not None:
        raise ItineraryConflictError("A revision is already running")

    revision = ItineraryRevision(
        itinerary_id=itinerary.id,
        requested_by_member_id=_uuid(current_member_id, "current_member_id"),
        instruction=instruction,
        status="RUNNING",
        base_version=itinerary.version,
    )
    itinerary.status = "REVISING"
    db.add(revision)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ItineraryConflictError("A revision is already running") from None
    db.refresh(revision)
    return revision


def complete_revision(
    db: Session,
    revision_id: UUID,
    result: ItineraryDraft,
) -> ItineraryRevision:
    revision = db.scalar(
        select(ItineraryRevision)
        .where(ItineraryRevision.id == _uuid(revision_id, "revision_id"))
        .with_for_update()
    )
    if revision is None:
        raise ItineraryConflictError("Revision does not exist")
    if revision.status not in {"PENDING", "RUNNING"}:
        raise ItineraryConflictError("Revision is no longer active")

    itinerary = db.scalar(
        select(Itinerary)
        .where(Itinerary.id == revision.itinerary_id)
        .options(selectinload(Itinerary.items))
        .with_for_update()
    )
    if itinerary is None:
        raise ItineraryConflictError("Itinerary does not exist")
    if revision.base_version != itinerary.version:
        revision.status = "FAILED"
        if itinerary.status == "REVISING":
            itinerary.status = "DRAFT"
        db.commit()
        raise ItineraryConflictError("Revision result is stale")

    content_changed = _item_fingerprint(itinerary.items) != _item_fingerprint(
        result.items
    ) or Decimal(str(itinerary.estimated_total_cost)).quantize(Decimal("0.01")) != Decimal(
        str(result.estimated_total_cost)
    ).quantize(Decimal("0.01"))
    if not content_changed:
        revision.status = "NO_CHANGE"
        itinerary.status = "DRAFT"
    else:
        _replace_items(itinerary, result.items)
        itinerary.estimated_total_cost = _decimal(result.estimated_total_cost)
        itinerary.version += 1
        itinerary.status = "DRAFT"
        db.execute(delete(ItineraryApproval).where(ItineraryApproval.itinerary_id == itinerary.id))
        revision.status = "SUCCEEDED"

    db.commit()
    db.refresh(revision)
    return revision


def fail_revision(db: Session, revision_id: UUID) -> ItineraryRevision:
    revision = db.scalar(
        select(ItineraryRevision)
        .where(ItineraryRevision.id == _uuid(revision_id, "revision_id"))
        .with_for_update()
    )
    if revision is None:
        raise ItineraryConflictError("Revision does not exist")
    if revision.status not in {"PENDING", "RUNNING"}:
        raise ItineraryConflictError("Revision is no longer active")

    itinerary = db.scalar(
        select(Itinerary).where(Itinerary.id == revision.itinerary_id).with_for_update()
    )
    revision.status = "FAILED"
    if itinerary is not None and itinerary.status == "REVISING":
        itinerary.status = "DRAFT"
    db.commit()
    db.refresh(revision)
    return revision


def approval_progress(
    db: Session,
    itinerary: Itinerary,
    eligible_member_ids: set[UUID],
) -> ApprovalProgress:
    eligible_member_uuids = {
        _uuid(member_id, "eligible member_id") for member_id in eligible_member_ids
    }
    approved_count = 0
    if eligible_member_uuids:
        approved_count = (
            db.query(ItineraryApproval)
            .filter(
                ItineraryApproval.itinerary_id == itinerary.id,
                ItineraryApproval.version == itinerary.version,
                ItineraryApproval.trip_member_id.in_(eligible_member_uuids),
                ItineraryApproval.approved.is_(True),
            )
            .count()
        )
    return ApprovalProgress(
        version=itinerary.version,
        approved_count=approved_count,
        eligible_count=len(eligible_member_ids),
        finalized=itinerary.status == "FINALIZED",
    )


def get_approval_progress(db: Session, context: TripContext) -> ApprovalProgress:
    itinerary = require_itinerary(db, context.trip_id)
    eligible_ids = {
        member.member_id for member in context.eligible_members if member.voting_eligible
    }
    return approval_progress(db, itinerary, eligible_ids)


def approve_current_version(
    db: Session,
    context: TripContext,
    current_member_id: UUID,
    approval_input: ApprovalInput,
) -> dict:
    itinerary = require_itinerary(db, context.trip_id, lock=True)
    eligible_ids = {
        member.member_id for member in context.eligible_members if member.voting_eligible
    }
    if current_member_id not in eligible_ids:
        raise ItineraryConflictError("Only eligible trip members may approve")
    if itinerary.status in {"GENERATING", "REVISING"}:
        raise ItineraryConflictError("Approval is blocked while itinerary generation is running")
    if approval_input.version != itinerary.version:
        raise ItineraryConflictError("Approval version is stale")
    if itinerary.status == "FINALIZED" and not approval_input.approved:
        raise ItineraryConflictError("Finalized itinerary approval cannot be withdrawn")

    approval = db.scalar(
        select(ItineraryApproval).where(
            ItineraryApproval.itinerary_id == itinerary.id,
            ItineraryApproval.trip_member_id == _uuid(current_member_id, "current_member_id"),
        )
    )
    if approval is None:
        approval = ItineraryApproval(
            itinerary_id=itinerary.id,
            trip_member_id=_uuid(current_member_id, "current_member_id"),
            version=approval_input.version,
            approved=approval_input.approved,
        )
        db.add(approval)
    else:
        approval.version = approval_input.version
        approval.approved = approval_input.approved

    db.flush()
    progress = approval_progress(db, itinerary, eligible_ids)
    if progress.eligible_count > 0 and progress.approved_count == progress.eligible_count:
        itinerary.status = "FINALIZED"

    db.commit()
    return {
        "member_id": approval.trip_member_id,
        "version": approval.version,
        "approved": approval.approved,
    }
