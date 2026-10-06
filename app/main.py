"""FastAPI application factory and entry point.

Run locally with:  uvicorn app.main:app --reload
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.config import get_settings
from app.database import init_db
from app.routers import certificates, jobs


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    if settings.auto_create_tables:
        init_db()
    logging.getLogger(__name__).info("%s started (env=%s)", settings.app_name, settings.environment)
    yield


def create_app() -> FastAPI:
    """Application factory. Tests build a fresh app per test, so dependency
    overrides never leak between tests."""
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "Submit a batch of recipients, generate one PDF certificate per recipient in the background, "
            "poll job progress and download each certificate."
        ),
        lifespan=lifespan,
    )
    app.include_router(jobs.router)
    app.include_router(certificates.router)

    @app.get("/health", tags=["health"], summary="Liveness probe")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
