"""Job submission, validation, progress tracking and fault-tolerance tests."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Certificate, CertificateJob, CertificateStatus, JobStatus
from app.services import bulk_processor
from app.services.bulk_processor import process_job


# --- Test 1: submit a valid job ------------------------------------------------
def test_submit_job_returns_202_and_creates_records(client: TestClient, job_payload: dict, db_session: Session):
    response = client.post("/api/v1/jobs/", json=job_payload)

    assert response.status_code == 202
    body = response.json()
    job_id = uuid.UUID(body["job_id"])
    # The response describes the state *at submission time*. Processing hasn't started yet.
    assert body["status"] == JobStatus.PENDING
    assert body["total_recipients"] == 3
    assert body["status_url"] == f"/api/v1/jobs/{job_id}"
    assert response.headers["Location"] == body["status_url"]

    job = db_session.get(CertificateJob, job_id)
    assert job is not None
    assert job.title == job_payload["title"]
    certs = db_session.scalars(select(Certificate).where(Certificate.job_id == job_id)).all()
    assert {c.recipient_email for c in certs} == {r["email"] for r in job_payload["recipients"]}


def test_submit_job_without_trailing_slash_is_accepted(client: TestClient, job_payload: dict):
    assert client.post("/api/v1/jobs", json=job_payload).status_code == 202


# --- Test 2: input validation ------------------------------------------------
def test_submit_job_rejects_empty_recipients(client: TestClient, job_payload: dict, db_session: Session):
    job_payload["recipients"] = []
    response = client.post("/api/v1/jobs/", json=job_payload)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "recipients"]
    # Fail fast: nothing is persisted for an invalid request.
    assert db_session.scalars(select(CertificateJob)).first() is None


def test_submit_job_rejects_invalid_email_and_reports_its_index(client: TestClient, job_payload: dict):
    job_payload["recipients"][1]["email"] = "not-an-email"
    response = client.post("/api/v1/jobs/", json=job_payload)

    assert response.status_code == 422
    locs = [err["loc"] for err in response.json()["detail"]]
    assert ["body", "recipients", 1, "email"] in locs


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("title"),
        lambda p: p.update(course_name="   "),  # whitespace-only is treated as empty
        lambda p: p.update(issue_date="06/10/2026"),  # not ISO-8601
        lambda p: p["recipients"][0].update(name=""),
        lambda p: p["recipients"][0].update(unexpected="field"),  # extra="forbid"
    ],
    ids=["missing-title", "blank-course", "bad-date", "blank-name", "unknown-field"],
)
def test_submit_job_rejects_malformed_payloads(client: TestClient, job_payload: dict, mutate):
    mutate(job_payload)
    assert client.post("/api/v1/jobs/", json=job_payload).status_code == 422


def test_submit_job_enforces_configured_max_recipients(client: TestClient, job_payload: dict):
    job_payload["recipients"] = [{"name": f"User {i}", "email": f"user{i}@example.com"} for i in range(51)]
    response = client.post("/api/v1/jobs/", json=job_payload)
    assert response.status_code == 413


# --- Test 4: job status and completion metrics ---------------------------------
def test_job_status_reports_completion_metrics(client: TestClient, job_payload: dict, storage_dir: Path):
    job_id = client.post("/api/v1/jobs/", json=job_payload).json()["job_id"]

    response = client.get(f"/api/v1/jobs/{job_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == JobStatus.COMPLETED
    assert body["total_recipients"] == 3
    assert body["successful_count"] == 3
    assert body["failed_count"] == 0
    assert body["pending_count"] == 0
    assert body["progress_percentage"] == 100.0
    assert len(body["certificates"]) == 3
    for cert in body["certificates"]:
        assert cert["status"] == CertificateStatus.SUCCESS
        assert cert["error_message"] is None
        assert cert["download_url"] == f"/api/v1/certificates/{cert['id']}/download"
        # Storage layout: storage/certificates/<job_id>/<cert_id>.pdf
        assert (storage_dir / job_id / f"{cert['id']}.pdf").is_file()
        assert "file_path" not in cert  # internal path is never exposed


def test_get_unknown_job_returns_404(client: TestClient):
    assert client.get(f"/api/v1/jobs/{uuid.uuid4()}").status_code == 404


def test_get_job_with_malformed_id_returns_422(client: TestClient):
    assert client.get("/api/v1/jobs/not-a-uuid").status_code == 422


def test_list_job_certificates_supports_pagination_and_status_filter(client: TestClient, job_payload: dict):
    job_id = client.post("/api/v1/jobs/", json=job_payload).json()["job_id"]

    page = client.get(f"/api/v1/jobs/{job_id}/certificates", params={"skip": 1, "limit": 1}).json()
    assert page["total"] == 3
    assert len(page["items"]) == 1

    failed = client.get(f"/api/v1/jobs/{job_id}/certificates", params={"status": "FAILED"}).json()
    assert failed["total"] == 0

    all_items = client.get(f"/api/v1/jobs/{job_id}/certificates").json()["items"]
    assert {i["recipient_name"] for i in all_items} == {"Ada Lovelace", "Alan Turing", "Grace Hopper"}
    ada = next(i for i in all_items if i["recipient_name"] == "Ada Lovelace")
    assert ada["meta"] == {"grade": "A"}


def test_list_certificates_for_unknown_job_returns_404(client: TestClient):
    assert client.get(f"/api/v1/jobs/{uuid.uuid4()}/certificates").status_code == 404


# --- Test 5: per-certificate failure handling ---------------------------------
def test_single_certificate_failure_does_not_fail_the_batch(
    client: TestClient, job_payload: dict, monkeypatch: pytest.MonkeyPatch, storage_dir: Path
):
    """Simulate a crash while rendering ONE recipient. The rest must succeed."""
    real_generate = bulk_processor.generate_certificate_pdf

    def flaky_generate(**kwargs):
        if kwargs["recipient_name"] == "Alan Turing":
            raise RuntimeError("simulated renderer crash")
        return real_generate(**kwargs)

    monkeypatch.setattr(bulk_processor, "generate_certificate_pdf", flaky_generate)

    job_id = client.post("/api/v1/jobs/", json=job_payload).json()["job_id"]
    body = client.get(f"/api/v1/jobs/{job_id}").json()

    assert body["status"] == JobStatus.PARTIALLY_FAILED
    assert body["successful_count"] == 2
    assert body["failed_count"] == 1
    assert body["progress_percentage"] == 100.0

    by_name = {c["recipient_name"]: c for c in body["certificates"]}
    failed = by_name["Alan Turing"]
    assert failed["status"] == CertificateStatus.FAILED
    assert "simulated renderer crash" in failed["error_message"]
    assert failed["download_url"] is None
    for name in ("Ada Lovelace", "Grace Hopper"):
        assert by_name[name]["status"] == CertificateStatus.SUCCESS
        assert (storage_dir / job_id / f"{by_name[name]['id']}.pdf").is_file()


def test_unrenderable_recipient_data_fails_only_that_record(client: TestClient, job_payload: dict):
    """A real (not mocked) data-level failure: characters the PDF font can't draw.
    The request passes schema validation, and only that recipient fails."""
    job_payload["recipients"].append({"name": "李雷", "email": "li@example.com"})

    job_id = client.post("/api/v1/jobs/", json=job_payload).json()["job_id"]
    body = client.get(f"/api/v1/jobs/{job_id}").json()

    assert body["status"] == JobStatus.PARTIALLY_FAILED
    assert (body["successful_count"], body["failed_count"]) == (3, 1)
    failed = next(c for c in body["certificates"] if c["status"] == CertificateStatus.FAILED)
    assert failed["recipient_email"] == "li@example.com"
    assert "not supported by the certificate font" in failed["error_message"]


def test_job_is_failed_when_every_certificate_fails(
    client: TestClient, job_payload: dict, monkeypatch: pytest.MonkeyPatch
):
    def always_fail(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(bulk_processor, "generate_certificate_pdf", always_fail)

    job_id = client.post("/api/v1/jobs/", json=job_payload).json()["job_id"]
    body = client.get(f"/api/v1/jobs/{job_id}").json()

    assert body["status"] == JobStatus.FAILED
    assert (body["successful_count"], body["failed_count"]) == (0, 3)


# --- Processor robustness ------------------------------------------------------
def _create_pending_job(client: TestClient, job_payload: dict, monkeypatch: pytest.MonkeyPatch) -> str:
    """Submit a job but stop the background task from running, leaving it PENDING."""
    monkeypatch.setattr("app.routers.jobs.process_job", lambda *a, **k: None)
    job_id = client.post("/api/v1/jobs/", json=job_payload).json()["job_id"]
    monkeypatch.undo()
    return job_id


def test_process_job_is_resumable_and_idempotent(
    client: TestClient,
    job_payload: dict,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    storage_dir: Path,
):
    job_id = uuid.UUID(_create_pending_job(client, job_payload, monkeypatch))

    # Simulate a crash after the first certificate: mark it SUCCESS by hand.
    with session_factory() as db:
        job = db.get(CertificateJob, job_id)
        first = job.certificates[0]
        first.status, first.file_path = CertificateStatus.SUCCESS, "already/done.pdf"
        job.successful_count, job.status = 1, JobStatus.PROCESSING
        db.commit()

    calls: list[str] = []
    real_generate = bulk_processor.generate_certificate_pdf
    monkeypatch.setattr(
        bulk_processor,
        "generate_certificate_pdf",
        lambda **kw: calls.append(kw["recipient_name"]) or real_generate(**kw),
    )

    process_job(job_id, session_factory, storage_dir)
    assert len(calls) == 2  # only the two PENDING certificates were rendered

    process_job(job_id, session_factory, storage_dir)  # finished job: no-op
    assert len(calls) == 2

    with session_factory() as db:
        job = db.get(CertificateJob, job_id)
        assert job.status == JobStatus.COMPLETED
        assert (job.successful_count, job.failed_count) == (3, 0)


def test_process_job_with_unknown_id_is_a_noop(session_factory: sessionmaker[Session], storage_dir: Path):
    process_job(uuid.uuid4(), session_factory, storage_dir)  # must not raise


def test_infrastructure_failure_marks_job_failed(
    client: TestClient,
    job_payload: dict,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    storage_dir: Path,
):
    job_id = uuid.UUID(_create_pending_job(client, job_payload, monkeypatch))

    def broken_query(*args, **kwargs):
        raise RuntimeError("database connection lost")

    monkeypatch.setattr(bulk_processor.crud, "pending_certificate_ids", broken_query)
    process_job(job_id, session_factory, storage_dir)  # must not raise

    with session_factory() as db:
        assert db.get(CertificateJob, job_id).status == JobStatus.FAILED


def test_health_endpoint(client: TestClient):
    assert client.get("/health").json() == {"status": "ok"}
