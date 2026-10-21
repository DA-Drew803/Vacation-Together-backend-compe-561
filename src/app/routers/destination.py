from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_trip_member
from app.models.member import TripMember
from app.schemas.destination import (
    CloseVotingRequest,
    DestinationCandidateListResponse,
    VoteBallot,
    VoteBallotUpdateRequest,
    VotingStateResponse,
)
from app.services import destination as destination_service
from app.services import member as member_service
from app.services.destination import (
    DestinationInvalidError,
    close_voting,
    start_voting,
)

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
    response_model=DestinationCandidateListResponse,
)
def read_destination_candidates(
    trip_id: Annotated[str, Path()],
    db: Annotated[Session, Depends(get_db)],
    current_member: Annotated[TripMember, Depends(get_current_trip_member)],
) -> dict:
    try:
        candidates = destination_service.get_candidates_for_trip(
            db,
            trip_id,
        )

        return destination_service.serialize_candidates(candidates)

    except DestinationInvalidError as error:
        raise invalid_response(error) from error


@router.get(
    "/votes/me",
    response_model=VoteBallot,
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

        return destination_service.serialize_ballot(votes)

    except DestinationInvalidError as error:
        raise invalid_response(error) from error


@router.put(
    "/votes/me",
    response_model=VoteBallot,
)
def update_my_ballot(
    trip_id: Annotated[str, Path()],
    ballot: VoteBallotUpdateRequest,
    db: Annotated[Session, Depends(get_db)],
    current_member: Annotated[TripMember, Depends(get_current_trip_member)],
) -> dict:
    try:
        votes = destination_service.update_ballot(
            db,
            trip_id,
            current_member.id,
            [selection.destination_candidate_id for selection in ballot.selections],
        )

        return destination_service.serialize_ballot(votes)

    except DestinationInvalidError as error:
        raise invalid_response(error) from error


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
