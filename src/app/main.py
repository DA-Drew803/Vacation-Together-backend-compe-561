from contextlib import asynccontextmanager
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI
from sqlmodel import Session

import app.models
from app.core.database import create_db_and_tables, get_db
from app.routers.itinerary import router as itinerary_router
from app.routers.member import router as member_router
from app.routers.user import router as user_router

APP_VERSION = "0.1.0"


SessionDep = Annotated[Session, Depends(get_db)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    yield


app = FastAPI(
    title="Vacation Together API",
    version=APP_VERSION,
    description="Backend API for the Vacation Together group trip planning app.",
    lifespan=lifespan,
)


app.include_router(itinerary_router, prefix="/api")
app.include_router(user_router, prefix="/api")
app.include_router(member_router, prefix="/api")


@app.get("/")
def read_root() -> dict[str, str | bool]:
    return {
        "name": "Vacation Together API",
        "version": APP_VERSION,
        "status": "ok",
        "docs": "/docs",
    }


@app.get("/health")
def health_check() -> dict[str, str | bool]:
    return {"status": "ok", "ready": True}


def run_dev() -> None:
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
    )
