"""Shared pytest fixtures.

Isolation strategy:
* Each test gets a brand-new **in-memory SQLite** database. StaticPool makes
  every session (request sessions *and* the background task's session) share
  one connection. Without it, each connection would see its own empty
  in-memory DB.
* Each test gets its own temporary storage directory (pytest `tmp_path`).
* We inject both through FastAPI `dependency_overrides`, the same seam the
  production code uses. Tests never touch the real DB or storage folder.
* Starlette's TestClient runs BackgroundTasks synchronously once the response
  is sent, so after `client.post(...)` returns, the job has finished
  processing. Tests stay deterministic with no sleeps or polling.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import PROJECT_ROOT, Settings, get_settings
from app.database import Base, get_session_factory
from app.main import create_app


@pytest.fixture
def engine() -> Iterator[Engine]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def db_session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """A session for assertions made directly against the database."""
    with session_factory() as session:
        yield session


@pytest.fixture
def storage_dir(tmp_path: Path) -> Path:
    path = tmp_path / "certificates"
    path.mkdir()
    return path


@pytest.fixture
def test_settings(storage_dir: Path) -> Settings:
    return Settings(
        database_url="sqlite://",
        storage_dir=storage_dir,
        certificate_layout_path=PROJECT_ROOT / "static" / "templates" / "certificate_layout.json",
        max_recipients_per_job=50,
        auto_create_tables=False,
    )


@pytest.fixture
def client(session_factory: sessionmaker[Session], test_settings: Settings) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_settings] = lambda: test_settings
    # Not used as a context manager, so the lifespan (which creates tables on
    # the real configured DB) never runs. Tables come from the `engine` fixture.
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def job_payload() -> dict:
    return {
        "title": "Python Backend Bootcamp - Autumn 2026",
        "course_name": "Advanced FastAPI & PostgreSQL",
        "issue_date": "2026-10-06",
        "recipients": [
            {"name": "Ada Lovelace", "email": "ada@example.com", "meta": {"grade": "A"}},
            {"name": "Alan Turing", "email": "alan@example.com"},
            {"name": "Grace Hopper", "email": "grace@example.com"},
        ],
    }
