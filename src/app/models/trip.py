from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def new_id() -> UUID:
    return uuid4()


class Trip(Base):
    __tablename__ = "trips"

    __table_args__ = (
        CheckConstraint(
            "state IN ('PLANNING', 'VOTING', 'CLOSED', 'FINALIZED')",
            name="ck_trip_state",
        ),
        CheckConstraint(
            "transportation_preference IN ('AUTO', 'FLIGHT', 'ROAD_TRIP')",
            name="ck_trip_transportation_preference",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=new_id,
    )

    name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    state: Mapped[str] = mapped_column(
        String(32),
        default="PLANNING",
        nullable=False,
    )

    selected_destination_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("destinations.id"),
        nullable=True,
    )

    voting_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    voting_closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    selected_start_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
    )

    selected_end_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
    )

    departure_location: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )

    transportation_preference: Mapped[str] = mapped_column(
        String(20),
        default="AUTO",
        nullable=False,
    )
