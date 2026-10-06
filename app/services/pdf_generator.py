"""Certificate PDF rendering with ReportLab.

Why ReportLab rather than an HTML-to-PDF tool (WeasyPrint/wkhtmltopdf):
it is pure Python with no system libraries (Cairo/Pango/a headless browser).
The Docker image stays small, renders are fast and deterministic, and it
installs the same way on Windows, macOS and Linux.

The renderer is a pure function (inputs -> file on disk) with no DB access.
This makes it trivial to unit-test and to reuse from a Celery worker.
"""

import json
import logging
import os
import uuid
from dataclasses import dataclass, fields
from datetime import date
from functools import lru_cache
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

logger = logging.getLogger(__name__)

PAGE_SIZE = landscape(A4)  # 842 x 595 pt

# ReportLab's built-in Type 1 fonts only cover the WinAnsi (cp1252) character set.
# Anything outside it would print as blank/garbled glyphs. We reject it instead,
# so nobody receives a certificate with an unreadable name.
BUILTIN_FONT_ENCODING = "cp1252"


class CertificateRenderError(ValueError):
    """Raised for per-recipient data that cannot be rendered.

    The bulk processor catches this, marks only that certificate as FAILED,
    and keeps going.
    """


@dataclass(frozen=True)
class CertificateTheme:
    primary_color: str = "#1F3A5F"
    accent_color: str = "#C9A227"
    text_color: str = "#2E2E2E"
    muted_color: str = "#6B7280"
    background_color: str = "#FFFDF7"
    title_font: str = "Helvetica-Bold"
    name_font: str = "Times-BoldItalic"
    body_font: str = "Helvetica"
    title_text: str = "CERTIFICATE OF COMPLETION"
    name_max_font_size: int = 44
    name_min_font_size: int = 22


@lru_cache(maxsize=8)
def load_theme(path: Path | None) -> CertificateTheme:
    """Load the theme JSON, falling back to defaults.

    A missing or broken theme file must never take down certificate
    generation, so we log it and use the defaults.
    """
    if path is None or not path.is_file():
        return CertificateTheme()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        allowed = {f.name for f in fields(CertificateTheme)}
        return CertificateTheme(**{k: v for k, v in raw.items() if k in allowed})
    except (OSError, ValueError, TypeError):
        logger.warning("Invalid certificate layout at %s; using defaults", path, exc_info=True)
        return CertificateTheme()


def _ensure_renderable(label: str, value: str) -> None:
    try:
        value.encode(BUILTIN_FONT_ENCODING)
    except UnicodeEncodeError as exc:
        bad = value[exc.start : exc.end]
        raise CertificateRenderError(
            f"{label} contains characters not supported by the certificate font: {bad!r}"
        ) from exc


def _fit_font_size(text: str, font: str, max_size: float, min_size: float, max_width: float) -> float:
    """Pick the largest font size (down to min_size) at which `text` fits."""
    size = max_size
    while size > min_size and stringWidth(text, font, size) > max_width:
        size -= 1
    if stringWidth(text, font, size) > max_width:
        raise CertificateRenderError(f"Text is too long to fit on the certificate: {text[:40]!r}...")
    return size


def _draw_border(c: canvas.Canvas, theme: CertificateTheme, width: float, height: float) -> None:
    primary, accent = colors.HexColor(theme.primary_color), colors.HexColor(theme.accent_color)

    # Background
    c.setFillColor(colors.HexColor(theme.background_color))
    c.rect(0, 0, width, height, stroke=0, fill=1)

    # Outer thick frame, then inner thin frame: a classic double border.
    c.setStrokeColor(primary)
    c.setLineWidth(6)
    c.rect(20, 20, width - 40, height - 40, stroke=1, fill=0)
    c.setStrokeColor(accent)
    c.setLineWidth(1.5)
    c.rect(32, 32, width - 64, height - 64, stroke=1, fill=0)

    # Corner ornaments: small filled squares, rotated 45 degrees into diamonds.
    c.setFillColor(accent)
    for x, y in ((32, 32), (width - 32, 32), (32, height - 32), (width - 32, height - 32)):
        c.saveState()
        c.translate(x, y)
        c.rotate(45)
        c.rect(-6, -6, 12, 12, stroke=0, fill=1)
        c.restoreState()


def render_certificate(
    c: canvas.Canvas,
    *,
    recipient_name: str,
    course_name: str,
    issue_date: date,
    certificate_id: uuid.UUID,
    program_title: str | None,
    theme: CertificateTheme,
) -> None:
    """Draw one certificate page onto `c`."""
    width, height = PAGE_SIZE
    cx = width / 2
    content_width = width - 160
    primary = colors.HexColor(theme.primary_color)
    accent = colors.HexColor(theme.accent_color)
    text = colors.HexColor(theme.text_color)
    muted = colors.HexColor(theme.muted_color)

    _draw_border(c, theme, width, height)

    # Title
    c.setFillColor(primary)
    c.setFont(theme.title_font, 34)
    c.drawCentredString(cx, height - 120, theme.title_text)

    # Accent rule under the title
    c.setStrokeColor(accent)
    c.setLineWidth(2)
    c.line(cx - 120, height - 138, cx + 120, height - 138)

    if program_title:
        c.setFillColor(muted)
        c.setFont(theme.body_font, 13)
        c.drawCentredString(cx, height - 162, program_title)

    c.setFillColor(text)
    c.setFont(theme.body_font, 15)
    c.drawCentredString(cx, height - 210, "This is to certify that")

    # Recipient name: the visual focal point. Shrink to fit long names.
    name_size = _fit_font_size(
        recipient_name, theme.name_font, theme.name_max_font_size, theme.name_min_font_size, content_width
    )
    c.setFillColor(primary)
    c.setFont(theme.name_font, name_size)
    c.drawCentredString(cx, height - 268, recipient_name)

    c.setStrokeColor(muted)
    c.setLineWidth(0.75)
    c.line(cx - 220, height - 282, cx + 220, height - 282)

    c.setFillColor(text)
    c.setFont(theme.body_font, 15)
    c.drawCentredString(cx, height - 318, "has successfully completed the course")

    course_size = _fit_font_size(course_name, theme.title_font, 24, 14, content_width)
    c.setFillColor(primary)
    c.setFont(theme.title_font, course_size)
    c.drawCentredString(cx, height - 355, course_name)

    # Date (left) and signature line (right)
    c.setFillColor(text)
    c.setFont(theme.body_font, 12)
    date_x, sign_x, line_y = 220, width - 220, 140
    c.drawCentredString(date_x, line_y + 8, issue_date.strftime("%B %d, %Y"))
    c.setStrokeColor(muted)
    c.line(date_x - 90, line_y, date_x + 90, line_y)
    c.line(sign_x - 90, line_y, sign_x + 90, line_y)
    c.setFillColor(muted)
    c.setFont(theme.body_font, 10)
    c.drawCentredString(date_x, line_y - 14, "Date of Issue")
    c.drawCentredString(sign_x, line_y - 14, "Authorized Signature")

    # Footer with the unique certificate ID, so the certificate can be verified.
    c.setFont(theme.body_font, 9)
    c.drawCentredString(cx, 50, f"Certificate ID: {certificate_id}")


def generate_certificate_pdf(
    *,
    output_path: Path,
    recipient_name: str,
    course_name: str,
    issue_date: date,
    certificate_id: uuid.UUID,
    program_title: str | None = None,
    theme_path: Path | None = None,
) -> Path:
    """Render a certificate PDF to `output_path` and return that path.

    Writes are atomic: we render to a temp file and `os.replace` it into place.
    A crash mid-render therefore never leaves a truncated PDF at the final
    path, which the download endpoint would otherwise serve.

    Raises:
        CertificateRenderError: the recipient data can't be rendered.
        OSError: the file system failed (disk full, permissions, ...).
    """
    _ensure_renderable("Recipient name", recipient_name)
    _ensure_renderable("Course name", course_name)
    if program_title:
        _ensure_renderable("Program title", program_title)

    theme = load_theme(theme_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(f".{output_path.name}.tmp")

    try:
        c = canvas.Canvas(str(tmp_path), pagesize=PAGE_SIZE)
        c.setTitle(f"Certificate - {recipient_name}")
        c.setAuthor("Bulk Certificate Generator")
        c.setSubject(course_name)
        render_certificate(
            c,
            recipient_name=recipient_name,
            course_name=course_name,
            issue_date=issue_date,
            certificate_id=certificate_id,
            program_title=program_title,
            theme=theme,
        )
        c.showPage()
        c.save()
        os.replace(tmp_path, output_path)
    finally:
        # Clean up the temp file if anything above failed.
        tmp_path.unlink(missing_ok=True)

    return output_path
