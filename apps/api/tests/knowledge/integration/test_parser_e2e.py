"""End-to-end integration tests for ``knowledge.parser``.

This directory is reserved for live, ``@pytest.mark.integration`` tests
that exercise the parser against real-world inputs the unit-test
fixtures can't cover (multi-page PDFs with complex layouts, real-world
Markdown with embedded HTML, encoded-JSON edge cases, etc.).

M1 status: SKIPPED.

The unit-test suite in ``tests/knowledge/test_parser.py`` already
covers the full async dispatch path against a hand-crafted valid PDF
(``test_parse_pdf_extracts_text_from_pages``), which validates
``pypdf.PdfReader`` + ``extract_text`` end-to-end. Adding a separate
integration test with a downloaded real-world PDF would be redundant
for M1 and would require shipping a binary fixture in git.

When this gets un-skipped, drop a small PDF under
``tests/knowledge/integration/fixtures/`` and add:

    @pytest.mark.integration
    async def test_parse_real_pdf_extracts_text() -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "small.pdf"
        file_bytes = fixture_path.read_bytes()
        result = await parse_document(
            file_bytes=file_bytes,
            file_name="small.pdf",
        )
        assert result.format == "pdf"
        assert result.metadata["page_count"] >= 1
        assert len(result.text) > 0
"""
from __future__ import annotations

import pytest


@pytest.mark.integration
def test_parser_e2e_placeholder() -> None:
    """Placeholder so the integration file stays discoverable.

    Skipped intentionally for M1 — see module docstring for the
    rationale. When the real e2e test lands, replace this with the
    ``test_parse_real_pdf_extracts_text`` shown in the docstring.
    """
    pytest.skip("M1: e2e PDF parsing covered by unit test; see module docstring")
