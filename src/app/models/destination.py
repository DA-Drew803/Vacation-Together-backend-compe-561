import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Destination(Base):
    __tablename__ = "destinations"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )

    name: Mapped[str] = mapped_column(
        String,
        nullable=False,
    )

    country_code: Mapped[str | None] = mapped_column(
        String(2),
        nullable=True,
    )

    latitude: Mapped[Decimal | None] = mapped_column(
        Numeric(9, 6),
        nullable=True,
    )

    longitude: Mapped[Decimal | None] = mapped_column(
        Numeric(9, 6),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    candidates: Mapped[list["DestinationCandidate"]] = relationship(
        back_populates="destination",
    )


class DestinationCandidate(Base):
    __tablename__ = "destination_candidates"
    __table_args__ = (
        UniqueConstraint(
            "trip_id",
            "destination_id",
            name="uq_destination_candidate_trip_destination",
        ),
        CheckConstraint(
            "match_score >= 0 AND match_score <= 1",
            name="ck_destination_candidate_match_score",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )

    trip_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("trips.id"),
        nullable=False,
        index=True,
    )

    destination_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("destinations.id"),
        nullable=False,
        index=True,
    )

    match_score: Mapped[Decimal] = mapped_column(
        Numeric(5, 4),
        nullable=False,
    )

    match_explanation: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    estimated_cost_per_person: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
    )

    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
    )

    recommendation_data_status: Mapped[str] = mapped_column(
        String,
        default="pending",
        nullable=False,
    )

    weather_snapshot: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    destination: Mapped["Destination"] = relationship(
        back_populates="candidates",
    )

    votes: Mapped[list["DestinationVote"]] = relationship(
        back_populates="candidate",
    )


class DestinationVote(Base):
    __tablename__ = "destination_votes"
    __table_args__ = (
        UniqueConstraint(
            "trip_id",
            "trip_member_id",
            name="uq_destination_vote_member_trip",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )

    trip_member_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("trip_members.id"),
        nullable=False,
        index=True,
    )

    trip_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("trips.id"),
        nullable=False,
        index=True,
    )

    destination_candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("destination_candidates.id"),
        nullable=False,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    candidate: Mapped["DestinationCandidate"] = relationship(
        back_populates="votes",
    )


class DestinationVotingState(Base):
    """Frozen voting inputs and the deterministic result for one trip."""

    __tablename__ = "destination_voting_states"

    trip_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    frozen_member_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    frozen_candidate_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    selected_candidate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    closed_by_member_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
