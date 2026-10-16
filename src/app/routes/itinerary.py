from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_member_id, get_trip_context
from app.schemas.itinerary import (
    Approval,
    ApprovalInput,
    ApprovalProgress,
    Itinerary,
    Revision,
    RevisionInput,
    TripContext,
)
from app.services import itinerary as itinerary_service
from app.services.itinerary import ItineraryConflictError, ItineraryInvalidError

router = APIRouter(prefix="/trips/{trip_id}", tags=["itinerary"])


def conflict_response(error: ItineraryConflictError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=str(error),
    )


def invalid_response(error: ItineraryInvalidError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=str(error),
    )


@router.get("/itinerary", response_model=Itinerary)
def read_itinerary(
    trip_id: Annotated[str, Path()],
    db: Annotated[Session, Depends(get_db)],
    context: Annotated[TripContext, Depends(get_trip_context)],
) -> dict:
    try:
        itinerary = itinerary_service.require_itinerary(db, trip_id)
        return itinerary_service.serialize_itinerary(itinerary, context.budget.currency)
    except ItineraryConflictError as error:
        raise conflict_response(error) from error


@router.post(
    "/itinerary/revisions",
    response_model=Revision,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_revision(
    trip_id: Annotated[str, Path()],
    revision_input: RevisionInput,
    db: Annotated[Session, Depends(get_db)],
    context: Annotated[TripContext, Depends(get_trip_context)],
    current_member_id: Annotated[str, Depends(get_current_member_id)],
) -> dict:
    try:
        revision = itinerary_service.request_revision(
            db,
            trip_id,
            current_member_id,
            revision_input,
        )
        return itinerary_service.serialize_revision(revision)
    except ItineraryConflictError as error:
        raise conflict_response(error) from error
    except ItineraryInvalidError as error:
        raise invalid_response(error) from error


@router.get("/itinerary/approval", response_model=ApprovalProgress)
def read_approval_progress(
    trip_id: Annotated[str, Path()],
    db: Annotated[Session, Depends(get_db)],
    context: Annotated[TripContext, Depends(get_trip_context)],
) -> ApprovalProgress:
    try:
        return itinerary_service.get_approval_progress(
            db,
            context.model_copy(update={"trip_id": trip_id}),
        )
    except ItineraryConflictError as error:
        raise conflict_response(error) from error


@router.put("/itinerary/approval/me", response_model=Approval)
def approve_itinerary(
    trip_id: Annotated[str, Path()],
    approval_input: ApprovalInput,
    db: Annotated[Session, Depends(get_db)],
    context: Annotated[TripContext, Depends(get_trip_context)],
    current_member_id: Annotated[str, Depends(get_current_member_id)],
) -> dict:
    try:
        return itinerary_service.approve_current_version(
            db,
            context.model_copy(update={"trip_id": trip_id}),
            current_member_id,
            approval_input,
        )
    except ItineraryConflictError as error:
        raise conflict_response(error) from error
