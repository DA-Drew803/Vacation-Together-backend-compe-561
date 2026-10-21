import secrets
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models.Invitation import Invitation
from datetime import datetime, timezone
from app.models.member import TripMember
from app.services import member as member_service


def get_invitation(
    db: Session,
    invitation_id: UUID,
) -> Invitation | None:
    statement = select(Invitation).where(
        Invitation.id == invitation_id
    )
    return db.scalar(statement)


def get_invitation_by_token(
    db: Session,
    token: str,
) -> Invitation | None:
    statement = select(Invitation).where(
        Invitation.token == token
    )
    return db.scalar(statement)


def list_trip_invitations(
    db: Session,
    trip_id: UUID,
) -> list[Invitation]:
    statement = (
        select(Invitation)
        .where(Invitation.trip_id == trip_id)
        .order_by(Invitation.created_at.desc())
    )

    return list(db.scalars(statement).all())


def create_invitation(
    db: Session,
    trip_id: UUID,
    inviter_id: UUID,
    invitee_email: str,
) -> Invitation:
    invitation = Invitation(
        trip_id=trip_id,
        inviter_id=inviter_id,
        invitee_email=invitee_email,
        token=secrets.token_urlsafe(32),
        status="PENDING",
    )

    db.add(invitation)
    db.commit()
    db.refresh(invitation)

    return invitation


def update_invitation_status(
    db: Session,
    invitation: Invitation,
    status: str,
) -> Invitation:
    invitation.status = status

    db.commit()
    db.refresh(invitation)

    return invitation

def accept_invitation(
    db: Session,
    invitation: Invitation,
    user_id: UUID,
    user_email: str,
) -> TripMember:
    if invitation.status != "PENDING":
        raise ValueError("Invitation is unavailable")

    expires_at = invitation.expires_at

    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at <= datetime.now(timezone.utc):
        invitation.status = "EXPIRED"
        db.commit()
        raise ValueError("Invitation has expired")

    if invitation.invitee_email.lower() != user_email.lower():
        raise ValueError(
            "This invitation was sent to a different email address"
        )

    existing_member = member_service.get_trip_member_by_user(
        db=db,
        trip_id=invitation.trip_id,
        user_id=user_id,
    )

    if existing_member is not None:
        raise ValueError("User is already a member of this trip")

    try:
        member = TripMember(
            trip_id=invitation.trip_id,
            user_id=user_id,
            role="MEMBER",
        )

        invitation.status = "ACCEPTED"

        db.add(member)
        db.commit()
        db.refresh(member)

        return member

    except Exception:
        db.rollback()
        raise

def revoke_invitation(
    db: Session,
    invitation: Invitation,
) -> Invitation:
    if invitation.status != "PENDING":
        raise ValueError(
            "Only pending invitations can be revoked"
        )

    invitation.status = "REVOKED"

    db.commit()
    db.refresh(invitation)

    return invitation