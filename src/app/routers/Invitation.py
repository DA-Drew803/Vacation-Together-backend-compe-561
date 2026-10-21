from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import (
    get_current_trip_member,
    get_current_user,
    require_trip_owner,
)
from app.models.member import TripMember
from app.models.User import User
from app.schemas.invitation import (
    InvitationCreate,
    InvitationCreateResponse,
    InvitationResponse,
    InvitationUpdate,
)
from app.schemas.member import TripMemberResponse
from app.services import invitation as invitation_service
from app.services import member as member_service

router = APIRouter(tags=["Invitations"])


@router.post(
    "/trips/{trip_id}/invitations",
    response_model=InvitationCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_invitation(
    trip_id: UUID,
    invitation_data: InvitationCreate,
    current_user: User = Depends(get_current_user),
    owner_membership: TripMember = Depends(require_trip_owner),
    db: Session = Depends(get_db),
):
    invitation, raw_token = invitation_service.create_invitation(
        db=db,
        trip_id=trip_id,
        inviter_id=current_user.id,
        invitee_email=invitation_data.invitee_email,
    )
    response = InvitationCreateResponse.model_validate(invitation)
    return response.model_copy(update={"token": raw_token})


@router.get(
    "/trips/{trip_id}/invitations",
    response_model=list[InvitationResponse],
)
def list_trip_invitations(
    trip_id: UUID,
    current_membership: TripMember = Depends(get_current_trip_member),
    db: Session = Depends(get_db),
):
    return invitation_service.list_trip_invitations(db=db, trip_id=trip_id)


@router.get(
    "/invitations/{invitation_id}",
    response_model=InvitationResponse,
)
def get_invitation(
    invitation_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    invitation = invitation_service.get_invitation(db=db, invitation_id=invitation_id)
    if invitation is None:
        raise HTTPException(status_code=404, detail="Invitation not found")

    membership = member_service.get_trip_member_by_user(
        db=db,
        trip_id=invitation.trip_id,
        user_id=current_user.id,
    )
    if membership is None or membership.status != "ACTIVE":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not an active member of this trip",
        )

    return invitation


@router.patch(
    "/invitations/{invitation_id}",
    response_model=InvitationResponse,
)
def update_invitation(
    invitation_id: UUID,
    invitation_data: InvitationUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    invitation = invitation_service.get_invitation(db=db, invitation_id=invitation_id)
    if invitation is None:
        raise HTTPException(status_code=404, detail="Invitation not found")

    membership = member_service.get_trip_member_by_user(
        db=db,
        trip_id=invitation.trip_id,
        user_id=current_user.id,
    )
    if membership is None or membership.status != "ACTIVE":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not an active member of this trip",
        )

    return invitation_service.update_invitation_status(
        db=db,
        invitation=invitation,
        status=invitation_data.status,
    )


@router.post(
    "/invitations/{token}/accept",
    response_model=TripMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
def accept_invitation(
    token: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    invitation = invitation_service.get_invitation_by_token(db=db, token=token)
    if invitation is None:
        raise HTTPException(status_code=404, detail="Invitation is invalid")

    try:
        return invitation_service.accept_invitation(
            db=db,
            invitation=invitation,
            user_id=current_user.id,
            user_email=current_user.email,
        )
    except ValueError as error:
        message = str(error)
        if "expired" in message.lower():
            error_status = status.HTTP_410_GONE
        elif "different email" in message.lower():
            error_status = status.HTTP_403_FORBIDDEN
        else:
            error_status = status.HTTP_409_CONFLICT
        raise HTTPException(status_code=error_status, detail=message) from error


@router.post(
    "/trips/{trip_id}/invitations/{invitation_id}/revoke",
    response_model=InvitationResponse,
)
def revoke_invitation(
    trip_id: UUID,
    invitation_id: UUID,
    owner_membership: TripMember = Depends(require_trip_owner),
    db: Session = Depends(get_db),
):
    invitation = invitation_service.get_invitation(db=db, invitation_id=invitation_id)
    if invitation is None or invitation.trip_id != trip_id:
        raise HTTPException(status_code=404, detail="Invitation not found")

    try:
        return invitation_service.revoke_invitation(db=db, invitation=invitation)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
