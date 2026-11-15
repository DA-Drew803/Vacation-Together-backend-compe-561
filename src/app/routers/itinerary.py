from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, status
from fastapi.responses import Response
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import get_db
from app.core.dependencies import (
    get_current_member_id,
    get_itinerary_provider,
    get_trip_context,
)
from app.models.destination import Destination
from app.schemas.itinerary import (
    Approval,
    ApprovalInput,
    ApprovalProgress,
    Error,
    Itinerary,
    Revision,
    RevisionInput,
    TripContext,
)
from app.services import itinerary as itinerary_service
from app.services.itinerary import ItineraryConflictError, ItineraryInvalidError
from app.services.openai_itinerary import OpenAIItineraryProvider
from app.services.places import fetch_photo, resolve_destination

PHASE_5_ERROR_RESPONSES = {
    status.HTTP_400_BAD_REQUEST: {"model": Error, "description": "Invalid request"},
    status.HTTP_401_UNAUTHORIZED: {"model": Error, "description": "Unauthenticated"},
    status.HTTP_403_FORBIDDEN: {"model": Error, "description": "Not a trip member / forbidden"},
    status.HTTP_409_CONFLICT: {"model": Error, "description": "State conflict"},
}

router = APIRouter(
    prefix="/trips/{trip_id}",
    tags=["itinerary"],
    responses=PHASE_5_ERROR_RESPONSES,
)


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
        destination = db.get(Destination, itinerary.destination_id)
        return itinerary_service.serialize_itinerary(
            itinerary,
            context.budget.currency,
            destination_name=context.destination_name,
            destination_location=destination.location if destination is not None else "",
            destination_photo_url=f"/trips/{trip_id}/itinerary/destination-photo",
            weather=context.weather,
        )
    except ItineraryConflictError as error:
        raise conflict_response(error) from error
    except ItineraryInvalidError as error:
        raise invalid_response(error) from error


@router.get("/itinerary/destination-photo")
def read_destination_photo(
    trip_id: Annotated[str, Path()],
    db: Annotated[Session, Depends(get_db)],
    context: Annotated[TripContext, Depends(get_trip_context)],
) -> Response:
    del context
    itinerary = itinerary_service.require_itinerary(db, trip_id)
    destination = db.get(Destination, itinerary.destination_id)
    if destination is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Destination not found")
    resolved = resolve_destination(destination.name, destination.location)
    photo_name = resolved.get("photo_name") if resolved else None
    if not isinstance(photo_name, str):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Destination photo unavailable"
        )
    photo = fetch_photo(photo_name)
    if photo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Destination photo unavailable"
        )
    content, media_type = photo
    return Response(
        content=content,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.post(
    "/itinerary/revisions",
    response_model=Revision,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_revision(
    trip_id: Annotated[str, Path()],
    revision_input: RevisionInput,
    background_tasks: BackgroundTasks,
    db: Annotated[Session, Depends(get_db)],
    context: Annotated[TripContext, Depends(get_trip_context)],
    current_member_id: Annotated[str, Depends(get_current_member_id)],
    provider: Annotated[OpenAIItineraryProvider | None, Depends(get_itinerary_provider)],
) -> dict:
    try:
        effective_context = context.model_copy(update={"trip_id": trip_id})
        revision = itinerary_service.request_revision(
            db, effective_context, current_member_id, revision_input
        )
        background_tasks.add_task(
            itinerary_service.run_revision_background,
            str(revision.id),
            trip_id,
            provider,
            sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=True),
        )
        return itinerary_service.serialize_revision(revision)
    except ItineraryConflictError as error:
        raise conflict_response(error) from error
    except ItineraryInvalidError as error:
        raise invalid_response(error) from error


@router.get("/itinerary/revisions/{revision_id}", response_model=Revision)
def read_revision_status(
    trip_id: Annotated[str, Path()],
    revision_id: Annotated[str, Path()],
    db: Annotated[Session, Depends(get_db)],
    context: Annotated[TripContext, Depends(get_trip_context)],
) -> dict:
    del context
    try:
        revision = itinerary_service.get_revision(db, trip_id, revision_id)
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
    except ItineraryInvalidError as error:
        raise invalid_response(error) from error


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
    except ItineraryInvalidError as error:
        raise invalid_response(error) from error
