from datetime import date
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlmodel import Field, Session, SQLModel, create_engine, select

class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)

class Trip(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)

class TripMember(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)

class Invitation(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)

class MemberAvailability(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    trip_member_id: int = Field(foreign_key="TripMember.id")
    start_date: list[date] = Field(sa_column_kwargs={"type_": "DATE"})
    end_date: list[date] = Field(sa_column_kwargs={"type_": "DATE"})

class MemberBudget(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    trip_member_id: int = Field(foreign_key="TripMember.id")
    max_amount: int

class MemberPreference(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    trip_member_id: int = Field(foreign_key="TripMember.id")
    interests: list[str] = Field(sa_column_kwargs={"type_": "JSON"})

class Destination(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)

class DestinationCandidate(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)

class DestinationVote(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)

class Itinerary(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)

class ItineraryItem(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)

class ItineraryRevision(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)

class ItineraryApproval(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
