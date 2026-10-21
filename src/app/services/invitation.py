import secrets
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.Invitation import Invitation


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