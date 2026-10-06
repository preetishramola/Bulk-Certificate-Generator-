import io
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app.config import Settings
from app.main import create_app
from app.worker import InlineJobRunner


def inline_runner(process, _settings):
    return InlineJobRunner(process)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        storage_dir=tmp_path / "files",
        max_recipients_per_job=50,
        worker_threads=1,
    )


@pytest.fixture
def make_client(settings) -> Callable[..., TestClient]:
    """Build a client; by default jobs are processed inline so tests are deterministic."""
    clients: list[TestClient] = []

    def _make(**overrides) -> TestClient:
        overrides.setdefault("runner_factory", inline_runner)
        client = TestClient(create_app(settings, **overrides))
        client.__enter__()  # run lifespan (create tables, start runner)
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


def job_payload(recipients=None, **overrides) -> dict:
    payload = {
        "course_name": "Python for Data Engineering",
        "issuer_name": "Acme Academy",
        "issue_date": "2026-09-30",
        "recipients": recipients
        if recipients is not None
        else [
            {"name": "Ada Lovelace", "email": "ada@example.com", "grade": "A+"},
            {"name": "Alan Turing", "email": "alan@example.com"},
            {"name": "Grace Hopper", "email": "grace@example.com"},
        ],
    }
    payload.update(overrides)
    return payload


def pdf_text(content: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(content)).pages)
