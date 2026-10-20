from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.User import User


def get_user_by_id(
    db: Session,
    user_id: UUID,
) -> User | None:
    statement = select(User).where(User.id == user_id)
    return db.scalar(statement)


def get_user_by_email(
    db: Session,
    email: str,
) -> User | None:
    statement = select(User).where(User.email == email)
    return db.scalar(statement)


def create_user(
    db: Session,
    email: str,
    display_name: str,
    password_hash: str,
) -> User:
    existing_user = get_user_by_email(db, email)

    if existing_user is not None:
        raise ValueError("A user with this email already exists")

    user = User(
        email=email,
        display_name=display_name,
        password_hash=password_hash,
    )

    db.add(user)
    db.commit()
    db.refresh(user)

    return user