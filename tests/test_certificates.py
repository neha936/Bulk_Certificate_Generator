"""PDF generation service and certificate download tests."""

import uuid
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.config import PROJECT_ROOT
from app.models import Certificate, CertificateStatus
from app.services import bulk_processor
from app.services.pdf_generator import (
    CertificateRenderError,
    CertificateTheme,
    generate_certificate_pdf,
    load_theme,
)

LAYOUT_PATH = PROJECT_ROOT / "static" / "templates" / "certificate_layout.json"


# --- Test 3: PDF generation service -------------------------------------------
def test_generate_certificate_pdf_creates_valid_file(tmp_path: Path):
    cert_id = uuid.uuid4()
    output = tmp_path / "job-123" / f"{cert_id}.pdf"  # parent folder doesn't exist yet

    result = generate_certificate_pdf(
        output_path=output,
        recipient_name="Ada Lovelace",
        course_name="Advanced FastAPI & PostgreSQL",
        issue_date=date(2026, 10, 6),
        certificate_id=cert_id,
        program_title="Python Backend Bootcamp",
        theme_path=LAYOUT_PATH,
    )

    assert result == output
    assert output.is_file()
    content = output.read_bytes()
    assert content.startswith(b"%PDF-")
    assert content.rstrip().endswith(b"%%EOF")
    assert output.stat().st_size > 1000
    # The atomic write leaves no temp file behind.
    assert list(output.parent.iterdir()) == [output]


def test_generate_certificate_pdf_shrinks_long_names_to_fit(tmp_path: Path):
    output = tmp_path / "long.pdf"
    generate_certificate_pdf(
        output_path=output,
        recipient_name="Maximilian Alexander Constantine Bartholomew-Worthington",
        course_name="Course",
        issue_date=date(2026, 1, 1),
        certificate_id=uuid.uuid4(),
    )
    assert output.is_file()


@pytest.mark.parametrize(
    ("name", "course"),
    [("李雷", "Course"), ("Ada", "Курс")],
    ids=["cjk-name", "cyrillic-course"],
)
def test_generate_certificate_pdf_rejects_unrenderable_text(tmp_path: Path, name: str, course: str):
    output = tmp_path / "bad.pdf"
    with pytest.raises(CertificateRenderError, match="not supported"):
        generate_certificate_pdf(
            output_path=output,
            recipient_name=name,
            course_name=course,
            issue_date=date(2026, 1, 1),
            certificate_id=uuid.uuid4(),
        )
    assert not output.exists()


def test_generate_certificate_pdf_rejects_text_too_long_to_fit(tmp_path: Path):
    with pytest.raises(CertificateRenderError, match="too long"):
        generate_certificate_pdf(
            output_path=tmp_path / "x.pdf",
            recipient_name="W" * 200,
            course_name="Course",
            issue_date=date(2026, 1, 1),
            certificate_id=uuid.uuid4(),
        )


def test_load_theme_falls_back_to_defaults(tmp_path: Path):
    assert load_theme(None) == CertificateTheme()
    assert load_theme(tmp_path / "missing.json") == CertificateTheme()

    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    assert load_theme(broken) == CertificateTheme()


def test_load_theme_reads_layout_file():
    theme = load_theme(LAYOUT_PATH)
    assert theme.title_text == "CERTIFICATE OF COMPLETION"


# --- Test 6: download endpoint -------------------------------------------------
def test_download_generated_certificate(client: TestClient, job_payload: dict):
    job_id = client.post("/api/v1/jobs/", json=job_payload).json()["job_id"]
    cert = client.get(f"/api/v1/jobs/{job_id}/certificates").json()["items"][0]

    response = client.get(cert["download_url"])

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "attachment" in response.headers["content-disposition"]
    assert ".pdf" in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF-")


def test_get_certificate_metadata(client: TestClient, job_payload: dict):
    job_id = client.post("/api/v1/jobs/", json=job_payload).json()["job_id"]
    cert = client.get(f"/api/v1/jobs/{job_id}/certificates").json()["items"][0]

    body = client.get(f"/api/v1/certificates/{cert['id']}").json()
    assert body["id"] == cert["id"]
    assert body["job_id"] == job_id

    assert client.get(f"/api/v1/certificates/{uuid.uuid4()}").status_code == 404


def test_download_unknown_certificate_returns_404(client: TestClient):
    assert client.get(f"/api/v1/certificates/{uuid.uuid4()}/download").status_code == 404


def test_download_failed_certificate_returns_409_with_reason(
    client: TestClient, job_payload: dict, monkeypatch: pytest.MonkeyPatch
):
    def always_fail(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(bulk_processor, "generate_certificate_pdf", always_fail)
    job_id = client.post("/api/v1/jobs/", json=job_payload).json()["job_id"]
    cert_id = client.get(f"/api/v1/jobs/{job_id}/certificates").json()["items"][0]["id"]

    response = client.get(f"/api/v1/certificates/{cert_id}/download")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["status"] == CertificateStatus.FAILED
    assert "boom" in detail["error_message"]


def test_download_returns_404_when_file_missing_on_disk(
    client: TestClient, job_payload: dict, storage_dir: Path
):
    job_id = client.post("/api/v1/jobs/", json=job_payload).json()["job_id"]
    cert_id = client.get(f"/api/v1/jobs/{job_id}/certificates").json()["items"][0]["id"]
    (storage_dir / job_id / f"{cert_id}.pdf").unlink()

    assert client.get(f"/api/v1/certificates/{cert_id}/download").status_code == 404


def test_download_refuses_paths_outside_storage_root(
    client: TestClient, job_payload: dict, session_factory: sessionmaker[Session], tmp_path: Path
):
    """Path traversal guard: a tampered file_path must never be served."""
    secret = tmp_path / "secret.pdf"
    secret.write_bytes(b"%PDF-secret")
    job_id = client.post("/api/v1/jobs/", json=job_payload).json()["job_id"]
    cert_id = client.get(f"/api/v1/jobs/{job_id}/certificates").json()["items"][0]["id"]

    with session_factory() as db:
        cert = db.get(Certificate, uuid.UUID(cert_id))
        cert.file_path = "../secret.pdf"
        db.commit()

    assert client.get(f"/api/v1/certificates/{cert_id}/download").status_code == 404
