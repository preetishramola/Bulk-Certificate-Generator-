import io
from datetime import date

import pytest
from pypdf import PdfReader

from app.renderer import CertificateData, RenderError, render_certificate
from tests.conftest import pdf_text


def make_data(**overrides) -> CertificateData:
    values = dict(
        certificate_id="cert-123",
        recipient_name="Ada Lovelace",
        course_name="Python for Data Engineering",
        issuer_name="Acme Academy",
        issue_date=date(2026, 9, 30),
        grade="A+",
    )
    values.update(overrides)
    return CertificateData(**values)


def test_renders_single_page_pdf_with_recipient_specific_content():
    content = render_certificate(make_data())

    assert content.startswith(b"%PDF")
    assert len(PdfReader(io.BytesIO(content)).pages) == 1
    text = pdf_text(content)
    for expected in ("Ada Lovelace", "Python for Data Engineering", "Acme Academy", "30 September 2026", "A+", "cert-123"):
        assert expected in text


def test_grade_line_is_optional():
    text = pdf_text(render_certificate(make_data(grade=None)))
    assert "grade" not in text


def test_very_long_name_still_renders():
    long_name = "Maximiliana " * 10
    text = pdf_text(render_certificate(make_data(recipient_name=long_name.strip())))
    assert "Maximiliana" in text


def test_unrenderable_characters_raise():
    with pytest.raises(RenderError, match="recipient_name"):
        render_certificate(make_data(recipient_name="李雷"))
