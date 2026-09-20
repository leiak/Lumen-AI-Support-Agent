"""PDF processor tests — use real PDF fixtures from tests/fixtures/."""
from pathlib import Path

import pytest

from knowledge.multimodal.pdf_processor import (
    ExtractedPdf,
    PdfProcessor,
)


@pytest.fixture
def sample_text_pdf():
    """PDF with only text, no images."""
    # tests/knowledge/multimodal/unit/test_pdf_processor.py
    #   → tests/  (4 .parent calls up to the tests/ dir, then /fixtures)
    return Path(__file__).parents[3] / "fixtures" / "sample_text.pdf"


@pytest.fixture
def sample_pdf_with_table():
    """PDF with a table on page 2 (key-page heuristic, table branch)."""
    return Path(__file__).parents[3] / "fixtures" / "sample_with_table.pdf"


@pytest.fixture
def sample_pdf_with_image():
    """PDF with an embedded image on page 2 (key-page heuristic, image branch)."""
    return Path(__file__).parents[3] / "fixtures" / "sample_with_image.pdf"


def test_extract_text_from_text_only_pdf(sample_text_pdf):
    """Pure-text PDF returns text chunks, no screenshots."""
    processor = PdfProcessor()
    result = processor.extract(sample_text_pdf.read_bytes())

    assert isinstance(result, ExtractedPdf)
    assert len(result.text_chunks) > 0
    assert all(isinstance(c, str) for c in result.text_chunks)
    # At least one chunk should mention the password reset topic
    combined = " ".join(result.text_chunks).lower()
    assert ("password" in combined) or ("reset" in combined)
    assert result.screenshot_pages == []  # No images/tables → no screenshots


def test_extract_detects_table_pages(sample_pdf_with_table):
    """Page with table → included in screenshot_pages."""
    processor = PdfProcessor()
    result = processor.extract(sample_pdf_with_table.read_bytes())

    assert len(result.screenshot_pages) >= 1
    page_num, png_bytes = result.screenshot_pages[0]
    assert isinstance(page_num, int)
    assert isinstance(png_bytes, bytes)
    assert png_bytes.startswith(b"\x89PNG")  # PNG magic bytes


def test_extract_detects_image_pages(sample_pdf_with_image):
    """Page with embedded image → included in screenshot_pages."""
    processor = PdfProcessor()
    result = processor.extract(sample_pdf_with_image.read_bytes())

    assert len(result.screenshot_pages) >= 1
    page_num, png_bytes = result.screenshot_pages[0]
    assert isinstance(page_num, int)
    assert isinstance(png_bytes, bytes)
    assert png_bytes.startswith(b"\x89PNG")  # PNG magic bytes


def test_extract_handles_blank_pdf():
    """Empty / blank PDF doesn't crash."""
    blank_pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >> endobj\n"
        b"xref\n0 4\n0000000000 65535 f\n"
        b"0000000009 00000 n\n0000000058 00000 n\n0000000105 00000 n\n"
        b"trailer << /Size 4 /Root 1 0 R >>\nstartxref\n164\n%%EOF"
    )
    processor = PdfProcessor()
    result = processor.extract(blank_pdf)
    assert result.text_chunks == []
    assert result.screenshot_pages == []


def test_extract_chunks_text_by_page(sample_text_pdf):
    """3-page PDF → at least 3 chunks (one per page when text fits chunk_size)."""
    processor = PdfProcessor(chunk_size=500)  # small for testing
    result = processor.extract(sample_text_pdf.read_bytes())
    # 3-page PDF with ~200 chars per page → ≥ 3 chunks (one per page)
    assert len(result.text_chunks) >= 3
