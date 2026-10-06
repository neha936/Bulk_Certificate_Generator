"""Application configuration.

Settings follow the 12-factor approach: every value can be overridden through
environment variables (or a local `.env` file), so the same image runs
unchanged in development, CI and production.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Bulk Certificate Generator API"
    environment: str = "development"
    log_level: str = "INFO"

    # SQLite is the zero-setup fallback for local runs; docker-compose points this
    # at PostgreSQL, e.g. postgresql+psycopg://user:pass@db:5432/certificates
    database_url: str = f"sqlite:///{(PROJECT_ROOT / 'certificates.db').as_posix()}"

    # Root folder for generated PDFs. Files are stored as <job_id>/<cert_id>.pdf.
    storage_dir: Path = PROJECT_ROOT / "storage" / "certificates"

    # Optional layout/theme file for the ReportLab renderer.
    certificate_layout_path: Path = PROJECT_ROOT / "static" / "templates" / "certificate_layout.json"

    # Upper bound on a single request. It keeps request bodies and in-process
    # background work bounded. Larger batches should be split by the client
    # (or this service moved to a Celery queue, see README).
    max_recipients_per_job: int = Field(default=1000, ge=1)

    # Creating tables on startup is convenient for the assignment and for local
    # runs. In production you would turn this off and run Alembic migrations.
    auto_create_tables: bool = True


@lru_cache
def get_settings() -> Settings:
    """Cached settings object, also used as a FastAPI dependency so tests can
    override it via `app.dependency_overrides`."""
    return Settings()
