from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_member_id
from app.schemas.destination import (
    DestinationCandidateListResponse,
    VoteBallot,
    VoteBallotUpdateRequest,
)
from app.services import destination as destination_service
from app.services.destination import DestinationInvalidError

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
    current_member_id: Annotated[
        str,
        Depends(get_current_member_id),
    ],
) -> dict: 
    try:
        votes = destination_service.get_votes_for_member(
            db,
            current_member_id,
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
    current_member_id: Annotated[
        str,
        Depends(get_current_member_id),
    ],
) -> dict:
    try:
        votes = destination_service.update_ballot(
            db,
            trip_id,
            current_member_id,
            [
                selection.destination_candidate_id
                for selection in ballot.selections
            ],
        )

        return destination_service.serialize_ballot(votes)

    except DestinationInvalidError as error:
        raise invalid_response(error) from error
