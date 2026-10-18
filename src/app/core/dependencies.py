from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import HTTPException, Path, status

from app.schemas.itinerary import TripContext


def _integration_unavailable(name: str) -> NoReturn:
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=f"{name} integration is not available",
    )


async def get_current_member_id() -> UUID:
    """Integration boundary for the authenticated trip member."""
    _integration_unavailable("Authentication")


async def get_trip_context(trip_id: Annotated[UUID, Path()]) -> TripContext:
    """Integration boundary for trip, destination, voting, and membership data."""
    del trip_id
    _integration_unavailable("Trip context")
