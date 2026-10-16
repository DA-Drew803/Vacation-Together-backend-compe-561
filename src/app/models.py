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

class MemberBudget(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)

class MemberPreference(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)

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
