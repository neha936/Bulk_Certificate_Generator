"""Database engine, session factory and FastAPI session dependencies.

Design note: we use *synchronous* SQLAlchemy 2.0. FastAPI runs `def` endpoints
and `def` background tasks in a thread pool, so blocking DB calls never block
the event loop. ReportLab is also synchronous and CPU-bound, so an async stack
would add complexity without a throughput win for this workload.
"""

from collections.abc import Iterator

from fastapi import Depends
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base shared by all ORM models."""


def build_engine(database_url: str) -> Engine:
    """Create an engine with backend-specific options."""
    if database_url.startswith("sqlite"):
        # SQLite connections are bound to the creating thread by default.
        # FastAPI's thread pool and background tasks use other threads.
        return create_engine(database_url, connect_args={"check_same_thread": False})
    # pool_pre_ping transparently replaces connections dropped by the server
    # (e.g. after a PostgreSQL restart) instead of failing the first request.
    return create_engine(database_url, pool_pre_ping=True, pool_size=10, max_overflow=20)


engine = build_engine(get_settings().database_url)

# expire_on_commit=False lets us return ORM objects after commit without
# triggering lazy reloads, which matters when we commit per certificate.
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """Create tables if they don't exist. Use Alembic migrations in production."""
    from app import models  # noqa: F401  (import registers the models on Base.metadata)

    Base.metadata.create_all(bind=engine)


def get_session_factory() -> sessionmaker[Session]:
    """Return the session factory.

    This is a dependency, not a module global used directly. Background tasks
    must open their *own* session because the request-scoped session is closed
    once the response is sent. Tests override this to inject an in-memory DB.
    """
    return SessionLocal


def get_db(session_factory: sessionmaker[Session] = Depends(get_session_factory)) -> Iterator[Session]:
    """Request-scoped session. It is always closed, even when the request fails."""
    with session_factory() as session:
        yield session
