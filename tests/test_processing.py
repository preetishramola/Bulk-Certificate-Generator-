"""Service-level tests for the processing pipeline (no HTTP)."""

from datetime import date

from fastapi.testclient import TestClient

from app.database import Base, make_engine, make_session_factory
from app.main import create_app
from app.models import Certificate, CertificateStatus, Job, JobStatus
from app.renderer import render_certificate
from app.schemas import JobCreate
from app.services import create_job, process_job, status_counts
from app.storage import CertificateStorage
from tests.conftest import inline_runner


def setup_db(settings):
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def new_job(session_factory, names) -> str:
    payload = JobCreate(
        course_name="Course",
        issuer_name="Issuer",
        issue_date=date(2026, 1, 1),
        recipients=[{"name": n, "email": f"{n.lower()}@example.com"} for n in names],
    )
    with session_factory() as db:
        return create_job(db, payload).id


def test_progress_is_committed_after_each_certificate(settings):
    session_factory = setup_db(settings)
    storage = CertificateStorage(settings.storage_dir)
    job_id = new_job(session_factory, ["Ann", "Ben", "Cat"])
    observed = []

    def observing_render(data):
        # Read progress from a *separate* session, as the status endpoint would.
        with session_factory() as db:
            observed.append(status_counts(db, job_id)[CertificateStatus.GENERATED])
            assert db.get(Job, job_id).status == JobStatus.PROCESSING
        return render_certificate(data)

    process_job(session_factory, storage, job_id, render=observing_render)

    assert observed == [0, 1, 2]
    with session_factory() as db:
        assert db.get(Job, job_id).status == JobStatus.COMPLETED


def test_reprocessing_only_touches_pending_certificates(settings):
    session_factory = setup_db(settings)
    storage = CertificateStorage(settings.storage_dir)
    job_id = new_job(session_factory, ["Ann", "Ben"])

    # Simulate a crash after the first certificate was generated.
    with session_factory() as db:
        first = db.query(Certificate).filter_by(job_id=job_id, position=0).one()
        first.status = CertificateStatus.GENERATED
        first.file_path = "already/there.pdf"
        db.get(Job, job_id).status = JobStatus.PROCESSING
        db.commit()

    rendered = []
    process_job(session_factory, storage, job_id, render=lambda d: rendered.append(d.recipient_name) or b"%PDF")

    assert rendered == ["Ben"]
    with session_factory() as db:
        assert db.get(Job, job_id).status == JobStatus.COMPLETED


def test_storage_failure_is_recorded_per_certificate(settings, monkeypatch):
    session_factory = setup_db(settings)
    storage = CertificateStorage(settings.storage_dir)
    job_id = new_job(session_factory, ["Ann", "Ben"])
    real_save = storage.save

    def flaky_save(job, cert_id, content):
        if content == b"ben":
            raise OSError("disk full")
        return real_save(job, cert_id, content)

    monkeypatch.setattr(storage, "save", flaky_save)
    process_job(session_factory, storage, job_id, render=lambda d: d.recipient_name.lower().encode())

    with session_factory() as db:
        certs = {c.recipient_name: c for c in db.query(Certificate).filter_by(job_id=job_id)}
        assert certs["Ann"].status == CertificateStatus.GENERATED
        assert certs["Ben"].status == CertificateStatus.FAILED
        assert certs["Ben"].errors == ["OSError: disk full"]
        assert db.get(Job, job_id).status == JobStatus.COMPLETED_WITH_ERRORS


def test_unfinished_jobs_are_resumed_on_startup(settings):
    session_factory = setup_db(settings)
    job_id = new_job(session_factory, ["Ann", "Ben"])  # created but never processed

    with TestClient(create_app(settings, runner_factory=inline_runner)) as client:
        job = client.get(f"/api/v1/jobs/{job_id}").json()

    assert job["status"] == "completed"
    assert job["progress"]["generated"] == 2
