from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.core.database import create_db_and_tables
from app.routers.auth import router as auth_router
from app.routers.destination import router as destination_router
from app.routers.Invitation import router as invitation_router
from app.routers.itinerary import router as itinerary_router
from app.routers.member import router as member_router
from app.routers.trip import router as trip_router
from app.routers.User import router as user_router

APP_VERSION = "0.1.0"


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

for router in (
    auth_router,
    user_router,
    trip_router,
    member_router,
    invitation_router,
    destination_router,
    itinerary_router,
):
    app.include_router(router, prefix="/api")


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
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
