from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


# Store trip member data in the database.
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
            "status IN ('ACTIVE', 'REMOVED')",
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
        default="ACTIVE",
        nullable=False,
    )

    voting_eligible: Mapped[bool] = mapped_column(default=True, nullable=False)

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

    availabilities: Mapped[list[MemberAvailability]] = relationship(
        back_populates="trip_member",
        cascade="all, delete-orphan",
        order_by="MemberAvailability.start_date",
    )
    budget: Mapped[MemberBudget | None] = relationship(
        back_populates="trip_member",
        cascade="all, delete-orphan",
        uselist=False,
    )


# Store member availability data in the database.
class MemberAvailability(Base):
    __tablename__ = "member_availability"
    __table_args__ = (
        CheckConstraint("start_date <= end_date", name="ck_member_availability_date_order"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    trip_member_id: Mapped[UUID] = mapped_column(
        ForeignKey("trip_members.id"), nullable=False, index=True
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)

    trip_member: Mapped[TripMember] = relationship(back_populates="availabilities")


# Store member budget data in the database.
class MemberBudget(Base):
    __tablename__ = "member_budgets"
    __table_args__ = (CheckConstraint("max_amount > 0", name="ck_member_budget_positive"),)

    trip_member_id: Mapped[UUID] = mapped_column(ForeignKey("trip_members.id"), primary_key=True)
    max_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)

    trip_member: Mapped[TripMember] = relationship(back_populates="budget")


# Store member preference data in the database.
class MemberPreference(Base):
    __tablename__ = "member_preferences"

    trip_member_id: Mapped[UUID] = mapped_column(ForeignKey("trip_members.id"), primary_key=True)
    interests: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    private_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    trip_member: Mapped[TripMember] = relationship(back_populates="preference")
