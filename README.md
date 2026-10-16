# Vacation Together Backend

🌎 FastAPI backend for Vacation Together, a group trip planning app for coordinating dates, budgets, destination voting, and shared itineraries.

The backend should follow `Vacation_Together_OpenAPI_Final.yaml` as the API source of truth.

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
    ├── api/v1/        # API router setup
    ├── core/          # settings, auth, database, dependencies
    ├── models/        # SQLAlchemy models
    ├── repository/    # database access functions
    ├── routes/        # FastAPI endpoint modules
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
uv run dev
```

Open:

```text
http://localhost:8000/docs
```

## Run With Docker

Make sure Docker Desktop is running, then:

```bash
docker compose up --build
```

The API is exposed on `localhost:8000`. PostgreSQL runs inside the Docker network as `db:5432` and is not exposed on the host by default.

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
uv run pytest
```

Run tests against Docker PostgreSQL:

```bash
TEST_DATABASE_URL=postgresql+psycopg://vacation_user:vacation_password@localhost:5433/vacation_together uv run pytest
```

With coverage:

```bash
uv run pytest --cov=app --cov-report=term-missing
```
