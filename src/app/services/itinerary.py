from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.core.database import SessionLocal
from app.models.destination import Destination, DestinationCandidate
from app.models.itinerary import (
    Itinerary,
    ItineraryApproval,
    ItineraryItem,
    ItineraryRevision,
)
from app.models.member import TripMember
from app.models.trip import Trip
from app.models.user import User
from app.schemas.itinerary import (
    ApprovalInput,
    ApprovalMember,
    ApprovalProgress,
    Budget,
    EligibleMember,
    ItineraryDraft,
    ItineraryItemInput,
    ItineraryItemMetadata,
    RevisionInput,
    TransportationPlan,
    TripContext,
    WeatherDay,
)
from app.services import member as member_service
from app.services.places import resolve_destination, resolve_place
from app.workflows.itinerary_graph import (
    run_itinerary_generation_graph,
    run_itinerary_revision_graph,
)

if TYPE_CHECKING:
    from app.services.openai_itinerary import ItineraryDraftProvider, ItineraryRevisionProvider


logger = logging.getLogger(__name__)


class ItineraryConflictError(ValueError):
    pass


class ItineraryInvalidError(ValueError):
    pass


def build_trip_context(db: Session, trip_id: str | UUID) -> TripContext:
    trip_uuid = parse_uuid(trip_id, "trip_id")
    trip = db.get(Trip, trip_uuid)
    if trip is None or trip.selected_destination_id is None:
        raise ItineraryConflictError("Trip does not have a selected destination")
    destination = db.get(Destination, trip.selected_destination_id)
    if destination is None:
        raise ItineraryConflictError("Selected destination does not exist")
    match = member_service.get_group_match(db, trip_uuid)
    rows = db.execute(
        select(TripMember, User)
        .join(User, User.id == TripMember.user_id)
        .where(TripMember.trip_id == trip_uuid, TripMember.status == "ACTIVE")
    ).all()
    eligible_members = [
        EligibleMember(
            member_id=str(member.id),
            display_name=user.display_name,
            voting_eligible=member.voting_eligible,
        )
        for member, user in rows
    ]
    candidate = db.scalar(
        select(DestinationCandidate).where(
            DestinationCandidate.trip_id == trip_uuid,
            DestinationCandidate.destination_id == destination.id,
        )
    )
    weather = None
    if candidate and candidate.weather_snapshot:
        snapshot = candidate.weather_snapshot
        if snapshot.get("data_type") == "forecast":
            weather = [
                WeatherDay(
                    date=item["date"],
                    condition=f"weather_code_{item.get('weather_code')}",
                    temperature_high=item.get("temperature_high"),
                    temperature_low=item.get("temperature_low"),
                    precipitation_probability=item.get("precipitation_probability"),
                )
                for item in snapshot.get("days", [])
            ]
    start_date = trip.selected_start_date or match["start_date"]
    end_date = trip.selected_end_date or match["end_date"]
    transportation = None
    if trip.transportation_preference != "AUTO":
        transportation = TransportationPlan(mode=trip.transportation_preference)

    return TripContext(
        trip_id=str(trip.id),
        selected_destination_id=str(destination.id),
        destination_name=destination.name,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        budget=Budget(max_amount=float(match["budget_amount"]), currency=match["currency"]),
        eligible_members=eligible_members,
        interests=match["interests"],
        weather=weather,
        transportation=transportation,
    )


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


def serialize_itinerary(
    itinerary: Itinerary,
    currency: str,
    *,
    destination_name: str | None = None,
    destination_location: str = "",
    destination_photo_url: str | None = None,
    weather: list[WeatherDay] | None = None,
) -> dict:
    days: dict[int, list[dict]] = defaultdict(list)
    for item in itinerary.items:
        activity = {
            "id": str(item.id),
            "position": item.position,
            "title": item.title,
            "estimated_cost": money_value(item.estimated_cost),
        }
        if item.semantic_metadata:
            tags = item.semantic_metadata.get("tags", [])
            category = item.semantic_metadata.get("category")
            purpose_parts = [str(tag).replace("_", " ") for tag in tags[:3] if str(tag).strip()]
            if not purpose_parts and category:
                purpose_parts = [str(category).replace("_", " ")]
            if purpose_parts:
                activity["purpose"] = " · ".join(purpose_parts).upper()
        if destination_name:
            destination_query = ", ".join(
                part for part in (destination_name, destination_location) if part.strip()
            )
            resolved = resolve_place(item.title, destination_query)
            if resolved:
                activity.update(resolved)
                if "photo_name" in resolved:
                    activity["photo_url"] = (
                        f"/trips/{itinerary.trip_id}/itinerary/items/{item.id}/photo"
                    )
        days[item.day_number].append(activity)

    destination = (
        resolve_destination(destination_name, destination_location) if destination_name else None
    )
    return {
        "id": str(itinerary.id),
        "trip_id": str(itinerary.trip_id),
        "destination_id": str(itinerary.destination_id),
        "destination_name": destination_name,
        "destination_place_id": destination.get("place_id") if destination else None,
        "destination_formatted_address": destination.get("location") if destination else None,
        "destination_latitude": destination.get("latitude") if destination else None,
        "destination_longitude": destination.get("longitude") if destination else None,
        "destination_photo_url": destination_photo_url
        if destination and destination.get("photo_name")
        else None,
        "weather": [item.model_dump(mode="json") for item in weather] if weather else None,
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
        "status": revision.status,
        "requested_version": revision.base_version,
    }


def get_revision(db: Session, trip_id: str, revision_id: str) -> ItineraryRevision:
    revision = db.scalar(
        select(ItineraryRevision).where(
            ItineraryRevision.id == parse_uuid(revision_id, "revision_id"),
            ItineraryRevision.itinerary_id
            == select(Itinerary.id)
            .where(Itinerary.trip_id == parse_uuid(trip_id, "trip_id"))
            .scalar_subquery(),
        )
    )
    if revision is None:
        raise ItineraryConflictError("Revision does not exist")
    return revision


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
    itinerary.items.clear()
    object_session = Session.object_session(itinerary)
    if object_session is not None:
        object_session.flush()
    itinerary.items = [
        ItineraryItem(
            day_number=item.day_number,
            position=item.position,
            title=item.title,
            estimated_cost=decimal_value(item.estimated_cost),
            semantic_metadata=(
                item.metadata.model_dump(mode="json") if item.metadata is not None else None
            ),
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
                semantic_metadata=(
                    item.metadata.model_dump(mode="json") if item.metadata is not None else None
                ),
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
    draft = run_itinerary_generation_graph(context, provider)
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
    logger.info(
        "itinerary_revision received id=%s trip_id=%s base_version=%s instruction=%r",
        revision.id,
        context.trip_id,
        revision.base_version,
        instruction,
    )
    return revision


def move_itinerary_item(
    db: Session,
    context: TripContext,
    current_member_id: str,
    item_id: str,
    day_number: int,
    position: int,
) -> Itinerary:
    eligible_ids = {
        member.member_id for member in context.eligible_members if member.voting_eligible
    }
    if current_member_id not in eligible_ids:
        raise ItineraryConflictError("Only eligible trip members may move itinerary activities")

    itinerary = require_itinerary(db, context.trip_id, lock=True)
    if itinerary.status != "DRAFT":
        raise ItineraryConflictError(
            f"Itinerary cannot be reordered while it is {itinerary.status.lower()}"
        )

    item_uuid = parse_uuid(item_id, "item_id")
    item = next((candidate for candidate in itinerary.items if candidate.id == item_uuid), None)
    if item is None:
        raise ItineraryConflictError("Itinerary activity does not exist")

    days: dict[int, list[ItineraryItem]] = {}
    for candidate in sorted(itinerary.items, key=lambda value: (value.day_number, value.position)):
        days.setdefault(candidate.day_number, []).append(candidate)
    if day_number > max(days, default=0):
        raise ItineraryInvalidError("Destination day is outside the itinerary")

    source_day = item.day_number
    source_position = item.position
    if source_day != day_number:
        raise ItineraryInvalidError("Cross-day changes require a target activity to swap with")
    if source_day == day_number and source_position == position:
        return itinerary

    source_items = days[source_day]
    destination_items = days.setdefault(day_number, [])
    source_items.remove(item)
    if source_day == day_number:
        if position > len(source_items) + 1:
            raise ItineraryInvalidError("Destination position is outside the selected day")
        source_items.insert(position - 1, item)
    else:
        if position > len(destination_items) + 1:
            raise ItineraryInvalidError("Destination position is outside the selected day")
        destination_items.insert(position - 1, item)

    affected_items = [candidate for values in days.values() for candidate in values]
    temporary_position = len(itinerary.items) + 1
    for candidate in affected_items:
        candidate.position = temporary_position
        temporary_position += 1
    db.flush()

    for affected_day, values in days.items():
        for index, candidate in enumerate(values, start=1):
            candidate.day_number = affected_day
            candidate.position = index

    itinerary.version += 1
    db.execute(delete(ItineraryApproval).where(ItineraryApproval.itinerary_id == itinerary.id))
    db.flush()
    db.commit()
    return require_itinerary(db, context.trip_id)


def swap_itinerary_items(
    db: Session,
    context: TripContext,
    current_member_id: str,
    source_item_id: str,
    target_item_id: str,
) -> Itinerary:
    eligible_ids = {
        member.member_id for member in context.eligible_members if member.voting_eligible
    }
    if current_member_id not in eligible_ids:
        raise ItineraryConflictError("Only eligible trip members may swap itinerary activities")

    itinerary = require_itinerary(db, context.trip_id, lock=True)
    if itinerary.status != "DRAFT":
        raise ItineraryConflictError(
            f"Itinerary cannot be reordered while it is {itinerary.status.lower()}"
        )

    source_id = parse_uuid(source_item_id, "source_activity_id")
    target_id = parse_uuid(target_item_id, "target_activity_id")
    if source_id == target_id:
        return itinerary

    items_by_id = {item.id: item for item in itinerary.items}
    source = items_by_id.get(source_id)
    target = items_by_id.get(target_id)
    if source is None or target is None:
        raise ItineraryConflictError("Both swap activities must belong to this itinerary")

    source_day, source_position = source.day_number, source.position
    target_day, target_position = target.day_number, target.position
    if source_day == target_day:
        return move_itinerary_item(
            db,
            context,
            current_member_id,
            source_item_id,
            target_day,
            target_position,
        )

    original_slots = {item.id: (item.day_number, item.position) for item in itinerary.items}
    temporary_position = len(itinerary.items) + 1
    for item in itinerary.items:
        item.position = temporary_position
        temporary_position += 1
    db.flush()

    for item in itinerary.items:
        if item.id == source.id:
            item.day_number, item.position = target_day, target_position
        elif item.id == target.id:
            item.day_number, item.position = source_day, source_position
        else:
            item.day_number, item.position = original_slots[item.id]
    itinerary.version += 1
    db.execute(delete(ItineraryApproval).where(ItineraryApproval.itinerary_id == itinerary.id))
    db.flush()
    db.commit()
    return require_itinerary(db, context.trip_id)


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
    revision = request_revision(db, context, current_member_id, revision_input)
    return execute_claimed_ai_revision(db, context, str(revision.id), provider)


def execute_claimed_ai_revision(
    db: Session,
    context: TripContext,
    revision_id: str,
    provider: ItineraryRevisionProvider,
) -> ItineraryRevision:
    """Run a committed revision claim without holding a DB transaction during the provider call."""
    revision = db.scalar(
        select(ItineraryRevision).where(
            ItineraryRevision.id == parse_uuid(revision_id, "revision_id"),
            ItineraryRevision.status == "RUNNING",
        )
    )
    if revision is None:
        raise ItineraryConflictError("Revision is no longer active")

    try:
        itinerary = require_itinerary(db, context.trip_id)
        current_draft = ItineraryDraft(
            items=[
                ItineraryItemInput(
                    day_number=item.day_number,
                    position=item.position,
                    title=item.title,
                    estimated_cost=money_value(item.estimated_cost),
                    metadata=(
                        ItineraryItemMetadata.model_validate(item.semantic_metadata)
                        if item.semantic_metadata is not None
                        else None
                    ),
                )
                for item in itinerary.items
            ],
            estimated_total_cost=money_value(itinerary.estimated_total_cost),
        )
        base_version = revision.base_version
        instruction = revision.instruction
        logger.info(
            "itinerary_revision graph_started id=%s trip_id=%s base_version=%s instruction=%r",
            revision_id,
            context.trip_id,
            base_version,
            instruction,
        )

        # End the read transaction before the external provider call.
        db.commit()
        revised_draft = run_itinerary_revision_graph(
            context,
            current_draft,
            base_version,
            instruction,
            provider,
            allow_persisted_metadata_revisions=True,
        )
        completed = complete_revision(db, revision_id, revised_draft)
        logger.info(
            "itinerary_revision persisted id=%s status=%s final_version=%s",
            revision_id,
            completed.status,
            require_itinerary(db, context.trip_id).version,
        )
        return completed
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


def run_revision_background(
    revision_id: str,
    trip_id: str,
    provider: ItineraryRevisionProvider | None,
    session_factory: sessionmaker[Session] | None = None,
) -> None:
    """Execute a committed revision claim using a fresh background-task session."""
    factory = session_factory or SessionLocal
    with factory() as db:
        try:
            revision = db.scalar(
                select(ItineraryRevision).where(
                    ItineraryRevision.id == parse_uuid(revision_id, "revision_id")
                )
            )
            if revision is None or revision.status != "RUNNING":
                return

            if provider is None:
                cleanup_failed_revision(db, revision_id)
                return

            context = build_trip_context(db, trip_id)
            logger.info("itinerary_revision provider_started id=%s", revision_id)
            execute_claimed_ai_revision(db, context, revision_id, provider)
        except Exception:
            logger.exception("Background itinerary revision failed: %s", revision_id)
            cleanup_failed_revision(db, revision_id)


def approval_progress(
    db: Session,
    itinerary: Itinerary,
    eligible_member_ids: set[str],
    current_member_id: str | None = None,
    eligible_members: list[EligibleMember] | None = None,
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
    approval_rows = (
        db.scalars(
            select(ItineraryApproval).where(
                ItineraryApproval.itinerary_id == itinerary.id,
                ItineraryApproval.version == itinerary.version,
                ItineraryApproval.trip_member_id.in_(eligible_member_uuids),
            )
        ).all()
        if eligible_member_uuids
        else []
    )
    approved_by_member = {
        str(approval.trip_member_id): approval.approved for approval in approval_rows
    }
    members = [
        ApprovalMember(
            member_id=member.member_id,
            display_name=member.display_name,
            approved=approved_by_member.get(member.member_id, False),
        )
        for member in sorted(
            (member for member in (eligible_members or [])),
            key=lambda member: member.display_name.lower(),
        )
    ]
    return ApprovalProgress(
        version=itinerary.version,
        approved_count=approved_count,
        eligible_count=len(eligible_member_ids),
        finalized=itinerary.status == "FINALIZED",
        members=members,
        current_member_id=current_member_id,
    )


def get_approval_progress(db: Session, context: TripContext) -> ApprovalProgress:
    itinerary = require_itinerary(db, context.trip_id)
    eligible_members = [member for member in context.eligible_members if member.voting_eligible]
    eligible_ids = {member.member_id for member in eligible_members}
    return approval_progress(db, itinerary, eligible_ids, eligible_members=eligible_members)


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
        trip = db.get(Trip, itinerary.trip_id)
        if trip is not None:
            trip.state = "FINALIZED"

    db.commit()
    return {
        "member_id": str(approval.trip_member_id),
        "version": approval.version,
        "approved": approval.approved,
    }
