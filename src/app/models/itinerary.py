from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def new_id() -> str:
    return str(uuid4())


class Itinerary(Base):
    __tablename__ = "itineraries"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_itinerary_version_positive"),
        CheckConstraint(
            "status IN ('GENERATING', 'DRAFT', 'REVISING', 'FINALIZED', 'FAILED')",
            name="ck_itinerary_status",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    trip_id: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    destination_id: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String, default="DRAFT", nullable=False)
    estimated_total_cost: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)

    items: Mapped[list["ItineraryItem"]] = relationship(
        back_populates="itinerary",
        cascade="all, delete-orphan",
        order_by="(ItineraryItem.day_number, ItineraryItem.position)",
    )
    revisions: Mapped[list["ItineraryRevision"]] = relationship(
        back_populates="itinerary",
        cascade="all, delete-orphan",
    )
    approvals: Mapped[list["ItineraryApproval"]] = relationship(
        back_populates="itinerary",
        cascade="all, delete-orphan",
    )


class ItineraryItem(Base):
    __tablename__ = "itinerary_items"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    itinerary_id: Mapped[str] = mapped_column(ForeignKey("itineraries.id"), nullable=False)
    day_number: Mapped[int] = mapped_column(Integer, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    estimated_cost: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)

    itinerary: Mapped[Itinerary] = relationship(back_populates="items")


class ItineraryRevision(Base):
    __tablename__ = "itinerary_revisions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'NO_CHANGE')",
            name="ck_itinerary_revision_status",
        ),
        Index(
            "uq_active_itinerary_revision",
            "itinerary_id",
            unique=True,
            postgresql_where=text("status IN ('PENDING', 'RUNNING')"),
            sqlite_where=text("status IN ('PENDING', 'RUNNING')"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    itinerary_id: Mapped[str] = mapped_column(ForeignKey("itineraries.id"), nullable=False)
    requested_by_member_id: Mapped[str] = mapped_column(String, nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, default="PENDING", nullable=False)
    base_version: Mapped[int] = mapped_column(Integer, nullable=False)

    itinerary: Mapped[Itinerary] = relationship(back_populates="revisions")


class ItineraryApproval(Base):
    __tablename__ = "itinerary_approvals"
    __table_args__ = (
        UniqueConstraint(
            "itinerary_id",
            "trip_member_id",
            name="uq_itinerary_approval_member",
        ),
        CheckConstraint("version >= 1", name="ck_itinerary_approval_version_positive"),
    )

    itinerary_id: Mapped[str] = mapped_column(
        ForeignKey("itineraries.id"),
        primary_key=True,
    )
    trip_member_id: Mapped[str] = mapped_column(String, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    itinerary: Mapped[Itinerary] = relationship(back_populates="approvals")
