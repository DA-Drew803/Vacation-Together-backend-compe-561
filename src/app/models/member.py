from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def new_id() -> UUID:
    return uuid4()


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

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    trip_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("trips.id"), nullable=False, index=True)
    user_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(32), default="MEMBER", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", nullable=False)
    voting_eligible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
