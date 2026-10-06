import re
import tempfile
import zipfile
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import services
from app.models import Certificate, CertificateStatus, Job
from app.schemas import CertificateOut, CertificatePage, JobCreate, JobOut
from app.storage import CertificateStorage

router = APIRouter(prefix=services.API_PREFIX)


# ---------- Dependencies ----------


def get_db(request: Request) -> Iterator[Session]:
    db = request.app.state.session_factory()
    try:
        yield db
    finally:
        db.close()


def get_storage(request: Request) -> CertificateStorage:
    return request.app.state.storage


DB = Annotated[Session, Depends(get_db)]
Storage = Annotated[CertificateStorage, Depends(get_storage)]


def _get_job_or_404(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")
    return job


def _certificate_to_out(cert: Certificate) -> CertificateOut:
    return CertificateOut(
        id=cert.id,
        job_id=cert.job_id,
        position=cert.position,
        status=cert.status,
        recipient_name=cert.recipient_name,
        recipient_email=cert.recipient_email,
        grade=cert.grade,
        errors=cert.errors,
        input=cert.raw_input,
        generated_at=cert.generated_at,
        download_url=(
            f"{services.API_PREFIX}/certificates/{cert.id}/file"
            if cert.status == CertificateStatus.GENERATED
            else None
        ),
    )


def _download_name(cert: Certificate) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", cert.recipient_name or "").strip("-").lower() or "recipient"
    return f"certificate-{slug}-{cert.id[:8]}.pdf"


# ---------- Jobs ----------


@router.post(
    "/jobs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobOut,
    responses={422: {"description": "Invalid envelope, too many recipients, or no valid recipients"}},
)
def create_job(payload: JobCreate, request: Request, response: Response, db: DB) -> JobOut:
    """Submit a bulk generation request. Returns immediately; poll the job for progress."""
    limit = request.app.state.settings.max_recipients_per_job
    if len(payload.recipients) > limit:
        raise HTTPException(
            422,
            detail=f"A job may contain at most {limit} recipients (got {len(payload.recipients)}); "
            "split the list into several jobs.",
        )

    try:
        job = services.create_job(db, payload)
    except services.NoValidRecipientsError as exc:
        raise HTTPException(
            422,
            detail={"message": str(exc), "invalid_recipients": [r.model_dump() for r in exc.invalid]},
        ) from exc

    request.app.state.runner.submit(job.id)

    db.refresh(job)  # the runner may already have progressed the job
    response.headers["Location"] = f"{services.API_PREFIX}/jobs/{job.id}"
    return services.job_to_out(db, job)


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, db: DB) -> JobOut:
    """Job status and progress counts."""
    return services.job_to_out(db, _get_job_or_404(db, job_id))


@router.get("/jobs/{job_id}/certificates", response_model=CertificatePage)
def list_job_certificates(
    job_id: str,
    db: DB,
    status_filter: Annotated[list[CertificateStatus] | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CertificatePage:
    """Per-recipient results, in submission order. Filter with e.g. `?status=failed&status=invalid`."""
    _get_job_or_404(db, job_id)

    conditions = [Certificate.job_id == job_id]
    if status_filter:
        conditions.append(Certificate.status.in_(status_filter))

    total = db.scalar(select(func.count()).select_from(Certificate).where(*conditions))
    rows = db.scalars(
        select(Certificate).where(*conditions).order_by(Certificate.position).limit(limit).offset(offset)
    ).all()
    return CertificatePage(items=[_certificate_to_out(c) for c in rows], total=total, limit=limit, offset=offset)


@router.get(
    "/jobs/{job_id}/archive",
    response_class=StreamingResponse,
    responses={200: {"content": {"application/zip": {}}}, 409: {"description": "Job not finished / nothing generated"}},
)
def download_job_archive(job_id: str, db: DB, storage: Storage) -> StreamingResponse:
    """All generated certificates of a finished job as a single ZIP file."""
    job = _get_job_or_404(db, job_id)
    if not job.status.is_terminal:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"Job is still {job.status.value}")

    certs = db.scalars(
        select(Certificate)
        .where(Certificate.job_id == job_id, Certificate.status == CertificateStatus.GENERATED)
        .order_by(Certificate.position)
    ).all()
    if not certs:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Job has no generated certificates")

    # Spool to disk past 10 MB so large jobs don't sit entirely in memory.
    spool = tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024)
    with zipfile.ZipFile(spool, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for cert in certs:
            archive.write(storage.resolve(cert.file_path), arcname=f"{cert.position:05d}-{_download_name(cert)}")
    spool.seek(0)

    def stream() -> Iterator[bytes]:
        with spool:
            while chunk := spool.read(64 * 1024):
                yield chunk

    return StreamingResponse(
        stream(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="certificates-{job_id}.zip"'},
    )


# ---------- Certificates ----------


@router.get("/certificates/{certificate_id}", response_model=CertificateOut)
def get_certificate(certificate_id: str, db: DB) -> CertificateOut:
    cert = db.get(Certificate, certificate_id)
    if cert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Certificate {certificate_id} not found")
    return _certificate_to_out(cert)


@router.get(
    "/certificates/{certificate_id}/file",
    response_class=FileResponse,
    responses={200: {"content": {"application/pdf": {}}}, 409: {"description": "Certificate not generated"}},
)
def download_certificate(certificate_id: str, db: DB, storage: Storage) -> FileResponse:
    cert = db.get(Certificate, certificate_id)
    if cert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Certificate {certificate_id} not found")
    if cert.status != CertificateStatus.GENERATED:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"Certificate is not available (status: {cert.status.value})"
        )
    path = storage.resolve(cert.file_path)
    if not path.exists():
        raise HTTPException(status.HTTP_410_GONE, detail="Certificate file is missing from storage")
    return FileResponse(path, media_type="application/pdf", filename=_download_name(cert))
