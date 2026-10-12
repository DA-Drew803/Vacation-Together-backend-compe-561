import uvicorn
from fastapi import FastAPI

APP_VERSION = "0.1.0"


app = FastAPI(
    title="Vacation Together API",
    version=APP_VERSION,
    description="Backend API for the Vacation Together group trip planning app.",
)


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
