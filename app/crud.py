"""Data-access layer.

Routers and services call these functions and never build queries themselves.
That keeps SQL in one place, which makes it easy to test and to optimise.

Transaction rule: these functions only *stage* changes (add / mutate / flush).
The caller decides when to `commit()`, so it controls the transaction scope.
For example, bulk_processor commits once per certificate.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import Certificate, CertificateJob, CertificateStatus, JobStatus
from app.schemas import JobCreate

# error_message is TEXT, but we cap it so a pathological stack trace
# can't bloat rows or API responses.
MAX_ERROR_MESSAGE_LENGTH = 1000


# ------------------------------------------------------------------- jobs ---


def create_job_with_certificates(db: Session, payload: JobCreate) -> CertificateJob:
    """Stage a job plus one PENDING certificate per recipient.

    Everything goes in one transaction, so a job never exists without its
    certificates (and the other way round).
    """
    job = CertificateJob(
        title=payload.title,
        status=JobStatus.PENDING,
        total_recipients=len(payload.recipients),
        successful_count=0,
        failed_count=0,
    )
    job.certificates = [
        Certificate(
            recipient_name=r.name,
            recipient_email=str(r.email),
            course_name=payload.course_name,
            issue_date=payload.issue_date,
            meta=r.meta,
            status=CertificateStatus.PENDING,
        )
        for r in payload.recipients
    ]
    db.add(job)
    db.flush()  # assigns PKs without committing
    return job


def get_job(db: Session, job_id: uuid.UUID) -> CertificateJob | None:
    return db.get(CertificateJob, job_id)


def get_job_with_certificates(db: Session, job_id: uuid.UUID) -> CertificateJob | None:
    # selectinload runs 2 queries in total (job + all certificates). Lazy loading
    # would run N+1 queries when the response serialises the certificate list.
    stmt = select(CertificateJob).where(CertificateJob.id == job_id).options(selectinload(CertificateJob.certificates))
    return db.scalars(stmt).first()


def mark_job_processing(db: Session, job: CertificateJob) -> None:
    job.status = JobStatus.PROCESSING


def mark_job_failed(db: Session, job: CertificateJob) -> None:
    job.status = JobStatus.FAILED


def finalize_job(db: Session, job: CertificateJob) -> CertificateJob:
    """Recompute counters from the source of truth and set the terminal status.

    The running counters should already be right. Recounting here is cheap
    insurance against drift (e.g. a job resumed after a crash).
    """
    counts = dict(
        db.execute(
            select(Certificate.status, func.count()).where(Certificate.job_id == job.id).group_by(Certificate.status)
        ).all()
    )
    job.successful_count = counts.get(CertificateStatus.SUCCESS, 0)
    job.failed_count = counts.get(CertificateStatus.FAILED, 0)

    if job.failed_count == 0:
        job.status = JobStatus.COMPLETED
    elif job.successful_count == 0:
        job.status = JobStatus.FAILED
    else:
        job.status = JobStatus.PARTIALLY_FAILED
    return job


# ----------------------------------------------------------- certificates ---


def get_certificate(db: Session, certificate_id: uuid.UUID) -> Certificate | None:
    return db.get(Certificate, certificate_id)


def list_certificates_for_job(
    db: Session,
    job_id: uuid.UUID,
    *,
    status: CertificateStatus | None = None,
    skip: int = 0,
    limit: int = 100,
) -> tuple[int, Sequence[Certificate]]:
    """Return (total_matching, page_of_certificates)."""
    base = select(Certificate).where(Certificate.job_id == job_id)
    if status is not None:
        base = base.where(Certificate.status == status)

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    # Sort by created_at, then id as a tie-breaker. Rows created in one flush can
    # share a timestamp, and pagination needs a stable order.
    items = db.scalars(base.order_by(Certificate.created_at, Certificate.id).offset(skip).limit(limit)).all()
    return total, items


def pending_certificate_ids(db: Session, job_id: uuid.UUID) -> list[uuid.UUID]:
    """IDs still waiting to be processed.

    Picking only PENDING rows makes processing idempotent and resumable: a
    re-run after a crash skips certificates that already finished.
    """
    stmt = (
        select(Certificate.id)
        .where(Certificate.job_id == job_id, Certificate.status == CertificateStatus.PENDING)
        .order_by(Certificate.created_at, Certificate.id)
    )
    return list(db.scalars(stmt).all())


def mark_certificate_success(db: Session, cert: Certificate, job: CertificateJob, relative_path: str) -> None:
    cert.status = CertificateStatus.SUCCESS
    cert.file_path = relative_path
    cert.error_message = None
    job.successful_count += 1


def mark_certificate_failed(db: Session, cert: Certificate, job: CertificateJob, error: str) -> None:
    cert.status = CertificateStatus.FAILED
    cert.file_path = None
    cert.error_message = error[:MAX_ERROR_MESSAGE_LENGTH]
    job.failed_count += 1
