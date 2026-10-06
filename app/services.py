import logging
from collections.abc import Callable
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Certificate, CertificateStatus, Job, JobStatus, utcnow
from app.renderer import CertificateData, render_certificate
from app.schemas import InvalidRecipient, JobCreate, JobOut, JobProgress
from app.storage import CertificateStorage
from app.validation import validate_recipients

logger = logging.getLogger(__name__)

Renderer = Callable[[CertificateData], bytes]

API_PREFIX = "/api/v1"


class NoValidRecipientsError(Exception):
    def __init__(self, invalid: list[InvalidRecipient]):
        super().__init__("None of the submitted recipients are valid")
        self.invalid = invalid


# ---------- Job creation ----------


def create_job(db: Session, payload: JobCreate) -> Job:
    """Validate recipients and persist the job with one certificate row per recipient.

    Invalid recipients are stored too (status=invalid) so the job report covers
    every row the client sent. If *no* recipient is valid there is nothing to
    do, so the request is rejected instead of creating an empty job.
    """
    valid, rejected = validate_recipients(payload.recipients)
    if not valid:
        raise NoValidRecipientsError([InvalidRecipient(index=r.index, errors=r.errors) for r in rejected])

    job = Job(
        course_name=payload.course_name,
        issuer_name=payload.issuer_name,
        issue_date=payload.issue_date or date.today(),
        total_recipients=len(payload.recipients),
        status=JobStatus.PENDING,
    )
    job.certificates = [
        Certificate(
            position=v.index,
            status=CertificateStatus.PENDING,
            recipient_name=v.recipient.name,
            recipient_email=v.recipient.email,
            grade=v.recipient.grade,
        )
        for v in valid
    ] + [
        Certificate(position=r.index, status=CertificateStatus.INVALID, raw_input=r.raw, errors=r.errors)
        for r in rejected
    ]
    db.add(job)
    db.commit()
    return job


# ---------- Processing ----------


def process_job(
    session_factory: sessionmaker[Session],
    storage: CertificateStorage,
    job_id: str,
    render: Renderer = render_certificate,
) -> None:
    """Generate every pending certificate in a job.

    - Each certificate is generated and committed on its own, so progress is
      visible while the job runs and one failure never affects the others.
    - Only PENDING rows are picked up, which makes this safe to re-run for a
      job that was interrupted (e.g. by a restart): finished rows are skipped.
    """
    with session_factory() as db:
        job = db.get(Job, job_id)
        if job is None:
            logger.warning("Job %s not found; skipping", job_id)
            return
        if job.status.is_terminal:
            return

        try:
            job.status = JobStatus.PROCESSING
            job.started_at = job.started_at or utcnow()
            db.commit()

            pending_ids = db.scalars(
                select(Certificate.id)
                .where(Certificate.job_id == job_id, Certificate.status == CertificateStatus.PENDING)
                .order_by(Certificate.position)
            ).all()

            for certificate_id in pending_ids:
                _generate_one(db, storage, job, certificate_id, render)

            _finalize(db, job)
        except Exception as exc:  # infrastructure failure (DB down, disk full, ...)
            logger.exception("Job %s aborted", job_id)
            db.rollback()
            job = db.get(Job, job_id)
            if job is not None:
                job.status = JobStatus.FAILED
                job.error = f"Job aborted: {type(exc).__name__}: {exc}"
                job.finished_at = utcnow()
                db.commit()


def _generate_one(
    db: Session, storage: CertificateStorage, job: Job, certificate_id: str, render: Renderer
) -> None:
    cert = db.get(Certificate, certificate_id)
    try:
        pdf = render(
            CertificateData(
                certificate_id=cert.id,
                recipient_name=cert.recipient_name,
                course_name=job.course_name,
                issuer_name=job.issuer_name,
                issue_date=job.issue_date,
                grade=cert.grade,
            )
        )
        cert.file_path = storage.save(job.id, cert.id, pdf)
        cert.status = CertificateStatus.GENERATED
        cert.generated_at = utcnow()
        cert.errors = None
    except Exception as exc:
        # Isolate the failure to this recipient and carry on with the rest of the job.
        logger.warning("Certificate %s (job %s) failed: %s", cert.id, job.id, exc)
        cert.status = CertificateStatus.FAILED
        cert.errors = [f"{type(exc).__name__}: {exc}"[:1000]]
    db.commit()


def _finalize(db: Session, job: Job) -> None:
    counts = status_counts(db, job.id)
    generated = counts[CertificateStatus.GENERATED]
    unsuccessful = counts[CertificateStatus.FAILED] + counts[CertificateStatus.INVALID]

    if generated == 0:
        job.status = JobStatus.FAILED
        job.error = "No certificates could be generated"
    elif unsuccessful:
        job.status = JobStatus.COMPLETED_WITH_ERRORS
    else:
        job.status = JobStatus.COMPLETED
    job.finished_at = utcnow()
    db.commit()


def unfinished_job_ids(session_factory: sessionmaker[Session]) -> list[str]:
    with session_factory() as db:
        return list(
            db.scalars(
                select(Job.id)
                .where(Job.status.in_([JobStatus.PENDING, JobStatus.PROCESSING]))
                .order_by(Job.created_at)
            )
        )


# ---------- Reporting ----------


def status_counts(db: Session, job_id: str) -> dict[CertificateStatus, int]:
    """Count certificates per status. Derived from the rows rather than stored
    counters, so the numbers can never drift from the actual data."""
    rows = db.execute(
        select(Certificate.status, func.count()).where(Certificate.job_id == job_id).group_by(Certificate.status)
    ).all()
    counts = {status: 0 for status in CertificateStatus}
    counts.update({status: n for status, n in rows})
    return counts


def job_to_out(db: Session, job: Job) -> JobOut:
    counts = status_counts(db, job.id)
    total = job.total_recipients
    processed = total - counts[CertificateStatus.PENDING]
    base = f"{API_PREFIX}/jobs/{job.id}"
    return JobOut(
        id=job.id,
        status=job.status,
        course_name=job.course_name,
        issuer_name=job.issuer_name,
        issue_date=job.issue_date,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        error=job.error,
        progress=JobProgress(
            total=total,
            pending=counts[CertificateStatus.PENDING],
            generated=counts[CertificateStatus.GENERATED],
            failed=counts[CertificateStatus.FAILED],
            invalid=counts[CertificateStatus.INVALID],
            processed=processed,
            percent_complete=round(100 * processed / total, 2) if total else 100.0,
        ),
        links={
            "self": base,
            "certificates": f"{base}/certificates",
            "unsuccessful": f"{base}/certificates?status=failed&status=invalid",
            "archive": f"{base}/archive",
        },
    )
