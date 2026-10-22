from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.security import create_access_token
from app.models.user import User
from app.schemas.token import AuthTokenResponse
from app.schemas.user import UserCreate, UserLogin, UserResponse
from app.services import user as user_service

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/register", response_model=AuthTokenResponse, status_code=status.HTTP_201_CREATED)
def register_user(
    user_data: UserCreate, db: Annotated[Session, Depends(get_db)]
) -> AuthTokenResponse:
    try:
        user = user_service.create_user(
            db=db,
            email=user_data.email,
            display_name=user_data.display_name,
            password=user_data.password,
        )
        return AuthTokenResponse(
            access_token=create_access_token(str(user.id)),
            user=UserResponse.model_validate(user),
        )
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post("/login", response_model=AuthTokenResponse)
def login_user(user_data: UserLogin, db: Annotated[Session, Depends(get_db)]) -> AuthTokenResponse:
    user = user_service.get_user_by_email(db=db, email=user_data.email)
    if user is None or not user_service.verify_password(user_data.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return AuthTokenResponse(
        access_token=create_access_token(str(user.id)),
        user=UserResponse.model_validate(user),
    )


@router.get("/me", response_model=UserResponse)
def read_current_user(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    return current_user
