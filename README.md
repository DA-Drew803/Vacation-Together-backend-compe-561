# Vacation Together Backend

🌎 FastAPI backend for Vacation Together, a group trip planning app for coordinating dates, budgets, destination voting, and shared itineraries.

The backend should follow `Vacation_Together_OpenAPI_Final.yaml` as the API source of truth.

Frontend repository: [Vacation Together Frontend](https://github.com/DA-Drew803/Vacation-Together-frontend-compe-561)

## Stack

- Python 3.12
- FastAPI
- SQLAlchemy
- PostgreSQL
- Pydantic
- Uvicorn
- Pytest
- pre-commit
- uv
- Docker

## Folder Structure

```text
Dockerfile
docker-compose.yml
src/
└── app/
    ├── main.py
    ├── core/          # security, database, dependencies
    ├── models/        # SQLAlchemy models
    ├── repository/    # database access functions
    ├── routers/       # FastAPI endpoint modules
    ├── schemas/       # Pydantic request/response schemas
    └── services/      # business logic
tests/                 # pytest tests
```

## Setup

Install `uv` if needed:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Set up the project:

```bash
uv python pin 3.12
uv venv
source .venv/bin/activate
uv sync
uv run pre-commit install
```

Run hooks manually:

```bash
uv run pre-commit run --all-files
```

## Run Local Server

```bash
cp .env.example .env
uv run dev
```

Set `FRONTEND_ORIGINS` in `.env` to the comma-separated frontend origins allowed by CORS, for example `http://localhost:3000`.

`JWT_SECRET_KEY` is required and should be a random value of at least 32 bytes. `DATABASE_URL` defaults to a local SQLite database when omitted. PostgreSQL is used by the Docker configuration. OpenAI and Google Places keys are optional for registration, trip planning, group matching, curated destination results, and voting; AI itinerary generation/revision and live place details require their respective keys.

Open:

```text
http://localhost:8000/docs
```

## Run With Docker

Make sure Docker Desktop is running, then:

```bash
docker compose up --build
```

The API is exposed on `localhost:8000`. PostgreSQL is available to local tools at `localhost:5432` and to the API container at `db:5432`.

Open:

```text
http://localhost:8000/docs
```

Stop containers:

```bash
docker compose down
```

## Test

```bash
JWT_SECRET_KEY=replace-with-a-test-secret-at-least-32-bytes uv run pytest
uv run ruff check .
```

Run tests against Docker PostgreSQL:

```bash
TEST_DATABASE_URL=postgresql+psycopg://vacation_user:vacation_password@localhost:5432/vacation_together_test uv run pytest
```

With coverage:

```bash
uv run pytest --cov=app --cov-report=term-missing
```
