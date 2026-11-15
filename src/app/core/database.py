from collections.abc import Generator
from os import getenv

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

load_dotenv()


class Base(DeclarativeBase):
    pass


DATABASE_URL = getenv("DATABASE_URL", "sqlite:///./vacation_together.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_db_and_tables() -> None:
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    if "itinerary_items" in inspect(engine).get_table_names():
        columns = {column["name"] for column in inspect(engine).get_columns("itinerary_items")}
        if "metadata" not in columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE itinerary_items ADD COLUMN metadata JSON"))
