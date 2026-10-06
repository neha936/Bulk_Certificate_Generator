# Bulk Certificate Generator API

A REST API that takes a batch of recipients and generates one branded PDF certificate per recipient in the background. Clients can track job progress and download each PDF.

**Stack:** Python 3.11+ · FastAPI · SQLAlchemy 2.0 · PostgreSQL (SQLite fallback) · ReportLab · pytest

```
POST /api/v1/jobs/                          → 202 Accepted {job_id}   (generation runs in background)
GET  /api/v1/jobs/{job_id}                  → progress %, counters, per-certificate statuses
GET  /api/v1/jobs/{job_id}/certificates     → paginated list (?status=FAILED&skip=0&limit=100)
GET  /api/v1/certificates/{id}              → single certificate metadata
GET  /api/v1/certificates/{id}/download     → streams the PDF
GET  /health                                → liveness probe
```

Interactive docs: **http://localhost:8000/docs** (Swagger UI) · http://localhost:8000/redoc

---

## 1. Setup & Run

### Option A: Local Python (SQLite, no external services)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # optional; every setting has a sensible default
uvicorn app.main:app --reload
```

Tables are created automatically on startup. PDFs are written to `storage/certificates/<job_id>/<cert_id>.pdf`.

To use a local PostgreSQL instead, set `DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/certificates`.

### Option B: Docker Compose (PostgreSQL + API)

```bash
docker compose up --build
```

- API on `http://localhost:8000`, PostgreSQL on `localhost:5432` (user/password/db: `certs`/`certs`/`certificates`).
- The API waits for Postgres's health check before starting.
- Generated PDFs are kept in the `certificates` named volume.

### Configuration

All settings are environment variables (see [.env.example](.env.example)):

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./certificates.db` | SQLAlchemy URL |
| `STORAGE_DIR` | `./storage/certificates` | Where PDFs are written |
| `CERTIFICATE_LAYOUT_PATH` | `./static/templates/certificate_layout.json` | Colours/fonts/title of the certificate |
| `MAX_RECIPIENTS_PER_JOB` | `1000` | Larger requests get `413` |
| `AUTO_CREATE_TABLES` | `true` | Turn off in production and use migrations |
| `LOG_LEVEL` | `INFO` | |

---

## 2. Running Tests

```bash
pytest -v
pytest --cov=app --cov-report=term-missing     # with coverage (currently ~97%)
```

Each test gets a fresh **in-memory SQLite** database and a temporary storage directory, injected through FastAPI `dependency_overrides`. No running services are needed. Starlette's `TestClient` runs background tasks synchronously once the response is sent, so the tests need no sleeps or polling.

| Required scenario | Test(s) |
|---|---|
| 1. Submit valid job → 202 + records created | `test_jobs.py::test_submit_job_returns_202_and_creates_records` |
| 2. Validation failure (empty list / bad email) | `test_submit_job_rejects_empty_recipients`, `test_submit_job_rejects_invalid_email_and_reports_its_index`, `test_submit_job_rejects_malformed_payloads[*]` |
| 3. PDF file created on disk | `test_certificates.py::test_generate_certificate_pdf_creates_valid_file` |
| 4. Job status & completion metrics | `test_job_status_reports_completion_metrics` |
| 5. Per-certificate failure, rest completes | `test_single_certificate_failure_does_not_fail_the_batch`, `test_unrenderable_recipient_data_fails_only_that_record` |
| 6. Download endpoint | `test_download_generated_certificate` (+ 404 / 409 / path-traversal cases) |

Additional tests cover resumability/idempotency, infrastructure failure handling, pagination and filtering, theme fallback, and long-name auto-fit.

---

## 3. Sample Requests

### Create a job

```bash
curl -i -X POST http://localhost:8000/api/v1/jobs/ \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Python Backend Bootcamp - Autumn 2026",
    "course_name": "Advanced FastAPI & PostgreSQL",
    "issue_date": "2026-10-06",
    "recipients": [
      {"name": "Ada Lovelace", "email": "ada@example.com", "meta": {"grade": "A"}},
      {"name": "Alan Turing",  "email": "alan@example.com"},
      {"name": "Grace Hopper", "email": "grace@example.com"}
    ]
  }'
```

```http
HTTP/1.1 202 Accepted
Location: /api/v1/jobs/c8efbf16-3bab-4a2d-bca7-55e56d4bc02e

{
  "job_id": "c8efbf16-3bab-4a2d-bca7-55e56d4bc02e",
  "status": "PENDING",
  "total_recipients": 3,
  "status_url": "/api/v1/jobs/c8efbf16-3bab-4a2d-bca7-55e56d4bc02e",
  "message": "Job accepted. Poll status_url for progress."
}
```

### Poll progress

```bash
curl http://localhost:8000/api/v1/jobs/<job_id>
```

```json
{
  "id": "c8efbf16-...",
  "title": "Python Backend Bootcamp - Autumn 2026",
  "status": "COMPLETED",
  "total_recipients": 3,
  "successful_count": 3,
  "failed_count": 0,
  "pending_count": 0,
  "progress_percentage": 100.0,
  "certificates": [
    {
      "id": "3a0d7464-...",
      "recipient_name": "Ada Lovelace",
      "recipient_email": "ada@example.com",
      "status": "SUCCESS",
      "error_message": null,
      "download_url": "/api/v1/certificates/3a0d7464-.../download",
      "...": "..."
    }
  ]
}
```

### List only the failures

```bash
curl "http://localhost:8000/api/v1/jobs/<job_id>/certificates?status=FAILED"
```

### Download a PDF

```bash
curl -OJ http://localhost:8000/api/v1/certificates/<certificate_id>/download
# saves e.g. certificate_Ada_Lovelace_3a0d7464.pdf
```

### Error responses

| Situation | Status |
|---|---|
| Empty `recipients`, malformed email, missing/blank field, unknown field | `422` with the exact location, e.g. `["body","recipients",1,"email"]` |
| More than `MAX_RECIPIENTS_PER_JOB` recipients | `413` |
| Unknown job/certificate ID | `404` |
| Download of a certificate that is `PENDING` or `FAILED` | `409` with `status` and `error_message` |

---

## 4. Design Decisions

### 4.1 FastAPI and asynchronous background execution

- **FastAPI** gives request validation with Pydantic v2, auto-generated OpenAPI docs, and dependency injection. Dependency injection is also the test seam: tests swap the DB session factory and settings without monkeypatching globals.
- **202 Accepted + polling.** Generating hundreds of PDFs can take far longer than an HTTP timeout. The `POST` handler only validates, inserts the job and its `PENDING` certificate rows in **one transaction**, commits, schedules the work, and returns. Clients poll `status_url` (also sent in the `Location` header).
- **FastAPI `BackgroundTasks` rather than Celery** for this scope. It needs no broker and no extra worker containers, and the task runs right after the response is sent. Its trade-off: work runs inside the API process, so a crash or redeploy interrupts running jobs. The design accounts for this:
  - `process_job(job_id, session_factory, storage_dir)` is a plain function that opens **its own DB session**. The request session is already closed by the time it runs. Turning it into a Celery task is a one-line `@celery_app.task` wrapper.
  - Processing is **resumable**: only `PENDING` certificates are picked up, so re-enqueuing an interrupted job finishes the rest without duplicates.
- **Sync SQLAlchemy and sync endpoints, deliberately.** FastAPI runs `def` endpoints and `def` background tasks in a thread pool, so blocking calls never block the event loop. PDF rendering (ReportLab) is CPU-bound and synchronous anyway, so an async DB driver would add complexity without a throughput gain.
- **ReportLab** is pure Python with no system dependencies (WeasyPrint needs Cairo/Pango, pdfkit needs wkhtmltopdf). The image stays small and rendering is fast and deterministic. The visual theme (colours, fonts, title) lives in [static/templates/certificate_layout.json](static/templates/certificate_layout.json), so it can be changed without code changes. Long names auto-shrink to fit.

### 4.2 Fault-tolerance strategy for individual certificate failures

Failures are handled at three levels, each with a clear owner:

| Level | Example | Handling |
|---|---|---|
| **Request** (structural) | bad email, empty list, blank name | Rejected up front with `422`; nothing persisted. A typo in a batch is a client bug, and failing fast with the exact index is the most useful response. |
| **Certificate** (per-record) | characters the font can't draw, text too long, ReportLab/I-O error | Caught **inside the loop for that recipient only**. The row becomes `FAILED` with a readable `error_message`, and the loop carries on. |
| **Infrastructure** | database unreachable mid-job | Can't reliably record per-row results, so we roll back, make a best-effort attempt to mark the job `FAILED`, and log the traceback. Unprocessed rows stay `PENDING` for a resume. |

Details:
- **One commit per certificate.** Each outcome is durable as soon as it happens. Progress is visible in real time, and a crash loses at most the certificate being rendered. The job's counters are updated **in the same transaction** as the certificate row, so they always match.
- **Expected vs. unexpected errors.** `CertificateRenderError` (bad data) is logged as a warning and its message is shown to the client as-is. Any other exception is logged with a full traceback for operators, and the client gets a short summary.
- **Final status is derived, not assumed.** `finalize_job` recounts the rows: no failures gives `COMPLETED`, no successes gives `FAILED`, otherwise `PARTIALLY_FAILED`.
- **Atomic file writes.** PDFs are rendered to a temp file and moved into place with `os.replace`, so a half-written file is never served.
- **Explicit rejection beats silent corruption.** ReportLab's built-in fonts only cover Latin-1/cp1252. Instead of issuing a certificate with an unreadable name, that recipient fails with a clear message. (Fix: register a Unicode TTF font such as Noto Sans. See Future Work.)

### 4.3 Database schema design

```
certificate_jobs                              certificates
───────────────────────────                   ──────────────────────────────────
id               UUID  PK                     id              UUID  PK
title            VARCHAR(200)                 job_id          UUID  FK → certificate_jobs.id (ON DELETE CASCADE)
status           VARCHAR(20)  idx  ◄────1:N── recipient_name  VARCHAR(200)
total_recipients INT                          recipient_email VARCHAR(320)
successful_count INT                          course_name     VARCHAR(200)
failed_count     INT                          issue_date      DATE
created_at       TIMESTAMPTZ                  meta            JSON
updated_at       TIMESTAMPTZ                  status          VARCHAR(20)
                                              file_path       VARCHAR(500) NULL   (relative to STORAGE_DIR)
                                              error_message   TEXT NULL
                                              created_at      TIMESTAMPTZ
                                              INDEX (job_id, status)
```

- **The job is the unit of submission. The certificate is the unit of work and failure.** Status and error live on each certificate, which is what makes partial failure possible.
- **Denormalised counters** on the job make the progress endpoint an O(1) read on a single row, with no `COUNT(*)` over thousands of children on every poll. `finalize_job` recounts at the end as a consistency check.
- **Composite index `(job_id, status)`** serves the hot queries: "certificates of job X" and "pending/failed certificates of job X".
- **UUID primary keys** can't be guessed or enumerated. That matters because certificate IDs appear in public download URLs and on the PDF itself.
- **`course_name` / `issue_date` copied onto each certificate.** An issued certificate is an immutable record of what was printed, and it should not change if job-level data does.
- **Relative `file_path`.** The DB stays valid if the storage volume is mounted elsewhere or moved to object storage. The internal path is never exposed. Clients get a `download_url`, and the download endpoint checks that the resolved path stays inside the storage root.
- **Enums stored as `VARCHAR` + CHECK** (`native_enum=False`): identical behaviour on PostgreSQL and SQLite, and adding a status needs no `ALTER TYPE` migration.

---

## 5. Project Structure

```
app/
├── main.py              # App factory, lifespan (storage dir + table creation), /health
├── config.py            # pydantic-settings: env-driven configuration
├── database.py          # Engine, session factory, get_db / get_session_factory dependencies
├── models.py            # CertificateJob, Certificate, status enums
├── schemas.py           # Pydantic v2 request/response models
├── crud.py              # All DB queries; callers own the transaction boundary
├── services/
│   ├── pdf_generator.py # Pure ReportLab renderer (no DB): inputs → PDF on disk
│   └── bulk_processor.py# Background orchestration, per-certificate error boundary
└── routers/
    ├── jobs.py          # POST /jobs, GET /jobs/{id}, GET /jobs/{id}/certificates
    └── certificates.py  # GET /certificates/{id}, GET /certificates/{id}/download
static/templates/certificate_layout.json   # Certificate theme (colours, fonts, title)
storage/certificates/                       # Generated PDFs (<job_id>/<cert_id>.pdf)
tests/                                      # pytest suite (in-memory SQLite)
```

---

## 6. Scaling & Future Work

- **Celery + Redis/RabbitMQ** for durability and horizontal scale: wrap `process_job` as a task, or fan out one task per certificate, with retries and exponential backoff for transient errors. Run multiple API replicas behind a load balancer.
- **Startup recovery / reaper**: re-enqueue jobs stuck in `PENDING`/`PROCESSING` after a restart. Processing is already resumable, so this is a small addition.
- **Alembic migrations** instead of `create_all`.
- **Object storage (S3/GCS)** with pre-signed download URLs instead of a local volume.
- **Unicode fonts** (register a TTF such as Noto Sans) so non-Latin names render.
- **AuthN/AuthZ** (API keys / OAuth2) and per-tenant job ownership. Rate limiting on `POST /jobs`.
- **Bulk ZIP download** per job, **webhook / email delivery** when a job finishes, and an `Idempotency-Key` header on `POST /jobs` to make client retries safe.
