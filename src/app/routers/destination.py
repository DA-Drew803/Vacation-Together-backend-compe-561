import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_trip_member, get_itinerary_provider
from app.models.member import TripMember
from app.schemas.destination import (
    CandidateResponse,
    CloseVotingRequest,
    VoteInput,
    VoteResponse,
    VotingProgress,
    VotingStateResponse,
)
from app.services import destination as destination_service
from app.services import itinerary as itinerary_service
from app.services import member as member_service
from app.services.destination import (
    DestinationInvalidError,
    close_voting,
    start_voting,
)
from app.services.openai_itinerary import ItineraryProviderError, OpenAIItineraryProvider

router = APIRouter(
    prefix="/trips/{trip_id}",
    tags=["destination"],
)


def invalid_response(error: DestinationInvalidError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=str(error),
    )


@router.get(
    "/destination_candidates",
    response_model=list[CandidateResponse],
)
def read_destination_candidates(
    trip_id: Annotated[str, Path()],
    db: Annotated[Session, Depends(get_db)],
    current_member: Annotated[TripMember, Depends(get_current_trip_member)],
) -> list[dict]:
    try:
        candidates = destination_service.get_candidates_for_trip(
            db,
            trip_id,
        )

        return [
            destination_service.serialize_candidate_contract(candidate) for candidate in candidates
        ]

    except DestinationInvalidError as error:
        raise invalid_response(error) from error


@router.post(
    "/destination_candidates/generate",
    response_model=list[CandidateResponse],
    status_code=status.HTTP_202_ACCEPTED,
)
def generate_destination_candidates(
    trip_id: Annotated[uuid.UUID, Path()],
    db: Annotated[Session, Depends(get_db)],
    current_member: Annotated[TripMember, Depends(get_current_trip_member)],
) -> list[dict]:
    del current_member
    try:
        candidates = destination_service.generate_candidates(db, trip_id)
        return [
            destination_service.serialize_candidate_contract(candidate) for candidate in candidates
        ]
    except (DestinationInvalidError, member_service.MemberConflictError) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.get(
    "/votes/me",
    response_model=VoteResponse,
)
def read_my_ballot(
    trip_id: Annotated[str, Path()],
    db: Annotated[Session, Depends(get_db)],
    current_member: Annotated[TripMember, Depends(get_current_trip_member)],
) -> dict:
    try:
        votes = destination_service.get_votes_for_member(
            db,
            trip_id,
            current_member.id,
        )

        return destination_service.serialize_vote_contract(votes, current_member.id)

    except DestinationInvalidError as error:
        raise invalid_response(error) from error


@router.put(
    "/votes/me",
    response_model=VoteResponse,
)
def update_my_ballot(
    trip_id: Annotated[str, Path()],
    ballot: VoteInput,
    db: Annotated[Session, Depends(get_db)],
    current_member: Annotated[TripMember, Depends(get_current_trip_member)],
) -> dict:
    try:
        votes = destination_service.update_ballot(
            db,
            trip_id,
            current_member.id,
            [ballot.candidate_id],
        )

        return destination_service.serialize_vote_contract(votes, current_member.id)

    except DestinationInvalidError as error:
        raise invalid_response(error) from error


@router.get("/voting_state", response_model=VotingProgress)
def read_voting_state(
    trip_id: Annotated[uuid.UUID, Path()],
    db: Annotated[Session, Depends(get_db)],
    current_member: Annotated[TripMember, Depends(get_current_trip_member)],
) -> dict:
    del current_member
    return destination_service.get_voting_progress(db, trip_id)


@router.post("/voting/start", response_model=VotingStateResponse)
def begin_voting(
    trip_id: Annotated[str, Path()],
    db: Annotated[Session, Depends(get_db)],
    current_member: Annotated[TripMember, Depends(get_current_trip_member)],
) -> dict:
    try:
        members = member_service.list_members(
            db,
            current_member.trip_id,
            current_member.user_id,
        )

        eligible_member_ids = [member.id for member, _user in members if member.voting_eligible]

        state = start_voting(
            db,
            trip_id,
            eligible_member_ids,
        )

        return {
            "trip_id": state.trip_id,
            "status": state.status,
            "selected_candidate_id": state.selected_candidate_id,
        }

    except DestinationInvalidError as error:
        raise invalid_response(error) from error


@router.post("/voting/close", response_model=VotingStateResponse)
def finish_voting(
    trip_id: Annotated[str, Path()],
    db: Annotated[Session, Depends(get_db)],
    current_member: Annotated[TripMember, Depends(get_current_trip_member)],
    request: CloseVotingRequest | None = None,
) -> dict:
    try:
        state = close_voting(
            db,
            trip_id,
            current_member.id,
            request.selected_candidate_id if request is not None else None,
        )
        return {
            "trip_id": state.trip_id,
            "status": state.status,
            "selected_candidate_id": state.selected_candidate_id,
        }
    except DestinationInvalidError as error:
        raise invalid_response(error) from error


@router.post("/votes/close", response_model=VotingProgress)
def close_votes(
    trip_id: Annotated[uuid.UUID, Path()],
    db: Annotated[Session, Depends(get_db)],
    current_member: Annotated[TripMember, Depends(get_current_trip_member)],
    provider: Annotated[OpenAIItineraryProvider | None, Depends(get_itinerary_provider)],
) -> dict:
    try:
        if provider is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="OpenAI itinerary generation is not configured",
            )
        destination_service.close_voting(db, trip_id, current_member.id)
        context = itinerary_service.build_trip_context(db, trip_id)
        itinerary_service.generate_and_persist_initial_itinerary(db, context, provider)
        return destination_service.get_voting_progress(db, trip_id)
    except DestinationInvalidError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ItineraryProviderError as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error
