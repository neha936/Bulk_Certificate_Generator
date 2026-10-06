"""Pydantic v2 request/response schemas.

Validation happens in two layers:

1. **Request validation (here):** structural problems such as a malformed
   email, an empty recipient list or a missing field are rejected with 422
   before anything is persisted. A batch with a typo in an address is almost
   always a client bug, so we fail fast and tell them which index is wrong
   (`loc: ["body", "recipients", 3, "email"]`).
2. **Per-recipient processing validation (bulk_processor / pdf_generator):**
   problems found only while rendering (e.g. characters the certificate
   font cannot draw, or a ReportLab error) are recorded on that one
   `Certificate` row as FAILED. The rest of the batch carries on.

Response models do not expose internal fields such as the on-disk `file_path`.
Clients get a `download_url` instead.
"""

import uuid
from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, computed_field

from app.models import CertificateStatus, JobStatus

# Strip whitespace first, then enforce length. This way "   " counts as empty.
NonEmptyStr200 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]

# Hard cap at schema level. config.max_recipients_per_job may lower it further.
MAX_RECIPIENTS_HARD_LIMIT = 10_000


# ---------------------------------------------------------------- requests ---


class RecipientIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: NonEmptyStr200
    email: EmailStr
    # Optional custom metadata (e.g. {"grade": "A", "cohort": "2026-Q3"}).
    # Values are scalars only, so the stored JSON stays flat and predictable.
    meta: dict[str, str | int | float | bool] = Field(default_factory=dict, max_length=20)


class JobCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "title": "Python Backend Bootcamp - Autumn 2026",
                    "course_name": "Advanced FastAPI & PostgreSQL",
                    "issue_date": "2026-10-06",
                    "recipients": [
                        {"name": "Ada Lovelace", "email": "ada@example.com", "meta": {"grade": "A"}},
                        {"name": "Alan Turing", "email": "alan@example.com"},
                    ],
                }
            ]
        },
    )

    title: NonEmptyStr200
    course_name: NonEmptyStr200
    issue_date: date
    recipients: list[RecipientIn] = Field(min_length=1, max_length=MAX_RECIPIENTS_HARD_LIMIT)


# --------------------------------------------------------------- responses ---


class CertificateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_id: uuid.UUID
    recipient_name: str
    recipient_email: str
    course_name: str
    issue_date: date
    meta: dict
    status: CertificateStatus
    error_message: str | None
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def download_url(self) -> str | None:
        """Present only once the PDF actually exists."""
        if self.status == CertificateStatus.SUCCESS:
            return f"/api/v1/certificates/{self.id}/download"
        return None


class JobCreatedResponse(BaseModel):
    job_id: uuid.UUID
    status: JobStatus
    total_recipients: int
    status_url: str
    message: str = "Job accepted. Poll status_url for progress."


class JobSummary(BaseModel):
    """Job-level counters and progress, without the certificate list."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    status: JobStatus
    total_recipients: int
    successful_count: int
    failed_count: int
    progress_percentage: float
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pending_count(self) -> int:
        return self.total_recipients - self.successful_count - self.failed_count


class JobStatusResponse(JobSummary):
    certificates: list[CertificateOut]


class CertificateListResponse(BaseModel):
    job_id: uuid.UUID
    total: int
    skip: int
    limit: int
    items: list[CertificateOut]
