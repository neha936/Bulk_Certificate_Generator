"""ORM models.

Schema design:
- `certificate_jobs` (1) to (N) `certificates`. The job is the unit of
  submission and tracking. The certificate is the unit of work and of failure.
- Each certificate has its own `status` and `error_message`, so a failure is
  recorded on that row and does not fail the whole job.
- The job's `successful_count` and `failed_count` are denormalised counters.
  They are updated in the same transaction as each certificate row, so polling
  progress is an O(1) read on the job row and needs no COUNT(*) over children.
- Certificates keep their own copy of `course_name` and `issue_date`. An issued
  certificate is an immutable record of what was printed on the PDF.
- UUID primary keys are not guessable, which matters because certificate IDs
  show up in public download URLs and on the PDF itself.
- Enums are stored as VARCHAR + CHECK (native_enum=False). This works the same
  on PostgreSQL and SQLite, and adding a state later needs no ALTER TYPE.
"""

import enum
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import JSON, Date, DateTime, Enum, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class JobStatus(enum.StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PARTIALLY_FAILED = "PARTIALLY_FAILED"


class CertificateStatus(enum.StrEnum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class CertificateJob(Base):
    __tablename__ = "certificate_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False, length=20), nullable=False, default=JobStatus.PENDING, index=True
    )
    total_recipients: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    successful_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    certificates: Mapped[list["Certificate"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="Certificate.created_at",
    )

    @property
    def processed_count(self) -> int:
        return self.successful_count + self.failed_count

    @property
    def progress_percentage(self) -> float:
        if self.total_recipients == 0:
            return 100.0
        return round(self.processed_count / self.total_recipients * 100, 2)

    def __repr__(self) -> str:
        return f"<CertificateJob id={self.id} status={self.status} {self.processed_count}/{self.total_recipients}>"


class Certificate(Base):
    __tablename__ = "certificates"
    # Composite index for the main query: "certificates of job X (with status Y)".
    __table_args__ = (Index("ix_certificates_job_id_status", "job_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("certificate_jobs.id", ondelete="CASCADE"), nullable=False
    )
    recipient_name: Mapped[str] = mapped_column(String(200), nullable=False)
    recipient_email: Mapped[str] = mapped_column(String(320), nullable=False)  # 320 = RFC 5321 max length
    course_name: Mapped[str] = mapped_column(String(200), nullable=False)
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Free-form per-recipient metadata (e.g. grade, cohort) from the request.
    meta: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[CertificateStatus] = mapped_column(
        Enum(CertificateStatus, native_enum=False, length=20), nullable=False, default=CertificateStatus.PENDING
    )
    # Relative to settings.storage_dir (e.g. "<job_id>/<cert_id>.pdf"). A relative
    # path keeps the DB valid if the storage volume is mounted somewhere else.
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    job: Mapped[CertificateJob] = relationship(back_populates="certificates")

    def __repr__(self) -> str:
        return f"<Certificate id={self.id} job_id={self.job_id} status={self.status}>"
