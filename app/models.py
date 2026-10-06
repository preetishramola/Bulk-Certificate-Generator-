import enum
import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import JSON, Date, DateTime, Enum, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"  # every recipient got a certificate
    COMPLETED_WITH_ERRORS = "completed_with_errors"  # some generated, some failed/invalid
    FAILED = "failed"  # nothing could be generated

    @property
    def is_terminal(self) -> bool:
        return self in (JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_ERRORS, JobStatus.FAILED)


class CertificateStatus(str, enum.Enum):
    PENDING = "pending"  # valid, waiting to be generated
    GENERATED = "generated"
    FAILED = "failed"  # valid input, but generation raised an error
    INVALID = "invalid"  # rejected by validation, never attempted


def _enum_column(enum_cls: type[enum.Enum]) -> Enum:
    # Store the lowercase values as plain strings (portable across databases).
    return Enum(enum_cls, native_enum=False, length=32, values_callable=lambda e: [m.value for m in e])


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    status: Mapped[JobStatus] = mapped_column(_enum_column(JobStatus), default=JobStatus.PENDING, index=True)

    # Certificate information shared by every recipient in the job.
    course_name: Mapped[str] = mapped_column(String(150))
    issuer_name: Mapped[str] = mapped_column(String(150))
    issue_date: Mapped[date] = mapped_column(Date)

    total_recipients: Mapped[int] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text, default=None)  # job-level failure reason

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    certificates: Mapped[list["Certificate"]] = relationship(
        back_populates="job", order_by="Certificate.position", cascade="all, delete-orphan"
    )


class Certificate(Base):
    """One row per recipient in the request, including invalid ones."""

    __tablename__ = "certificates"
    __table_args__ = (
        UniqueConstraint("job_id", "position"),
        Index("ix_certificates_job_status", "job_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)  # index in the submitted recipients list
    status: Mapped[CertificateStatus] = mapped_column(_enum_column(CertificateStatus))

    recipient_name: Mapped[str | None] = mapped_column(String(120), default=None)
    recipient_email: Mapped[str | None] = mapped_column(String(320), default=None)
    grade: Mapped[str | None] = mapped_column(String(50), default=None)

    # For INVALID rows: the raw input as submitted, so the client can see what was rejected.
    raw_input: Mapped[Any | None] = mapped_column(JSON, default=None)
    # Validation messages (INVALID) or the generation error (FAILED).
    errors: Mapped[list[str] | None] = mapped_column(JSON, default=None)

    file_path: Mapped[str | None] = mapped_column(String(500), default=None)  # relative to storage root
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    job: Mapped[Job] = relationship(back_populates="certificates")
