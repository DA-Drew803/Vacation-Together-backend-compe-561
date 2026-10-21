from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.security import create_access_token
from app.models.user import User
from app.schemas.token import TokenResponse
from app.schemas.user import UserCreate, UserResponse
from app.services import user as user_service

router = APIRouter(prefix="/users", tags=["Users"])


@router.post(
    "",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_user(user_data: UserCreate, db: Session = Depends(get_db)):
    try:
        return user_service.create_user(
            db=db,
            email=user_data.email,
            display_name=user_data.display_name,
            password=user_data.password,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error


@router.post("/login", response_model=TokenResponse)
def login_user(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    user = user_service.get_user_by_email(db=db, email=form_data.username)
    if user is None or not user_service.verify_password(
        form_data.password,
        user.password_hash,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return TokenResponse(access_token=create_access_token(str(user.id)))


@router.get("/me", response_model=UserResponse)
def get_current_user_info(
    current_user: User = Depends(get_current_user),
):
    return current_user


@router.get("/{user_id}", response_model=UserResponse)
def get_user(user_id: UUID, db: Session = Depends(get_db)):
    user = user_service.get_user_by_id(db=db, user_id=user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user
