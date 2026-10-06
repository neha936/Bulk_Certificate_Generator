"""Job endpoints: submit a bulk request, poll its progress, list its certificates.

Endpoints are plain `def` (not `async def`) on purpose. They use the sync
SQLAlchemy session, and FastAPI runs `def` endpoints in a thread pool, so
blocking DB I/O never stalls the event loop.
"""

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session, sessionmaker

from app import crud
from app.config import Settings, get_settings
from app.database import get_db, get_session_factory
from app.models import CertificateStatus
from app.schemas import (
    CertificateListResponse,
    CertificateOut,
    JobCreate,
    JobCreatedResponse,
    JobStatusResponse,
)
from app.services.bulk_processor import process_job

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


@router.post(
    "/",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobCreatedResponse,
    summary="Submit a bulk certificate generation job",
    responses={422: {"description": "Invalid payload (e.g. empty recipients, malformed email)"}},
)
@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=JobCreatedResponse, include_in_schema=False)
def create_job(
    payload: JobCreate,
    background_tasks: BackgroundTasks,
    response: Response,
    db: Session = Depends(get_db),
    session_factory: sessionmaker[Session] = Depends(get_session_factory),
    settings: Settings = Depends(get_settings),
) -> JobCreatedResponse:
    """Persist the job and its PENDING certificates, then return **202 Accepted**
    straight away. PDF generation runs in the background after the response
    is sent."""
    if len(payload.recipients) > settings.max_recipients_per_job:
        raise HTTPException(
            status_code=413,  # Content Too Large (the constant's name differs across Starlette versions)
            detail=f"A job may contain at most {settings.max_recipients_per_job} recipients.",
        )

    job = crud.create_job_with_certificates(db, payload)
    # Commit BEFORE scheduling the task. The background worker uses a different
    # session and must see the committed rows.
    db.commit()

    background_tasks.add_task(
        process_job,
        job.id,
        session_factory,
        settings.storage_dir,
        settings.certificate_layout_path,
    )

    status_url = f"/api/v1/jobs/{job.id}"
    response.headers["Location"] = status_url
    return JobCreatedResponse(
        job_id=job.id,
        status=job.status,
        total_recipients=job.total_recipients,
        status_url=status_url,
    )


@router.get(
    "/{job_id}",
    response_model=JobStatusResponse,
    summary="Get job progress and per-certificate statuses",
    responses={404: {"description": "Job not found"}},
)
def get_job_status(job_id: uuid.UUID, db: Session = Depends(get_db)) -> JobStatusResponse:
    job = crud.get_job_with_certificates(db, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return JobStatusResponse.model_validate(job)


@router.get(
    "/{job_id}/certificates",
    response_model=CertificateListResponse,
    summary="List certificates of a job (paginated, filterable by status)",
    responses={404: {"description": "Job not found"}},
)
def list_job_certificates(
    job_id: uuid.UUID,
    status_filter: CertificateStatus | None = Query(default=None, alias="status"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> CertificateListResponse:
    if crud.get_job(db, job_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    total, items = crud.list_certificates_for_job(db, job_id, status=status_filter, skip=skip, limit=limit)
    return CertificateListResponse(
        job_id=job_id,
        total=total,
        skip=skip,
        limit=limit,
        items=[CertificateOut.model_validate(c) for c in items],
    )
