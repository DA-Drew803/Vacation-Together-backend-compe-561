from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING
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

if TYPE_CHECKING:
    from app.services.openai_itinerary import ItineraryDraftProvider, ItineraryRevisionProvider


logger = logging.getLogger(__name__)


class ItineraryConflictError(ValueError):
    pass


class ItineraryInvalidError(ValueError):
    pass


def money_value(value: float | Decimal) -> float:
    return float(value)


def parse_uuid(value: str | UUID, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ItineraryInvalidError(f"{field_name} must be a valid UUID") from None


def decimal_value(value: float | Decimal) -> Decimal:
    return Decimal(str(value))


def serialize_itinerary(itinerary: Itinerary, currency: str) -> dict:
    days: dict[int, list[dict]] = defaultdict(list)
    for item in itinerary.items:
        days[item.day_number].append(
            {
                "id": str(item.id),
                "position": item.position,
                "title": item.title,
                "estimated_cost": money_value(item.estimated_cost),
            }
        )

    return {
        "id": str(itinerary.id),
        "trip_id": str(itinerary.trip_id),
        "destination_id": str(itinerary.destination_id),
        "version": itinerary.version,
        "status": itinerary.status,
        "days": [
            {"day_number": day_number, "activities": activities}
            for day_number, activities in sorted(days.items())
        ],
        "estimated_total_cost": {
            "max_amount": money_value(itinerary.estimated_total_cost),
            "currency": currency,
        },
    }


def serialize_revision(revision: ItineraryRevision) -> dict:
    return {
        "id": str(revision.id),
        "instruction": revision.instruction,
        "status": revision.status,
        "base_version": revision.base_version,
    }


def get_itinerary(db: Session, trip_id: str, *, lock: bool = False) -> Itinerary | None:
    statement = (
        select(Itinerary)
        .where(Itinerary.trip_id == parse_uuid(trip_id, "trip_id"))
        .options(selectinload(Itinerary.items))
    )
    if lock:
        statement = statement.with_for_update()
    return db.scalar(statement)


def require_itinerary(db: Session, trip_id: str, *, lock: bool = False) -> Itinerary:
    itinerary = get_itinerary(db, trip_id, lock=lock)
    if itinerary is None:
        raise ItineraryConflictError("Itinerary has not been generated")
    return itinerary


def replace_items(
    itinerary: Itinerary,
    item_inputs: list[ItineraryItemInput],
) -> None:
    itinerary.items = [
        ItineraryItem(
            day_number=item.day_number,
            position=item.position,
            title=item.title,
            estimated_cost=decimal_value(item.estimated_cost),
        )
        for item in item_inputs
    ]


def item_fingerprint(items: list[ItineraryItem] | list[ItineraryItemInput]) -> tuple:
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
        trip_id=parse_uuid(context.trip_id, "trip_id"),
        destination_id=parse_uuid(context.selected_destination_id, "selected_destination_id"),
        version=1,
        status="DRAFT",
        estimated_total_cost=decimal_value(draft.estimated_total_cost),
        items=[
            ItineraryItem(
                day_number=item.day_number,
                position=item.position,
                title=item.title,
                estimated_cost=decimal_value(item.estimated_cost),
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


def generate_and_persist_initial_itinerary(
    db: Session,
    context: TripContext,
    provider: ItineraryDraftProvider,
) -> Itinerary:
    """Prototype integration boundary; not safe for concurrent production use.

    This pre-check can avoid a provider call after an itinerary already exists, but concurrent
    callers can still invoke the provider more than once. It also performs a database read before
    the external call. Do not connect this wrapper to voting until the team defines atomic claims,
    transaction boundaries, failure recording, and retries with the voting workflow owner.
    """
    existing = get_itinerary(db, context.trip_id)
    if existing is not None:
        return existing
    draft = provider.generate(context)
    return generate_itinerary_for_selected_destination(db, context, draft)


def request_revision(
    db: Session,
    context: TripContext,
    current_member_id: str,
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
        requested_by_member_id=parse_uuid(current_member_id, "current_member_id"),
        instruction=instruction,
        status="RUNNING",
        base_version=itinerary.version,
    )
    itinerary.status = "REVISING"
    db.add(revision)
    try:
        db.flush()
        db.expunge(revision)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ItineraryConflictError("A revision is already running") from None
    return revision


def complete_revision(
    db: Session,
    revision_id: str,
    result: ItineraryDraft,
) -> ItineraryRevision:
    revision = db.scalar(
        select(ItineraryRevision)
        .where(ItineraryRevision.id == parse_uuid(revision_id, "revision_id"))
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

    content_changed = item_fingerprint(itinerary.items) != item_fingerprint(
        result.items
    ) or Decimal(str(itinerary.estimated_total_cost)).quantize(Decimal("0.01")) != Decimal(
        str(result.estimated_total_cost)
    ).quantize(Decimal("0.01"))
    if not content_changed:
        revision.status = "NO_CHANGE"
        itinerary.status = "DRAFT"
    else:
        replace_items(itinerary, result.items)
        itinerary.estimated_total_cost = decimal_value(result.estimated_total_cost)
        itinerary.version += 1
        itinerary.status = "DRAFT"
        db.execute(delete(ItineraryApproval).where(ItineraryApproval.itinerary_id == itinerary.id))
        revision.status = "SUCCEEDED"

    db.commit()
    db.refresh(revision)
    return revision


def fail_revision(db: Session, revision_id: str) -> ItineraryRevision:
    revision = db.scalar(
        select(ItineraryRevision)
        .where(ItineraryRevision.id == parse_uuid(revision_id, "revision_id"))
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


def execute_ai_revision(
    db: Session,
    context: TripContext,
    current_member_id: str,
    revision_input: RevisionInput,
    provider: ItineraryRevisionProvider,
) -> ItineraryRevision:
    """Run an isolated AI revision without holding a database transaction during the API call.

    This orchestration boundary is intentionally not connected to the HTTP 202 route. A production
    endpoint needs a team decision about background execution and recovery after process failure.
    Authorization, active-revision exclusion, versioning, and approval handling remain delegated
    to the existing deterministic lifecycle functions.
    """
    revision = request_revision(db, context, current_member_id, revision_input)
    revision_id = str(revision.id)
    try:
        itinerary = require_itinerary(db, context.trip_id)
        current_draft = ItineraryDraft(
            items=[
                ItineraryItemInput(
                    day_number=item.day_number,
                    position=item.position,
                    title=item.title,
                    estimated_cost=money_value(item.estimated_cost),
                )
                for item in itinerary.items
            ],
            estimated_total_cost=money_value(itinerary.estimated_total_cost),
        )
        base_version = revision.base_version

        # End the read transaction before the external provider call.
        db.commit()
        revised_draft = provider.revise(
            context,
            current_draft,
            base_version,
            revision_input.instruction,
        )
        return complete_revision(db, revision_id, revised_draft)
    except Exception:
        cleanup_failed_revision(db, revision_id)
        raise


def cleanup_failed_revision(db: Session, revision_id: str) -> None:
    """Best-effort cleanup that never masks the exception which triggered it."""
    try:
        db.rollback()
    except Exception:
        logger.exception("Failed to roll back before cleaning up revision %s", revision_id)
        return
    try:
        fail_revision(db, revision_id)
    except Exception:
        try:
            db.rollback()
        except Exception:
            logger.exception(
                "Failed to roll back after cleanup failure for revision %s", revision_id
            )
        logger.exception("Failed to clean up itinerary revision %s", revision_id)


def approval_progress(
    db: Session,
    itinerary: Itinerary,
    eligible_member_ids: set[str],
) -> ApprovalProgress:
    eligible_member_uuids = {
        parse_uuid(member_id, "eligible member_id") for member_id in eligible_member_ids
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
    current_member_id: str,
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
            ItineraryApproval.trip_member_id == parse_uuid(current_member_id, "current_member_id"),
        )
    )
    if approval is None:
        approval = ItineraryApproval(
            itinerary_id=itinerary.id,
            trip_member_id=parse_uuid(current_member_id, "current_member_id"),
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
        "member_id": str(approval.trip_member_id),
        "version": approval.version,
        "approved": approval.approved,
    }
