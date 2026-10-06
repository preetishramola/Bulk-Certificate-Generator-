"""End-to-end API tests. Jobs are processed inline (see conftest) unless stated otherwise."""

import io
import time
import zipfile

from app.renderer import render_certificate
from app.worker import ThreadPoolJobRunner
from tests.conftest import job_payload, pdf_text

JOBS = "/api/v1/jobs"


def certificates(client, job_id, **params):
    response = client.get(f"{JOBS}/{job_id}/certificates", params=params)
    assert response.status_code == 200
    return response.json()


# ---------- Creating a job ----------


def test_create_job_returns_202_with_location_and_job_summary(client):
    response = client.post(JOBS, json=job_payload())

    assert response.status_code == 202
    body = response.json()
    assert response.headers["Location"] == f"{JOBS}/{body['id']}"
    assert body["course_name"] == "Python for Data Engineering"
    assert body["issue_date"] == "2026-09-30"
    assert body["progress"]["total"] == 3
    assert body["links"]["certificates"] == f"{JOBS}/{body['id']}/certificates"


def test_issue_date_defaults_to_today(client):
    from datetime import date

    response = client.post(JOBS, json=job_payload(issue_date=None))
    assert response.json()["issue_date"] == date.today().isoformat()


# ---------- Input validation ----------


def test_envelope_validation_errors_reject_the_whole_request(client):
    cases = [
        job_payload(course_name=""),
        job_payload(recipients=[]),
        job_payload(issue_date="not-a-date"),
        {k: v for k, v in job_payload().items() if k != "issuer_name"},
        job_payload(recipients="ada@example.com"),
    ]
    for payload in cases:
        assert client.post(JOBS, json=payload).status_code == 422, payload


def test_too_many_recipients_is_rejected(client):
    recipients = [{"name": f"Person {i}", "email": f"p{i}@example.com"} for i in range(51)]

    response = client.post(JOBS, json=job_payload(recipients=recipients))

    assert response.status_code == 422
    assert "at most 50 recipients" in response.json()["detail"]


def test_request_with_no_valid_recipients_is_rejected_with_per_recipient_errors(client):
    response = client.post(
        JOBS, json=job_payload(recipients=[{"name": "Bob", "email": "nope"}, {"email": "x@example.com"}])
    )

    assert response.status_code == 422
    invalid = response.json()["detail"]["invalid_recipients"]
    assert [r["index"] for r in invalid] == [0, 1]
    assert "email" in invalid[0]["errors"][0]
    assert client.get(JOBS + "/does-not-matter").status_code == 404  # and no job was created


def test_invalid_recipients_are_reported_while_valid_ones_are_generated(client):
    recipients = [
        {"name": "Ada Lovelace", "email": "ada@example.com"},
        {"name": "Bad Email", "email": "bad"},
        {"name": "Grace Hopper", "email": "grace@example.com"},
        {"name": "Ada Duplicate", "email": "ADA@example.com"},
    ]
    job = client.post(JOBS, json=job_payload(recipients=recipients)).json()

    assert job["status"] == "completed_with_errors"
    assert job["progress"] | {"percent_complete": None} == {
        "total": 4, "pending": 0, "generated": 2, "failed": 0, "invalid": 2, "processed": 4, "percent_complete": None
    }

    invalid = certificates(client, job["id"], status="invalid")["items"]
    assert [(c["position"], c["input"]["name"]) for c in invalid] == [(1, "Bad Email"), (3, "Ada Duplicate")]
    assert all(c["errors"] and c["download_url"] is None for c in invalid)


# ---------- Job status / progress ----------


def test_successful_job_reports_completed_status_and_full_progress(client):
    job_id = client.post(JOBS, json=job_payload()).json()["id"]

    job = client.get(f"{JOBS}/{job_id}").json()

    assert job["status"] == "completed"
    assert job["progress"] == {
        "total": 3, "pending": 0, "generated": 3, "failed": 0, "invalid": 0, "processed": 3, "percent_complete": 100.0
    }
    assert job["started_at"] and job["finished_at"] and job["error"] is None


def test_unknown_job_returns_404(client):
    assert client.get(f"{JOBS}/missing").status_code == 404
    assert client.get(f"{JOBS}/missing/certificates").status_code == 404


def test_job_processed_in_background_thread_moves_to_completed(make_client):
    client = make_client(runner_factory=lambda process, s: ThreadPoolJobRunner(process, max_workers=1))
    recipients = [{"name": f"Person {i}", "email": f"p{i}@example.com"} for i in range(20)]

    created = client.post(JOBS, json=job_payload(recipients=recipients))
    assert created.status_code == 202
    assert created.json()["status"] in {"pending", "processing", "completed"}

    deadline = time.monotonic() + 30
    while (job := client.get(f"{JOBS}/{created.json()['id']}").json())["status"] in {"pending", "processing"}:
        assert time.monotonic() < deadline, "job did not finish in time"
        time.sleep(0.05)

    assert job["status"] == "completed"
    assert job["progress"]["generated"] == 20


# ---------- Individual failures ----------


def failing_for(*names):
    def render(data):
        if data.recipient_name in names:
            raise RuntimeError(f"simulated failure for {data.recipient_name}")
        return render_certificate(data)

    return render


def test_one_failed_certificate_does_not_stop_the_others(make_client):
    client = make_client(renderer=failing_for("Alan Turing"))

    job = client.post(JOBS, json=job_payload()).json()

    assert job["status"] == "completed_with_errors"
    assert job["progress"]["generated"] == 2
    assert job["progress"]["failed"] == 1

    failed = certificates(client, job["id"], status="failed")["items"]
    assert len(failed) == 1
    assert failed[0]["recipient_name"] == "Alan Turing"
    assert failed[0]["errors"] == ["RuntimeError: simulated failure for Alan Turing"]
    assert client.get(f"/api/v1/certificates/{failed[0]['id']}/file").status_code == 409

    generated = certificates(client, job["id"], status="generated")["items"]
    assert {c["recipient_name"] for c in generated} == {"Ada Lovelace", "Grace Hopper"}


def test_job_fails_when_no_certificate_can_be_generated(make_client):
    client = make_client(renderer=failing_for("Ada Lovelace", "Alan Turing", "Grace Hopper"))

    job = client.post(JOBS, json=job_payload()).json()

    assert job["status"] == "failed"
    assert job["error"] == "No certificates could be generated"
    assert job["progress"]["failed"] == 3


# ---------- Retrieving certificates ----------


def test_list_certificates_is_ordered_paginated_and_filterable(client):
    recipients = [{"name": f"Person {i}", "email": f"p{i}@example.com"} for i in range(5)] + [
        {"name": "x", "email": "bad"}
    ]
    job_id = client.post(JOBS, json=job_payload(recipients=recipients)).json()["id"]

    page = certificates(client, job_id, limit=2, offset=2)
    assert page["total"] == 6
    assert [c["position"] for c in page["items"]] == [2, 3]

    both = certificates(client, job_id, status=["generated", "invalid"])
    assert both["total"] == 6
    assert certificates(client, job_id, status="invalid")["total"] == 1
    assert client.get(f"{JOBS}/{job_id}/certificates", params={"status": "bogus"}).status_code == 422


def test_download_generated_certificate_pdf(client):
    job_id = client.post(JOBS, json=job_payload()).json()["id"]
    cert = certificates(client, job_id)["items"][0]
    assert cert["status"] == "generated"

    meta = client.get(f"/api/v1/certificates/{cert['id']}")
    assert meta.status_code == 200 and meta.json()["recipient_email"] == "ada@example.com"

    response = client.get(cert["download_url"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert 'filename="certificate-ada-lovelace-' in response.headers["content-disposition"]
    text = pdf_text(response.content)
    assert "Ada Lovelace" in text and cert["id"] in text


def test_download_unknown_certificate_returns_404(client):
    assert client.get("/api/v1/certificates/missing").status_code == 404
    assert client.get("/api/v1/certificates/missing/file").status_code == 404


def test_download_job_archive_contains_every_generated_certificate(make_client):
    client = make_client(renderer=failing_for("Alan Turing"))
    job_id = client.post(JOBS, json=job_payload()).json()["id"]

    response = client.get(f"{JOBS}/{job_id}/archive")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = sorted(archive.namelist())
        assert len(names) == 2
        assert names[0].startswith("00000-certificate-ada-lovelace")
        assert names[1].startswith("00002-certificate-grace-hopper")
        assert "Grace Hopper" in pdf_text(archive.read(names[1]))
