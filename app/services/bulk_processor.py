"""Background orchestration of a bulk certificate job.

Fault-tolerance strategy:

* **Isolation per certificate.** Each recipient runs inside its own
  try/except and its own DB commit. An exception while rendering recipient #7
  is written to certificate #7 (`status=FAILED`, `error_message=...`), and
  the loop moves on to #8. One bad record never aborts the batch.
* **Live progress.** Committing after every certificate means
  `GET /jobs/{id}` shows progress while the job runs. Each commit also
  updates the job counters in the same transaction, so the counters and the
  certificate rows always agree.
* **Idempotent / resumable.** Only PENDING certificates are picked up, so if
  the process dies mid-job, calling `process_job` again finishes the rest
  without duplicating work.
* **Infrastructure failures are different.** If the *database itself* fails (or
  anything else breaks outside the per-certificate boundary),
  we can't record per-row results reliably. In that case we make a best-effort
  attempt to mark the job FAILED, log the error, and stop. Remaining rows stay
  PENDING, so the job can be resumed.

The entry point takes plain arguments (job_id, session factory, storage dir)
and opens its own session. It works as a FastAPI BackgroundTask and could
become a Celery task with just a decorator.
"""

import logging
import time
import uuid
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app import crud
from app.models import Certificate, CertificateJob, JobStatus
from app.services.pdf_generator import CertificateRenderError, generate_certificate_pdf

logger = logging.getLogger(__name__)


def certificate_relative_path(job_id: uuid.UUID, certificate_id: uuid.UUID) -> str:
    """Storage layout: <job_id>/<cert_id>.pdf (relative to the storage root)."""
    return f"{job_id}/{certificate_id}.pdf"


def process_job(
    job_id: uuid.UUID,
    session_factory: sessionmaker[Session],
    storage_dir: Path,
    theme_path: Path | None = None,
) -> None:
    """Generate every pending certificate of a job. Never raises."""
    started = time.perf_counter()
    with session_factory() as db:
        job = crud.get_job(db, job_id)
        if job is None:
            logger.error("process_job: job %s not found", job_id)
            return
        if job.status not in (JobStatus.PENDING, JobStatus.PROCESSING):
            # Already finished. Re-running a finished job is a no-op.
            logger.info("process_job: job %s already in terminal state %s", job_id, job.status)
            return

        try:
            crud.mark_job_processing(db, job)
            db.commit()

            for certificate_id in crud.pending_certificate_ids(db, job_id):
                _process_one(db, job, certificate_id, storage_dir, theme_path)

            crud.finalize_job(db, job)
            db.commit()
        except Exception:  # noqa: BLE001  (a background task must not die silently and leave the job in PROCESSING)
            logger.exception("process_job: infrastructure failure while processing job %s", job_id)
            db.rollback()
            _try_mark_job_failed(db, job_id)
            return

    logger.info(
        "Job %s finished: status=%s success=%d failed=%d in %.2fs",
        job_id,
        job.status,
        job.successful_count,
        job.failed_count,
        time.perf_counter() - started,
    )


def _process_one(
    db: Session,
    job: CertificateJob,
    certificate_id: uuid.UUID,
    storage_dir: Path,
    theme_path: Path | None,
) -> None:
    """Render one certificate and record the outcome. The error boundary for one recipient."""
    cert = db.get(Certificate, certificate_id)
    if cert is None:  # e.g. deleted while the job was running
        return

    relative_path = certificate_relative_path(job.id, cert.id)
    try:
        generate_certificate_pdf(
            output_path=storage_dir / relative_path,
            recipient_name=cert.recipient_name,
            course_name=cert.course_name,
            issue_date=cert.issue_date,
            certificate_id=cert.id,
            program_title=job.title,
            theme_path=theme_path,
        )
    except CertificateRenderError as exc:
        # Expected, data-related failure: the message is safe to show the client.
        logger.warning("Certificate %s (job %s) rejected: %s", cert.id, job.id, exc)
        crud.mark_certificate_failed(db, cert, job, str(exc))
    except Exception as exc:  # noqa: BLE001  (deliberately broad: this is the per-record boundary)
        # Unexpected failure (I/O, ReportLab bug, ...). Log the full traceback
        # for operators. Store a concise message for the client.
        logger.exception("Certificate %s (job %s) failed during generation", cert.id, job.id)
        crud.mark_certificate_failed(db, cert, job, f"PDF generation failed: {type(exc).__name__}: {exc}")
    else:
        crud.mark_certificate_success(db, cert, job, relative_path)

    # One commit per certificate gives durable, observable progress.
    db.commit()


def _try_mark_job_failed(db: Session, job_id: uuid.UUID) -> None:
    try:
        job = crud.get_job(db, job_id)
        if job is not None:
            crud.mark_job_failed(db, job)
            db.commit()
    except SQLAlchemyError:
        logger.exception("Could not mark job %s as FAILED", job_id)
        db.rollback()
