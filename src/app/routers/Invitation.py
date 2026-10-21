from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_trip_owner
from app.models.member import TripMember
from app.models.User import User
from app.schemas.invitation import InvitationCreate, InvitationCreateResponse
from app.schemas.member import TripMemberResponse
from app.services import invitation as invitation_service

router = APIRouter(tags=["Invitations"])


@router.post(
    "/trips/{trip_id}/invitations",
    response_model=InvitationCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_invitation(
    trip_id: UUID,
    invitation_data: InvitationCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    owner_membership: Annotated[TripMember, Depends(require_trip_owner)],
    db: Annotated[Session, Depends(get_db)],
) -> InvitationCreateResponse:
    del owner_membership
    invitation, raw_token = invitation_service.create_invitation(
        db=db,
        trip_id=trip_id,
        inviter_id=current_user.id,
        invitee_email=invitation_data.invitee_email,
    )
    response = InvitationCreateResponse.model_validate(invitation)
    return response.model_copy(update={"token": raw_token})


@router.post(
    "/invitations/{token}/accept",
    response_model=TripMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
def accept_invitation(
    token: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TripMember:
    invitation = invitation_service.get_invitation_by_token(db=db, token=token)
    if invitation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invitation is invalid",
        )

    try:
        return invitation_service.accept_invitation(
            db=db,
            invitation=invitation,
            user_id=current_user.id,
            user_email=current_user.email,
        )
    except ValueError as error:
        message = str(error)
        error_status = (
            status.HTTP_410_GONE
            if "expired" in message.lower()
            else status.HTTP_403_FORBIDDEN
            if "different email" in message.lower()
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(status_code=error_status, detail=message) from error
