import hashlib
import secrets
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.invitation import Invitation
from app.models.member import TripMember
from app.repository import member as member_repository


def hash_invitation_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def get_invitation(db: Session, invitation_id: UUID) -> Invitation | None:
    statement = select(Invitation).where(Invitation.id == invitation_id)
    return db.scalar(statement)


def get_invitation_by_token(db: Session, token: str) -> Invitation | None:
    token_hash = hash_invitation_token(token)
    statement = select(Invitation).where(Invitation.token_hash == token_hash)
    return db.scalar(statement)


def list_trip_invitations(db: Session, trip_id: UUID) -> list[Invitation]:
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
) -> tuple[Invitation, str]:
    raw_token = secrets.token_urlsafe(32)
    invitation = Invitation(
        trip_id=trip_id,
        inviter_id=inviter_id,
        invitee_email=invitee_email,
        token_hash=hash_invitation_token(raw_token),
        status="PENDING",
    )
    db.add(invitation)
    db.commit()
    db.refresh(invitation)
    return invitation, raw_token


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
    existing_member = member_repository.get_trip_member_by_user(db, invitation.trip_id, user_id)

    if invitation.status == "ACCEPTED" and existing_member is not None:
        return existing_member

    if invitation.status != "PENDING":
        raise ValueError("Invitation is unavailable")

    expires_at = invitation.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        invitation.status = "EXPIRED"
        db.commit()
        raise ValueError("This invitation has expired")

    if invitation.invitee_email.lower() != user_email.lower():
        raise ValueError("This invitation was sent to a different email address")

    if existing_member is not None:
        raise ValueError("User is already a member of this trip")

    try:
        member = member_repository.create_trip_member(db, invitation.trip_id, user_id, "MEMBER")
        invitation.status = "ACCEPTED"
        db.flush()
        db.commit()
        db.refresh(member)
        return member
    except IntegrityError as error:
        db.rollback()
        raise ValueError("User is already a member of this trip") from error
    except Exception:
        db.rollback()
        raise


def revoke_invitation(db: Session, invitation: Invitation) -> Invitation:
    if invitation.status != "PENDING":
        raise ValueError("Only pending invitations can be revoked")
    invitation.status = "REVOKED"
    db.commit()
    db.refresh(invitation)
    return invitation
