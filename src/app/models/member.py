from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class TripMember(Base):
    __tablename__ = "trip_members"

    __table_args__ = (
        UniqueConstraint(
            "trip_id",
            "user_id",
            name="uq_trip_member_trip_user",
        ),
        CheckConstraint(
            "role IN ('OWNER', 'MEMBER')",
            name="ck_trip_member_role",
        ),
        CheckConstraint(
            "status IN ('active', 'removed')",
            name="ck_trip_member_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid4,
    )

    trip_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("trips.id"),
        nullable=False,
        index=True,
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )

    role: Mapped[str] = mapped_column(
        String(32),
        default="MEMBER",
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(32),
        default="active",
        nullable=False,
    )

    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    preference: Mapped[MemberPreference | None] = relationship(
        back_populates="trip_member",
        cascade="all, delete-orphan",
        uselist=False,
    )


class MemberPreference(Base):
    __tablename__ = "member_preferences"

    __table_args__ = (
        UniqueConstraint("trip_member_id", name="uq_member_preference_trip_member"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    trip_member_id: Mapped[UUID] = mapped_column(
        ForeignKey("trip_members.id"),
        nullable=False,
        index=True,
    )
    interests: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    free_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    trip_member: Mapped[TripMember] = relationship(back_populates="preference")
