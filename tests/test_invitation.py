import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.Invitation import Invitation
from app.models.trip import Trip
from app.models.User import User
from app.services.invitation import (
    accept_invitation,
    create_invitation,
    get_invitation_by_token,
)
from app.services.user import create_user


def test_invitation_persists_only_a_hash_and_accepts_raw_token() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as db:
        invitation, raw_token = create_invitation(
            db=db,
            trip_id=uuid.uuid4(),
            inviter_id=uuid.uuid4(),
            invitee_email="member@example.com",
        )

        assert raw_token
        assert invitation.token_hash != raw_token
        assert len(invitation.token_hash) == 64
        assert get_invitation_by_token(db, raw_token) is not None
        assert get_invitation_by_token(db, "wrong-token") is None

    engine.dispose()


def create_invitation_fixture(db: Session) -> tuple[Invitation, str, User, User]:
    trip = Trip(name="Test trip")
    inviter = create_user(db, "owner@example.com", "Owner", "password123")
    invited_user = create_user(db, "member@example.com", "Member", "password123")
    db.add(trip)
    db.commit()
    db.refresh(trip)
    invitation, raw_token = create_invitation(
        db=db,
        trip_id=trip.id,
        inviter_id=inviter.id,
        invitee_email=invited_user.email,
    )
    return invitation, raw_token, inviter, invited_user


def test_accept_invitation_creates_active_member() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as db:
        invitation, raw_token, _, invited_user = create_invitation_fixture(db)
        found = get_invitation_by_token(db, raw_token)
        assert found is invitation

        member = accept_invitation(db, invitation, invited_user.id, invited_user.email)

        assert member.user_id == invited_user.id
        assert member.status == "ACTIVE"
        assert member.voting_eligible is True
        assert invitation.status == "ACCEPTED"

    engine.dispose()


def test_accept_invitation_rejects_expired_token() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as db:
        invitation, _, _, invited_user = create_invitation_fixture(db)
        invitation.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()

        try:
            accept_invitation(db, invitation, invited_user.id, invited_user.email)
        except ValueError as error:
            assert str(error) == "This invitation has expired"
        else:
            raise AssertionError("Expired invitation was accepted")

    engine.dispose()


def test_accept_invitation_is_idempotent_for_same_user() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as db:
        invitation, raw_token, _, invited_user = create_invitation_fixture(db)
        first = accept_invitation(db, invitation, invited_user.id, invited_user.email)
        second_invitation = get_invitation_by_token(db, raw_token)
        assert second_invitation is not None
        second = accept_invitation(db, second_invitation, invited_user.id, invited_user.email)

        assert second.id == first.id
        assert db.query(Invitation).count() == 1

    engine.dispose()
