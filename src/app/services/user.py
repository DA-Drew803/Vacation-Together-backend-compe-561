import hashlib
import hmac
import secrets
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.User import User

PASSWORD_ITERATIONS = 310_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    password_digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return (
        "pbkdf2_sha256$" + str(PASSWORD_ITERATIONS) + "$" + salt.hex() + "$" + password_digest.hex()
    )


def verify_password(password: str, stored_password_hash: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = stored_password_hash.split("$")
        if algorithm != "pbkdf2_sha256":
            return False

        password_digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
        return hmac.compare_digest(password_digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def get_user_by_id(db: Session, user_id: UUID) -> User | None:
    statement = select(User).where(User.id == user_id)
    return db.scalar(statement)


def get_user_by_email(db: Session, email: str) -> User | None:
    statement = select(User).where(User.email == email)
    return db.scalar(statement)


def create_user(
    db: Session,
    email: str,
    display_name: str,
    password: str,
) -> User:
    if get_user_by_email(db, email) is not None:
        raise ValueError("A user with this email already exists")

    user = User(
        email=email,
        display_name=display_name,
        password_hash=hash_password(password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
