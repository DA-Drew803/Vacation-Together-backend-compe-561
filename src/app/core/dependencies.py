from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Path, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import decode_access_token, oauth2_scheme
from app.models.member import TripMember
from app.models.user import User
from app.schemas.itinerary import TripContext
from app.services import itinerary as itinerary_service
from app.services import member as member_service
from app.services import user as user_service
from app.services.openai_itinerary import ItineraryProviderError, OpenAIItineraryProvider


def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    user_id_text = decode_access_token(token)

    try:
        user_id = UUID(user_id_text)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user identity",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error

    user = user_service.get_user_by_id(db=db, user_id=user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User no longer exists",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


def get_current_trip_member(
    trip_id: Annotated[UUID, Path()],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TripMember:
    membership = member_service.get_trip_member_by_user(
        db=db,
        trip_id=trip_id,
        user_id=current_user.id,
    )

    if membership is None or membership.status != "ACTIVE":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not an active member of this trip",
        )

    return membership


def get_current_member_id(
    membership: Annotated[TripMember, Depends(get_current_trip_member)],
) -> str:
    return str(membership.id)


def get_trip_context(
    trip_id: Annotated[UUID, Path()],
    membership: Annotated[TripMember, Depends(get_current_trip_member)],
    db: Annotated[Session, Depends(get_db)],
) -> TripContext:
    del membership
    try:
        return itinerary_service.build_trip_context(db, trip_id)
    except (itinerary_service.ItineraryConflictError, member_service.MemberConflictError) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


def get_itinerary_provider() -> OpenAIItineraryProvider | None:
    try:
        return OpenAIItineraryProvider.from_env()
    except ItineraryProviderError:
        return None


def require_trip_owner(
    membership: Annotated[TripMember, Depends(get_current_trip_member)],
) -> TripMember:
    if membership.role != "OWNER":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only trip owners can perform this action",
        )

    return membership
