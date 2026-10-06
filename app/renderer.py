"""The single, predefined certificate template, rendered as a one-page PDF."""

import io
import unicodedata
from dataclasses import dataclass
from datetime import date

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

PAGE_WIDTH, PAGE_HEIGHT = landscape(A4)
CENTER_X = PAGE_WIDTH / 2
CONTENT_WIDTH = PAGE_WIDTH - 2 * 90  # usable width inside the decorative border

NAVY = colors.HexColor("#1F2A44")
GOLD = colors.HexColor("#B8892B")
CREAM = colors.HexColor("#FBF7EE")
GREY = colors.HexColor("#6B6B6B")

# ReportLab's built-in fonts use the WinAnsi (cp1252) encoding, so text outside
# that character set cannot be drawn. Validation uses this to reject such input
# up front rather than producing a certificate with missing glyphs.
TEMPLATE_ENCODING = "cp1252"


class RenderError(Exception):
    pass


@dataclass(frozen=True)
class CertificateData:
    certificate_id: str
    recipient_name: str
    course_name: str
    issuer_name: str
    issue_date: date
    grade: str | None = None


def unsupported_characters(text: str) -> list[str]:
    """Characters in `text` that the template fonts cannot render."""
    bad = []
    for ch in text:
        if unicodedata.category(ch).startswith("C"):  # control / format chars
            bad.append(ch)
            continue
        try:
            ch.encode(TEMPLATE_ENCODING)
        except UnicodeEncodeError:
            bad.append(ch)
    return sorted(set(bad))


def render_certificate(data: CertificateData) -> bytes:
    """Render a certificate and return the PDF bytes."""
    for field_name in ("recipient_name", "course_name", "issuer_name", "grade"):
        value = getattr(data, field_name)
        if value and (bad := unsupported_characters(value)):
            raise RenderError(f"{field_name} contains characters the template cannot render: {bad!r}")

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(PAGE_WIDTH, PAGE_HEIGHT))
    pdf.setTitle(f"Certificate - {data.recipient_name}")
    pdf.setAuthor(data.issuer_name)
    pdf.setSubject(data.course_name)

    _draw_frame(pdf)

    pdf.setFillColor(NAVY)
    pdf.setFont("Helvetica-Bold", 38)
    pdf.drawCentredString(CENTER_X, 470, "CERTIFICATE")
    pdf.setFillColor(GOLD)
    pdf.setFont("Helvetica", 14)
    pdf.drawCentredString(CENTER_X, 448, "O F   C O M P L E T I O N")

    pdf.setFillColor(GREY)
    pdf.setFont("Times-Italic", 16)
    pdf.drawCentredString(CENTER_X, 395, "This is to certify that")

    pdf.setFillColor(NAVY)
    _draw_fitted(pdf, data.recipient_name, "Times-BoldItalic", 40, 345)
    pdf.setStrokeColor(GOLD)
    pdf.setLineWidth(1)
    pdf.line(CENTER_X - 220, 332, CENTER_X + 220, 332)

    pdf.setFillColor(GREY)
    pdf.setFont("Times-Italic", 16)
    pdf.drawCentredString(CENTER_X, 298, "has successfully completed")

    pdf.setFillColor(NAVY)
    _draw_fitted(pdf, data.course_name, "Helvetica-Bold", 24, 262)

    if data.grade:
        pdf.setFillColor(GREY)
        pdf.setFont("Times-Italic", 14)
        pdf.drawCentredString(CENTER_X, 230, f"with grade: {data.grade}")

    _draw_signature_block(pdf, CENTER_X - 200, data.issue_date.strftime("%d %B %Y"), "Date of Issue")
    _draw_signature_block(pdf, CENTER_X + 200, data.issuer_name, "Issued By")

    pdf.setFillColor(GREY)
    pdf.setFont("Helvetica", 8)
    pdf.drawCentredString(CENTER_X, 58, f"Certificate ID: {data.certificate_id}")

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _draw_frame(pdf: canvas.Canvas) -> None:
    pdf.setFillColor(CREAM)
    pdf.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, stroke=0, fill=1)
    pdf.setStrokeColor(NAVY)
    pdf.setLineWidth(6)
    pdf.rect(28, 28, PAGE_WIDTH - 56, PAGE_HEIGHT - 56)
    pdf.setStrokeColor(GOLD)
    pdf.setLineWidth(1.5)
    pdf.rect(40, 40, PAGE_WIDTH - 80, PAGE_HEIGHT - 80)


def _draw_fitted(pdf: canvas.Canvas, text: str, font: str, max_size: float, y: float) -> None:
    """Draw centred text, shrinking the font so long values stay inside the border."""
    width_at_max = stringWidth(text, font, max_size)
    size = max_size if width_at_max <= CONTENT_WIDTH else max_size * CONTENT_WIDTH / width_at_max
    pdf.setFont(font, size)
    pdf.drawCentredString(CENTER_X, y, text)


def _draw_signature_block(pdf: canvas.Canvas, x: float, value: str, label: str) -> None:
    pdf.setFillColor(NAVY)
    font, size = "Helvetica-Bold", 13
    while stringWidth(value, font, size) > 220 and size > 7:
        size -= 0.5
    pdf.setFont(font, size)
    pdf.drawCentredString(x, 128, value)
    pdf.setStrokeColor(NAVY)
    pdf.setLineWidth(0.8)
    pdf.line(x - 115, 120, x + 115, 120)
    pdf.setFillColor(GREY)
    pdf.setFont("Helvetica", 10)
    pdf.drawCentredString(x, 105, label)
