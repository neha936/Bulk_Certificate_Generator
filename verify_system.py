#!/usr/bin/env python3
"""End-to-end verification of a *running* Bulk Certificate Generator API.

Unlike the pytest suite (which runs in-process against an in-memory DB), this
script talks to a live Uvicorn server over HTTP. It checks the real
background processing, the real database and the real files on disk.

Usage:
    uvicorn app.main:app                      # terminal 1
    python verify_system.py                   # terminal 2
    python verify_system.py --base-url http://127.0.0.1:8000 --storage-dir ./storage/certificates --timeout 60

Exit code is 0 when every check passes, 1 otherwise (CI friendly).
Only dependency: httpx (already in requirements.txt).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover
    sys.exit("httpx is required: pip install httpx")

PROJECT_ROOT = Path(__file__).resolve().parent
API = "/api/v1"
TERMINAL_JOB_STATES = {"COMPLETED", "PARTIALLY_FAILED", "FAILED"}

# ----------------------------------------------------------------- output ---

if sys.platform == "win32":
    os.system("")  # enables ANSI escape processing in the Windows console
try:
    sys.stdout.reconfigure(encoding="utf-8")  # ✅/❌ on legacy Windows code pages
except (AttributeError, ValueError):
    pass

USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


GREEN, RED, YELLOW, CYAN, BOLD, DIM = (
    lambda t: _c("32", t),
    lambda t: _c("31", t),
    lambda t: _c("33", t),
    lambda t: _c("36", t),
    lambda t: _c("1", t),
    lambda t: _c("2", t),
)


@dataclass
class Report:
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    failures: list[str] = field(default_factory=list)
    section_results: dict[str, bool] = field(default_factory=dict)
    _current: str = ""

    def section(self, title: str) -> None:
        self._current = title
        self.section_results.setdefault(title, True)
        print(f"\n{BOLD(CYAN('━━ ' + title))}")

    def check(self, name: str, condition: bool, detail: Any = "") -> bool:
        if condition:
            self.passed += 1
            print(f"  {GREEN('✅ PASS')}  {name}" + (f"  {DIM(str(detail))}" if detail != "" else ""))
        else:
            self.failed += 1
            self.section_results[self._current] = False
            self.failures.append(f"[{self._current}] {name}: {detail}")
            print(f"  {RED('❌ FAIL')}  {name}" + (f"\n           {RED(str(detail))}" if detail != "" else ""))
        return condition

    def skip(self, name: str, reason: str) -> None:
        self.skipped += 1
        print(f"  {YELLOW('⏭️  SKIP')}  {name}  {DIM(reason)}")

    def info(self, text: str) -> None:
        print(f"  {DIM('ℹ️  ' + text)}")

    def warn(self, text: str) -> None:
        print(f"  {YELLOW('⚠️  ' + text)}")

    def crash(self, exc: BaseException) -> None:
        """An unexpected exception inside a section counts as a failure, but
        doesn't abort the rest of the run."""
        tb = traceback.format_exception_only(type(exc), exc)[-1].strip()
        self.check("section completed without unexpected errors", False, tb)


# ---------------------------------------------------------------- helpers ---


def is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


def error_locs(response: httpx.Response) -> list[list]:
    try:
        return [e.get("loc") for e in response.json().get("detail", [])]
    except Exception:  # noqa: BLE001
        return []


def resolve_storage_dir(cli_value: str | None, report: Report) -> Path:
    """Mirror the server's resolution: --storage-dir > env STORAGE_DIR > .env STORAGE_DIR > default."""
    if cli_value:
        return Path(cli_value).resolve()
    if os.environ.get("STORAGE_DIR"):
        return Path(os.environ["STORAGE_DIR"]).resolve()

    env_file = PROJECT_ROOT / ".env"
    values: dict[str, str] = {}
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip().upper()] = v.strip().strip('"').strip("'")
    if "STORAGE_PATH" in values and "STORAGE_DIR" not in values:
        report.warn(
            ".env defines STORAGE_PATH, but the app reads STORAGE_DIR, so the server ignores it "
            "and uses the default storage/certificates. Rename the variable to STORAGE_DIR."
        )
    if "STORAGE_DIR" in values:
        p = Path(values["STORAGE_DIR"])
        return (p if p.is_absolute() else PROJECT_ROOT / p).resolve()
    return (PROJECT_ROOT / "storage" / "certificates").resolve()


def poll_job(client: httpx.Client, job_id: str, timeout: float, interval: float = 0.25) -> tuple[dict | None, list[str]]:
    """Poll until the job reaches a terminal state. Returns (final_body, observed_status_sequence)."""
    observed: list[str] = []
    deadline = time.monotonic() + timeout
    body = None
    while time.monotonic() < deadline:
        r = client.get(f"{API}/jobs/{job_id}")
        if r.status_code != 200:
            return None, observed
        body = r.json()
        if not observed or observed[-1] != body["status"]:
            observed.append(body["status"])
        if body["status"] in TERMINAL_JOB_STATES:
            return body, observed
        time.sleep(interval)
    return body, observed


def run_section(report: Report, title: str, fn: Callable[[], None]) -> None:
    report.section(title)
    try:
        fn()
    except httpx.HTTPError as exc:
        report.crash(exc)
    except Exception as exc:  # noqa: BLE001
        report.crash(exc)


# ------------------------------------------------------------------ suite ---


class SystemVerifier:
    def __init__(self, base_url: str, storage_dir: Path, timeout: float, report: Report) -> None:
        self.client = httpx.Client(base_url=base_url, timeout=30.0)
        self.storage_dir = storage_dir
        self.timeout = timeout
        self.r = report
        self.run_tag = uuid.uuid4().hex[:6]  # makes this run's jobs easy to spot in the DB
        # State shared between sections
        self.valid_job_id: str | None = None
        self.valid_job: dict | None = None
        self.partial_job_id: str | None = None
        self.partial_job: dict | None = None
        self.downloaded: dict[str, bytes] = {}

    # -- payloads ----------------------------------------------------------
    def valid_payload(self) -> dict:
        return {
            "title": f"QA Verification Run {self.run_tag}",
            "course_name": "Advanced FastAPI & PostgreSQL",
            "issue_date": date.today().isoformat(),
            "recipients": [
                {"name": "Ada Lovelace", "email": f"ada.{self.run_tag}@example.com", "meta": {"grade": "A"}},
                {"name": "Alan Turing", "email": f"alan.{self.run_tag}@example.com"},
                {"name": "Grace Hopper", "email": f"grace.{self.run_tag}@example.com", "meta": {"cohort": 7}},
                {"name": "José Núñez-O'Brien", "email": f"jose.{self.run_tag}@example.com"},  # Latin-1 accents: must succeed
            ],
        }

    def partial_payload(self) -> dict:
        """Two recipients pass schema validation but are engineered to fail during PDF rendering."""
        return {
            "title": f"QA Partial Failure Run {self.run_tag}",
            "course_name": "Fault Tolerance 101",
            "issue_date": date.today().isoformat(),
            "recipients": [
                {"name": "Valid Person One", "email": f"one.{self.run_tag}@example.com"},
                # Characters the built-in PDF fonts can't draw → CertificateRenderError
                {"name": "李雷", "email": f"cjk.{self.run_tag}@example.com"},
                {"name": "Valid Person Two", "email": f"two.{self.run_tag}@example.com"},
                # 200 chars passes the schema (max_length=200) but can't fit on the page even at min font size
                {"name": "W" * 200, "email": f"long.{self.run_tag}@example.com"},
                {"name": "Valid Person Three", "email": f"three.{self.run_tag}@example.com"},
            ],
        }

    # -- 1 -----------------------------------------------------------------
    def health(self) -> None:
        try:
            r = self.client.get("/health")
        except httpx.ConnectError as exc:
            self.r.check("server reachable", False, f"cannot connect to {self.client.base_url}: {exc}")
            raise SystemExit(self.r.failed)  # nothing else can run
        self.r.check("GET /health returns 200", r.status_code == 200, r.status_code)
        self.r.check("health body is {'status': 'ok'}", r.json() == {"status": "ok"}, r.text[:200])

        r = self.client.get("/openapi.json")
        paths = r.json().get("paths", {}) if r.status_code == 200 else {}
        expected = [
            f"{API}/jobs/",
            f"{API}/jobs/{{job_id}}",
            f"{API}/jobs/{{job_id}}/certificates",
            f"{API}/certificates/{{certificate_id}}",
            f"{API}/certificates/{{certificate_id}}/download",
        ]
        missing = [p for p in expected if p not in paths]
        self.r.check("OpenAPI schema lists all API routes", not missing, f"missing: {missing}" if missing else f"{len(paths)} paths")
        self.r.check("Swagger UI (/docs) is served", self.client.get("/docs").status_code == 200)

    # -- 2 -----------------------------------------------------------------
    def create_job(self) -> None:
        payload = self.valid_payload()
        r = self.client.post(f"{API}/jobs/", json=payload)
        self.r.check("POST /jobs/ returns 202 Accepted (or 201)", r.status_code in (201, 202), r.status_code)
        if r.status_code not in (201, 202):
            self.r.info(f"response: {r.text[:300]}")
            return
        body = r.json()
        self.valid_job_id = body.get("job_id")
        self.r.check("response contains a valid UUID job_id", is_uuid(self.valid_job_id), self.valid_job_id)
        self.r.check("initial status is PENDING", body.get("status") == "PENDING", body.get("status"))
        self.r.check(
            "total_recipients matches payload",
            body.get("total_recipients") == len(payload["recipients"]),
            body.get("total_recipients"),
        )
        self.r.check(
            "Location header points to the status URL",
            r.headers.get("location") == f"{API}/jobs/{self.valid_job_id}" == body.get("status_url"),
            r.headers.get("location"),
        )

        r2 = self.client.post(f"{API}/jobs", json={**payload, "title": payload["title"] + " (no slash)"})
        self.r.check("POST /jobs (no trailing slash) also returns 202", r2.status_code == 202, r2.status_code)

    # -- 3 -----------------------------------------------------------------
    def validation(self) -> None:
        def post(p: dict) -> httpx.Response:
            return self.client.post(f"{API}/jobs/", json=p)

        p = self.valid_payload()
        p["recipients"][1]["email"] = "not-an-email"
        r = post(p)
        self.r.check("invalid email → 422", r.status_code == 422, r.status_code)
        self.r.check(
            "422 error pinpoints recipients[1].email",
            ["body", "recipients", 1, "email"] in error_locs(r),
            error_locs(r),
        )

        for bad in ("missing-at.example.com", "user@", "@example.com", "a b@example.com"):
            p = self.valid_payload()
            p["recipients"][0]["email"] = bad
            r = post(p)
            self.r.check(f"malformed email {bad!r} → 422", r.status_code == 422, r.status_code)

        p = self.valid_payload()
        p["recipients"] = []
        r = post(p)
        self.r.check("empty recipient list → 422", r.status_code == 422, r.status_code)

        cases: dict[str, Callable[[dict], Any]] = {
            "missing recipients field → 422": lambda p: p.pop("recipients"),
            "missing title → 422": lambda p: p.pop("title"),
            "whitespace-only course_name → 422": lambda p: p.update(course_name="   "),
            "non-ISO issue_date → 422": lambda p: p.update(issue_date="07/10/2026"),
            "blank recipient name → 422": lambda p: p["recipients"][0].update(name=""),
            "unknown extra field → 422": lambda p: p["recipients"][0].update(phone="123"),
            "nested meta object (non-scalar) → 422": lambda p: p["recipients"][0].update(meta={"x": {"y": 1}}),
        }
        for label, mutate in cases.items():
            p = self.valid_payload()
            mutate(p)
            r = post(p)
            self.r.check(label, r.status_code == 422, r.status_code)

        r = self.client.post(f"{API}/jobs/", content=b"{not json", headers={"Content-Type": "application/json"})
        self.r.check("malformed JSON body → 422", r.status_code == 422, r.status_code)

        p = self.valid_payload()
        p["recipients"] = [{"name": f"User {i}", "email": f"u{i}.{self.run_tag}@example.com"} for i in range(1001)]
        r = post(p)
        self.r.check("over MAX_RECIPIENTS_PER_JOB (default 1000) → 413", r.status_code == 413, r.status_code)

    # -- 4 -----------------------------------------------------------------
    def progress(self) -> None:
        if not self.valid_job_id:
            self.r.skip("job progress tracking", "no job was created in step 2")
            return
        started = time.monotonic()
        body, observed = poll_job(self.client, self.valid_job_id, self.timeout)
        elapsed = time.monotonic() - started
        self.r.info(f"observed status sequence: {' → '.join(observed) or '(none)'}  ({elapsed:.2f}s)")
        if not self.r.check("GET /jobs/{id} returns the job", body is not None):
            return
        self.valid_job = body
        self.r.check(
            f"reaches COMPLETED within {self.timeout:.0f}s",
            body["status"] == "COMPLETED",
            f"final status={body['status']}",
        )
        self.r.check(
            "only valid state transitions observed",
            all(s in {"PENDING", "PROCESSING", "COMPLETED"} for s in observed),
            observed,
        )
        total = len(self.valid_payload()["recipients"])
        self.r.check("total_recipients is correct", body["total_recipients"] == total, body["total_recipients"])
        self.r.check("successful_count == total", body["successful_count"] == total, body["successful_count"])
        self.r.check("failed_count == 0", body["failed_count"] == 0, body["failed_count"])
        self.r.check("pending_count == 0", body.get("pending_count") == 0, body.get("pending_count"))
        self.r.check("progress_percentage == 100.0", body["progress_percentage"] == 100.0, body["progress_percentage"])
        certs = body.get("certificates", [])
        self.r.check("status lists one entry per recipient", len(certs) == total, len(certs))
        self.r.check(
            "every certificate is SUCCESS with a download_url",
            all(c["status"] == "SUCCESS" and c["download_url"] for c in certs),
            [c["status"] for c in certs],
        )
        self.r.check("internal file_path is not exposed", all("file_path" not in c for c in certs))
        ada = next((c for c in certs if c["recipient_name"] == "Ada Lovelace"), {})
        self.r.check("custom meta is preserved", ada.get("meta") == {"grade": "A"}, ada.get("meta"))

        r = self.client.get(f"{API}/jobs/{uuid.uuid4()}")
        self.r.check("unknown job_id → 404", r.status_code == 404, r.status_code)
        r = self.client.get(f"{API}/jobs/not-a-uuid")
        self.r.check("malformed job_id → 422", r.status_code == 422, r.status_code)

    # -- 5 -----------------------------------------------------------------
    def partial_failure(self) -> None:
        payload = self.partial_payload()
        r = self.client.post(f"{API}/jobs/", json=payload)
        self.r.check(
            "job with render-hostile data still passes request validation (202)",
            r.status_code == 202,
            r.status_code,
        )
        if r.status_code != 202:
            return
        self.partial_job_id = r.json()["job_id"]
        body, observed = poll_job(self.client, self.partial_job_id, self.timeout)
        self.r.info(f"observed status sequence: {' → '.join(observed)}")
        if not self.r.check("job reaches a terminal state", body is not None and body["status"] in TERMINAL_JOB_STATES):
            return
        self.partial_job = body
        self.r.check("job status is PARTIALLY_FAILED", body["status"] == "PARTIALLY_FAILED", body["status"])
        self.r.check("successful_count == 3 (valid recipients unaffected)", body["successful_count"] == 3, body["successful_count"])
        self.r.check("failed_count == 2", body["failed_count"] == 2, body["failed_count"])
        self.r.check(
            "counters add up to total_recipients",
            body["successful_count"] + body["failed_count"] == body["total_recipients"] == 5,
        )
        self.r.check("progress_percentage == 100.0", body["progress_percentage"] == 100.0, body["progress_percentage"])

        by_email = {c["recipient_email"]: c for c in body["certificates"]}
        cjk = by_email.get(f"cjk.{self.run_tag}@example.com", {})
        long_ = by_email.get(f"long.{self.run_tag}@example.com", {})
        self.r.check("unsupported-glyph recipient is FAILED", cjk.get("status") == "FAILED", cjk.get("status"))
        self.r.check(
            "…with a readable error_message",
            "not supported by the certificate font" in (cjk.get("error_message") or ""),
            cjk.get("error_message"),
        )
        self.r.check("oversized-name recipient is FAILED", long_.get("status") == "FAILED", long_.get("status"))
        self.r.check("…with a readable error_message", "too long" in (long_.get("error_message") or ""), long_.get("error_message"))
        self.r.check(
            "failed certificates have no download_url",
            cjk.get("download_url") is None and long_.get("download_url") is None,
        )
        ok = [c for c in body["certificates"] if c["recipient_name"].startswith("Valid Person")]
        self.r.check(
            "all 3 valid recipients are SUCCESS",
            len(ok) == 3 and all(c["status"] == "SUCCESS" for c in ok),
            [c["status"] for c in ok],
        )

        r = self.client.get(f"{API}/jobs/{self.partial_job_id}/certificates", params={"status": "FAILED"})
        self.r.check(
            "?status=FAILED filter returns exactly the 2 failures",
            r.status_code == 200 and r.json()["total"] == 2,
            r.json().get("total") if r.status_code == 200 else r.status_code,
        )

        if cjk.get("id"):
            r = self.client.get(f"{API}/certificates/{cjk['id']}/download")
            detail = r.json().get("detail", {}) if r.status_code == 409 else {}
            self.r.check("downloading a FAILED certificate → 409 Conflict", r.status_code == 409, r.status_code)
            self.r.check(
                "409 body explains why (status + error_message)",
                detail.get("status") == "FAILED" and bool(detail.get("error_message")),
                detail,
            )

    # -- 6 -----------------------------------------------------------------
    def retrieval_and_download(self) -> None:
        if not self.valid_job_id:
            self.r.skip("certificate retrieval & download", "no job was created in step 2")
            return
        job_id = self.valid_job_id

        r = self.client.get(f"{API}/jobs/{job_id}/certificates")
        self.r.check("GET /jobs/{id}/certificates returns 200", r.status_code == 200, r.status_code)
        listing = r.json()
        items = listing.get("items", [])
        total = len(self.valid_payload()["recipients"])
        self.r.check("listing total and items match recipients", listing.get("total") == total == len(items), listing.get("total"))

        r = self.client.get(f"{API}/jobs/{job_id}/certificates", params={"skip": 1, "limit": 2})
        page = r.json()
        self.r.check(
            "pagination (skip=1, limit=2) returns 2 items, full total",
            r.status_code == 200 and len(page["items"]) == 2 and page["total"] == total,
        )
        r = self.client.get(f"{API}/jobs/{job_id}/certificates", params={"limit": 0})
        self.r.check("invalid pagination (limit=0) → 422", r.status_code == 422, r.status_code)
        r = self.client.get(f"{API}/jobs/{uuid.uuid4()}/certificates")
        self.r.check("listing for unknown job → 404", r.status_code == 404, r.status_code)

        if not items:
            return
        first = items[0]
        r = self.client.get(f"{API}/certificates/{first['id']}")
        detail = r.json() if r.status_code == 200 else {}
        self.r.check("GET /certificates/{id} returns metadata", r.status_code == 200 and detail.get("id") == first["id"], r.status_code)
        self.r.check("certificate belongs to the job", detail.get("job_id") == job_id)
        self.r.check("unknown certificate → 404", self.client.get(f"{API}/certificates/{uuid.uuid4()}").status_code == 404)

        for cert in items:
            url = cert.get("download_url") or f"{API}/certificates/{cert['id']}/download"
            # Read the body in chunks to exercise the streaming response.
            with self.client.stream("GET", url) as resp:
                chunks = [c for c in resp.iter_bytes()]
                content = b"".join(chunks)
                ok = resp.status_code == 200
                ctype = resp.headers.get("content-type", "")
                disp = resp.headers.get("content-disposition", "")
                clen = resp.headers.get("content-length")
            name = cert["recipient_name"]
            if not self.r.check(f"download {name!r} → 200", ok, resp.status_code):
                continue
            self.downloaded[cert["id"]] = content
            self.r.check("  content-type is application/pdf", ctype == "application/pdf", ctype)
            self.r.check("  content-disposition is an attachment .pdf", "attachment" in disp and ".pdf" in disp, disp)
            self.r.check(
                "  body is a complete PDF (%PDF- … %%EOF)",
                content.startswith(b"%PDF-") and content.rstrip().endswith(b"%%EOF"),
                f"{len(content)} bytes",
            )
            self.r.check("  content-length matches bytes received", clen is not None and int(clen) == len(content), clen)

        self.r.check(
            "unknown certificate download → 404",
            self.client.get(f"{API}/certificates/{uuid.uuid4()}/download").status_code == 404,
        )

    # -- 7 -----------------------------------------------------------------
    def filesystem(self) -> None:
        self.r.info(f"storage dir: {self.storage_dir}")
        if not self.r.check("storage directory exists", self.storage_dir.is_dir(), self.storage_dir):
            self.r.warn("If the server runs in Docker or on another host, pass --storage-dir or use --skip-fs.")
            return

        for label, job in (("valid job", self.valid_job), ("partial-failure job", self.partial_job)):
            if not job:
                self.r.skip(f"files for {label}", "job not available")
                continue
            job_dir = self.storage_dir / job["id"]
            self.r.check(f"{label}: folder storage/certificates/<job_id>/ exists", job_dir.is_dir(), job_dir.name)
            for cert in job["certificates"]:
                pdf = job_dir / f"{cert['id']}.pdf"
                short = f"{cert['recipient_name'][:24]!r}"
                if cert["status"] == "SUCCESS":
                    exists = pdf.is_file()
                    self.r.check(f"{label}: {short} → <cert_id>.pdf on disk", exists, pdf.name)
                    if exists:
                        data = pdf.read_bytes()
                        self.r.check("  file is a valid PDF", data.startswith(b"%PDF-"), f"{len(data)} bytes")
                        if cert["id"] in self.downloaded:
                            self.r.check(
                                "  file on disk is byte-identical to the download",
                                data == self.downloaded[cert["id"]],
                            )
                else:
                    self.r.check(f"{label}: {short} (FAILED) left no file behind", not pdf.exists(), pdf.name)
            leftovers = [p.name for p in job_dir.glob(".*.tmp")] if job_dir.is_dir() else []
            self.r.check(f"{label}: no temp files left by atomic writes", not leftovers, leftovers)

    # -- runner ------------------------------------------------------------
    def run(self, skip_fs: bool) -> None:
        run_section(self.r, "1. Health / Reachability", self.health)
        run_section(self.r, "2. Bulk Job Creation  (POST /api/v1/jobs/)", self.create_job)
        run_section(self.r, "3. Input Validation", self.validation)
        run_section(self.r, "4. Job Progress Tracking  (GET /api/v1/jobs/{job_id})", self.progress)
        run_section(self.r, "5. Partial Failure Resilience", self.partial_failure)
        run_section(self.r, "6. Certificate Retrieval & Download", self.retrieval_and_download)
        if skip_fs:
            self.r.section("7. File System Verification")
            self.r.skip("file system checks", "--skip-fs given")
        else:
            run_section(self.r, "7. File System Verification", self.filesystem)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a running Bulk Certificate Generator API.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--storage-dir", default=None, help="Server's STORAGE_DIR (default: resolved like the server does)")
    parser.add_argument("--timeout", type=float, default=60.0, help="Seconds to wait for a job to finish")
    parser.add_argument("--skip-fs", action="store_true", help="Skip disk checks (server on another host/container)")
    args = parser.parse_args()

    report = Report()
    print(BOLD("Bulk Certificate Generator: System Verification"))
    print(DIM(f"target: {args.base_url}"))
    storage_dir = resolve_storage_dir(args.storage_dir, report)

    verifier = SystemVerifier(args.base_url, storage_dir, args.timeout, report)
    started = time.monotonic()
    try:
        verifier.run(args.skip_fs)
    except SystemExit:
        pass
    finally:
        verifier.client.close()

    # ---- summary
    print(f"\n{BOLD('━━ Summary')}")
    for title, ok in report.section_results.items():
        print(f"  {GREEN('✅ PASS') if ok else RED('❌ FAIL')}  {title}")
    total = report.passed + report.failed
    line = f"{report.passed}/{total} checks passed, {report.failed} failed, {report.skipped} skipped in {time.monotonic() - started:.1f}s"
    print("\n  " + (GREEN(BOLD("ALL CHECKS PASSED  " + line)) if report.failed == 0 else RED(BOLD(line))))
    if report.failures:
        print(f"\n{BOLD(RED('Failures:'))}")
        for f in report.failures:
            print(f"  • {f}")
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
