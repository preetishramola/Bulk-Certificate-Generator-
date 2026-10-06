# Bulk Certificate Generator

A FastAPI service that takes a list of recipients in one request, generates a PDF certificate for each one from a single predefined template, tracks progress, and serves the generated files.

![Sample certificate](docs/sample-certificate.png)

**Stack:** Python 3.11+ · FastAPI · SQLAlchemy 2 · SQLite by default (any SQLAlchemy-supported relational DB through `DATABASE_URL`) · ReportLab (PDF) · pytest

---

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt     # use requirements.txt for runtime-only installs
```

## Running the application

```bash
uvicorn app.main:create_app --factory --reload
```

- API: http://localhost:8000
- Interactive docs (Swagger): http://localhost:8000/docs

Tables are created automatically on startup.

Configuration uses environment variables, all optional:

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./certificates.db` | SQLAlchemy URL, e.g. `postgresql+psycopg://user:pw@localhost/certs` (also install the driver) |
| `STORAGE_DIR` | `./generated` | Where PDFs are written (`<dir>/<job_id>/<certificate_id>.pdf`) |
| `MAX_RECIPIENTS_PER_JOB` | `5000` | Largest recipient list accepted in one request |
| `WORKER_THREADS` | `2` | Number of jobs processed at the same time |

## Running tests

```bash
pytest
```

There are 27 tests, grouped as follows:

| File | Covers |
|---|---|
| `tests/test_jobs_api.py` | Creating jobs, envelope and recipient validation, status/progress, a single failure among successes, a job where every certificate fails, background-thread processing, listing/filtering/pagination, PDF download, ZIP archive |
| `tests/test_validation.py` | Per-recipient rules: bad email, missing or extra fields, non-object entries, duplicate emails, characters the template can't render |
| `tests/test_renderer.py` | Generated PDF content (name, course, date, grade, ID), long names, rejection of characters the fonts can't draw |
| `tests/test_processing.py` | Progress committed after each certificate, resuming an interrupted job, storage failure isolated to one certificate, restart recovery |

Most API tests use an **inline runner**, so a job has finished by the time `POST` returns and the assertions are deterministic. One test uses the real thread pool and polls until the job finishes.

---

## API usage

All endpoints are under `/api/v1`.

### 1. Submit a generation request

```bash
curl -i -X POST http://localhost:8000/api/v1/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "course_name": "Advanced Backend Engineering",
    "issuer_name": "Acme Academy",
    "issue_date": "2026-09-30",
    "recipients": [
      {"name": "Ada Lovelace", "email": "ada@example.com", "grade": "Distinction"},
      {"name": "Alan Turing",  "email": "alan@example.com"},
      {"name": "Bad Row",      "email": "not-an-email"}
    ]
  }'
```

| Field | Rules |
|---|---|
| `course_name`, `issuer_name` | required, 1–150 chars |
| `issue_date` | optional ISO date, defaults to today |
| `recipients` | required, 1..`MAX_RECIPIENTS_PER_JOB` items |
| `recipients[].name` | required, 1–120 chars, must contain a letter |
| `recipients[].email` | required, valid email, unique within the job (case-insensitive) |
| `recipients[].grade` | optional, ≤ 50 chars |

Unknown fields are rejected. Text is trimmed. Names must use characters the template fonts can draw (Latin-1 / Windows-1252, which includes accents such as `José Müller`).

**Response: `202 Accepted`** with a `Location` header pointing to the job:

```json
{
  "id": "5e7944e3-...",
  "status": "pending",
  "progress": {"total": 3, "pending": 2, "generated": 0, "failed": 0, "invalid": 1, "processed": 1, "percent_complete": 33.33},
  "links": {
    "self": "/api/v1/jobs/5e7944e3-...",
    "certificates": "/api/v1/jobs/5e7944e3-.../certificates",
    "unsuccessful": "/api/v1/jobs/5e7944e3-.../certificates?status=failed&status=invalid",
    "archive": "/api/v1/jobs/5e7944e3-.../archive"
  },
  "...": "..."
}
```

**`422`** is returned when:
- the envelope is invalid (e.g. missing `course_name`, empty `recipients`, bad date)
- the list exceeds `MAX_RECIPIENTS_PER_JOB`
- **no** recipient is valid; the body then lists each recipient's errors as `detail.invalid_recipients[{index, errors}]`

### 2. Check progress

```bash
curl http://localhost:8000/api/v1/jobs/<job_id>
```

| Job `status` | Meaning |
|---|---|
| `pending` | Accepted and queued |
| `processing` | Generation in progress; `progress` updates after every certificate |
| `completed` | Every recipient has a certificate |
| `completed_with_errors` | Some certificates were generated; others failed or were invalid |
| `failed` | Nothing could be generated (see `error`) |

### 3. See the result for each recipient

```bash
curl 'http://localhost:8000/api/v1/jobs/<job_id>/certificates'                          # all, in submission order
curl 'http://localhost:8000/api/v1/jobs/<job_id>/certificates?status=failed&status=invalid'
curl 'http://localhost:8000/api/v1/jobs/<job_id>/certificates?limit=100&offset=200'     # paginated (max 500)
```

Each item has:
- `position` (index in your request)
- `status`: `pending` | `generated` | `failed` | `invalid`
- the recipient fields
- `errors`
- `input` (the raw submitted data, for invalid rows)
- `download_url` (only when generated)

### 4. Retrieve certificates

```bash
# One certificate (PDF)
curl -OJ http://localhost:8000/api/v1/certificates/<certificate_id>/file

# Certificate metadata
curl http://localhost:8000/api/v1/certificates/<certificate_id>

# Every generated certificate of a finished job as a ZIP
curl -OJ http://localhost:8000/api/v1/jobs/<job_id>/archive
```

Error responses:
- `/file` returns `409` when the certificate wasn't generated (failed, invalid or still pending)
- `/archive` returns `409` while the job is still running

---

## Design decisions

### Background processing (not synchronous)

`POST /jobs` validates the input, stores the job and one row per recipient in a single transaction, hands the job ID to a worker, and returns `202` straight away. Generation runs in a bounded `ThreadPoolExecutor` inside the API process (`app/worker.py`).

- **Why not synchronous?** One request can hold thousands of recipients. Generating them inside the request would hold the HTTP connection open for a long time, hit client and proxy timeouts, and give no progress information. A local run of 5,000 recipients took about 8 s to generate, while `POST` returned in about 1 s (mostly validation and the bulk insert).
- **Why an in-process pool instead of Celery or RQ?** It needs no broker or extra worker processes and is simple to run and test, which suits this scope. The pool is bounded (`WORKER_THREADS`), so a burst of large jobs waits in a queue instead of overloading the server.
- **Durability.** All state is in the database, never only in memory. On startup the app re-queues every job still `pending` or `processing`. Processing only picks up `pending` certificates, so a resumed job skips work that's already done. Queued jobs are dropped on shutdown and recovered the same way.
- **Known limits, and how I'd scale:** see below.

### Partial acceptance of recipients

Validation has two levels:
1. **Envelope** (course, issuer, date, list size): an error rejects the whole request with `422`, because there is no sensible job to create.
2. **Each recipient** (`app/validation.py`): every recipient is validated separately. Invalid ones are **stored** with status `invalid`, their error messages, and the raw input. Valid ones are still generated. One typo in a list of 2,000 shouldn't force the client to resend everything, and the job report still accounts for every submitted row by `position`.

If *no* recipient is valid, the request is rejected with per-index errors instead of creating a job with nothing to do.

Duplicate emails within a job are treated as invalid. Otherwise one person could receive two certificates because a spreadsheet was pasted twice.

### Failure isolation

Each certificate is rendered, saved and **committed separately** (`services._generate_one`). If rendering or storage raises, only that row is marked `failed` with the error message, and the loop continues. Committing after each certificate also makes progress visible to `GET /jobs/{id}` while the job runs. An infrastructure error outside the loop (e.g. the database becomes unreachable) marks the whole job `failed` with a reason.

### Counts are derived, not stored

Progress numbers come from a `GROUP BY status` over the job's certificate rows (indexed on `(job_id, status)`), not from counters on the job row. They can't drift from the actual data, even after a crash between updates.

### Certificate template and storage

- The template (`app/renderer.py`) is a landscape A4 PDF drawn with ReportLab. It includes the recipient name, course, optional grade, issue date, issuer, and the certificate ID (useful for verification). Long names or course titles are scaled down to fit inside the border.
- The renderer is a pure function `CertificateData -> bytes`. That makes it easy to test, and easy to swap for a fake in tests or for another format (HTML→PDF, PNG) later.
- The built-in PDF fonts only cover Windows-1252, so names with other scripts (e.g. Chinese) are rejected during validation with a clear message instead of rendering as blank boxes. Supporting them means embedding a Unicode TTF font, which is a small, contained change in the renderer.
- Files are written atomically (temp file + rename) under `STORAGE_DIR/<job_id>/`. The database stores the *relative* path, so storage can move, or be replaced by S3, behind the same `save` / `resolve` interface.

### Data model

| Table | Holds |
|---|---|
| `jobs` | Shared certificate info, status, timestamps, job-level error |
| `certificates` | One row per submitted recipient: `position`, `status`, recipient fields, `errors` (JSON), `raw_input` (JSON, invalid rows only), `file_path`, `generated_at` |

IDs are UUIDs, so job and certificate URLs can't be guessed by counting.

---

## Limitations and next steps

- **Multiple API processes.** Each process would resume the same unfinished jobs on startup. To scale out, move generation to a dedicated queue and workers (Celery, RQ, or Arq), or let workers claim jobs atomically (`SELECT … FOR UPDATE SKIP LOCKED` on Postgres). `JobRunner` is the only interface that needs a new implementation.
- **CPU parallelism.** ReportLab is mostly pure Python, so threads mainly let *different jobs* run concurrently. For more throughput, use a `ProcessPoolExecutor` or separate worker processes.
- **Not included:** authentication, idempotency keys on `POST /jobs`, a retry endpoint for failed certificates (resetting `failed` rows to `pending` and resubmitting would be enough, because processing is already resumable), file retention/cleanup, and schema migrations (Alembic). A real deployment would use Alembic instead of `create_all`.

## Project layout

```
app/
  main.py        app factory, lifespan (create tables, start worker, resume jobs)
  api.py         HTTP routes
  schemas.py     Pydantic request/response models, recipient field rules
  validation.py  per-recipient validation + duplicate detection
  services.py    job creation, processing loop, progress reporting
  worker.py      ThreadPoolJobRunner (prod) / InlineJobRunner (tests)
  renderer.py    the certificate template (ReportLab)
  storage.py     local file storage for PDFs
  models.py      SQLAlchemy models: Job, Certificate
  database.py    engine/session setup
  config.py      settings from environment variables
tests/
```
