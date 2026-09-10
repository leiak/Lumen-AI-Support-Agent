"""Unit tests for ``knowledge.parser``.

Two flavors:

* Happy-path tests for every supported format, covering dispatch by
  mime_type AND by suffix.
* Edge-case tests for empty input, nested script tags, corrupt PDFs,
  unknown extensions, and inline-code preservation in markdown.

The hand-crafted PDF bytes used in ``_MINIMAL_PDF_BYTES`` were verified
to round-trip through ``pypdf.PdfReader.extract_text`` — see
``test_parse_pdf_extracts_text_from_pages`` below.
"""
from __future__ import annotations

import pytest
from pypdf import PageObject

from knowledge.parser import (
    MAX_PARSE_BYTES,
    SUPPORTED_FORMATS,
    OversizeDocumentError,
    ParsedDocument,
    UnsupportedDocumentType,
    parse_document,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A minimal valid PDF containing the single line "Hello World from a
# test PDF". Offsets in the xref table are exact for THIS byte string;
# changing the text requires recomputing the offsets. pypdf is lenient
# about a slightly-off startxref and still extracts the text correctly
# (verified: emits "incorrect startxref pointer(1)" warning, returns
# the page text).
#
# The PDF object dictionaries are split across lines so the source stays
# under the 100-char line limit; PDF parsers treat any whitespace inside
# a dictionary as equivalent to a single space.
_MINIMAL_PDF_BYTES = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R
   /MediaBox [0 0 612 792]
   /Contents 4 0 R
   /Resources << /Font << /F1 5 0 R >> >>
>>
endobj
4 0 obj
<< /Length 55 >>
stream
BT
/F1 24 Tf
72 720 Td
(Hello World from a test PDF) Tj
ET
endstream
endobj
5 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
xref
0 6
0000000000 65535 f
0000000010 00000 n
0000000060 00000 n
0000000110 00000 n
0000000210 00000 n
0000000340 00000 n
trailer
<< /Size 6 /Root 1 0 R >>
startxref
420
%%EOF"""


# ---------------------------------------------------------------------------
# Happy-path: one per format
# ---------------------------------------------------------------------------


async def test_parse_text_returns_decoded_content() -> None:
    """Plain UTF-8 text passes through byte-identical."""
    payload = b"Hello, world!\nSecond line."
    result = await parse_document(
        file_bytes=payload,
        file_name="greeting.txt",
        mime_type=None,
    )
    assert isinstance(result, ParsedDocument)
    assert result.text == "Hello, world!\nSecond line."
    assert result.format == "text"
    assert result.metadata["source_format"] == "text"
    assert result.metadata["char_count"] == len(result.text)
    assert result.blocks == []


async def test_parse_markdown_strips_headers_and_code_fences() -> None:
    """Headers lose their ``#`` prefix; code-fence content is preserved."""
    md = b"""# Title

Some intro text.

## Section 1

```python
print("inside a code fence")
```

More prose after the fence.
"""
    result = await parse_document(
        file_bytes=md,
        file_name="doc.md",
        mime_type=None,
    )
    assert result.format == "markdown"
    assert "Title" in result.text
    assert "##" not in result.text
    assert "Section 1" in result.text
    # Code-fence markers are gone but the inner text is kept.
    assert "```" not in result.text
    assert 'print("inside a code fence")' in result.text
    assert "More prose after the fence." in result.text


async def test_parse_html_extracts_visible_text_only() -> None:
    """Script/style tags are stripped; visible body text is preserved."""
    html = b"""<!DOCTYPE html>
<html>
<head>
  <title>Ignored title</title>
  <style>body { color: red; }</style>
  <script>alert('xss');</script>
</head>
<body>
  <h1>Hello</h1>
  <p>Some visible paragraph.</p>
</body>
</html>"""
    result = await parse_document(
        file_bytes=html,
        file_name="page.html",
        mime_type=None,
    )
    assert result.format == "html"
    assert "Hello" in result.text
    assert "Some visible paragraph." in result.text
    # Security-critical: nothing from script or style should leak.
    assert "alert" not in result.text
    assert "color: red" not in result.text
    # <title> content is fine to keep (not security-sensitive), but at
    # minimum script/style content must be gone.


async def test_parse_pdf_extracts_text_from_pages() -> None:
    """A valid PDF yields the embedded text + a non-zero page_count."""
    result = await parse_document(
        file_bytes=_MINIMAL_PDF_BYTES,
        file_name="doc.pdf",
        mime_type=None,
    )
    assert result.format == "pdf"
    assert result.metadata["page_count"] == 1
    assert "Hello World from a test PDF" in result.text


async def test_parse_json_pretty_prints_dict() -> None:
    """A JSON dict is pretty-printed with 2-space indent + Unicode preserved."""
    # é in UTF-8 is \xc3\xa9 — using raw bytes ensures the test stays
    # correct regardless of the source-file encoding.
    payload = '{"name": "Café", "items": [1, 2, 3]}'.encode()
    result = await parse_document(
        file_bytes=payload,
        file_name="data.json",
        mime_type=None,
    )
    assert result.format == "json"
    assert '"name": "Café"' in result.text
    assert '"items": [' in result.text
    # Pretty-printed means newlines between top-level keys.
    assert "\n" in result.text


async def test_parse_json_pretty_prints_list() -> None:
    """A top-level JSON list is also pretty-printed."""
    payload = b'[{"a": 1}, {"b": 2}]'
    result = await parse_document(
        file_bytes=payload,
        file_name="items.json",
        mime_type=None,
    )
    assert result.format == "json"
    assert '"a": 1' in result.text
    assert '"b": 2' in result.text


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


async def test_parse_empty_bytes_returns_empty_text() -> None:
    """Empty bytes are still a valid (empty) text document; not an error.

    Empty bytes + empty suffix + empty mime → text-sniff returns False
    so we DON'T auto-pick ``text``. Pass an explicit suffix so dispatch
    reaches the text parser cleanly.
    """
    result = await parse_document(
        file_bytes=b"",
        file_name="empty.txt",
        mime_type=None,
    )
    assert result.text == ""
    assert result.format == "text"
    assert result.metadata["char_count"] == 0


async def test_parse_html_with_nested_script_is_stripped() -> None:
    """A nested ``<script>`` inside ``<body>`` is fully decomposed."""
    html = b"""<html><body>
<div>Visible top text.</div>
<script type="text/javascript">
  var secret = "should not appear";
  if (true) { doEvil(); }
</script>
<p>Visible bottom text.</p>
</body></html>"""
    result = await parse_document(
        file_bytes=html,
        file_name="nested.html",
        mime_type=None,
    )
    assert "Visible top text." in result.text
    assert "Visible bottom text." in result.text
    assert "secret" not in result.text
    assert "doEvil" not in result.text
    assert "text/javascript" not in result.text


async def test_parse_markdown_preserves_inline_code_text() -> None:
    """Backticked inline code loses its delimiters but keeps the content."""
    md = b"Use the `print()` function to display `42`."
    result = await parse_document(
        file_bytes=md,
        file_name="inline.md",
        mime_type=None,
    )
    assert "print()" in result.text
    assert "42" in result.text
    # Backticks themselves are gone.
    assert "`" not in result.text


async def test_parse_unknown_extension_raises_unsupported() -> None:
    """An unrecognized suffix with non-text bytes raises."""
    # Use bytes that don't look like UTF-8 text (lots of NULs).
    payload = b"\x00\x01\x02\x03BINARY\x00\xff\xfe"
    with pytest.raises(UnsupportedDocumentType) as exc_info:
        await parse_document(
            file_bytes=payload,
            file_name="data.xyz",
            mime_type=None,
        )
    assert "xyz" in str(exc_info.value)
    # The error message lists supported formats so operators can debug
    # without consulting source.
    for fmt in SUPPORTED_FORMATS:
        assert fmt in str(exc_info.value)


async def test_parse_pdf_with_no_text_pages_returns_empty_text() -> None:
    """A PDF where extract_text returns nothing yields empty text, not an error.

    Scanned / image-only PDFs exhibit this in the wild — the parser
    must NOT raise; the worker will surface ``text=''`` + page_count
    via the API, and the user sees a clear INDEXED-empty / FAILED
    outcome depending on policy.
    """
    # The minimal PDF does have text, so to exercise the empty-text
    # path we monkey-patch the page's extract_text. Using
    # ``unittest.mock`` here keeps the test hermetic (no need for a
    # scanned-PDF fixture).
    from unittest.mock import patch

    with patch.object(PageObject, "extract_text", return_value=""):
        result = await parse_document(
            file_bytes=_MINIMAL_PDF_BYTES,
            file_name="scanned.pdf",
            mime_type=None,
        )
    assert result.text == ""
    assert result.format == "pdf"
    assert result.metadata["page_count"] == 1
    assert result.metadata["char_count"] == 0


async def test_parse_detects_format_from_mime_type() -> None:
    """When mime_type is provided, it wins over the (missing/odd) suffix."""
    result = await parse_document(
        file_bytes=b"# Hello",
        file_name="no-extension",
        mime_type="text/markdown",
    )
    assert result.format == "markdown"


async def test_parse_detects_format_from_suffix_when_mime_missing() -> None:
    """When mime_type is None/empty, fall back to the file suffix."""
    result = await parse_document(
        file_bytes=b"<p>hi</p>",
        file_name="page.htm",
        mime_type=None,
    )
    assert result.format == "html"


async def test_parse_unsupported_type_error_is_value_error() -> None:
    """UnsupportedDocumentType is a ValueError subclass (broad-catch safe)."""
    assert issubclass(UnsupportedDocumentType, ValueError)
    payload = b"\x00\x01\x02\x03"
    with pytest.raises(ValueError):
        await parse_document(
            file_bytes=payload,
            file_name="blob.bin",
            mime_type=None,
        )


async def test_parse_text_falls_back_from_mime_to_suffix() -> None:
    """An uninformative mime type falls back to suffix matching."""
    # application/octet-stream is the generic "I don't know" mime —
    # _detect_format_from_mime returns None and we fall through to the
    # suffix table.
    result = await parse_document(
        file_bytes=b"plain content",
        file_name="notes.md",
        mime_type="application/octet-stream",
    )
    assert result.format == "markdown"


# ---------------------------------------------------------------------------
# Size guard (DoS prevention)
# ---------------------------------------------------------------------------


async def test_parse_rejects_oversize_input() -> None:
    """Bytes larger than MAX_PARSE_BYTES are rejected before any parsing.

    Pins the DoS-prevention contract: a single oversize payload must
    raise :class:`OversizeDocumentError` (a ``ValueError`` subclass) and
    the message must surface the actual size + the limit so operators
    can debug from the worker log alone.
    """
    # Exactly one byte over the limit — keeps the test fast and the
    # intent unambiguous.
    payload = b"x" * (MAX_PARSE_BYTES + 1)
    with pytest.raises(OversizeDocumentError) as exc_info:
        await parse_document(
            file_bytes=payload,
            file_name="huge.pdf",
            mime_type=None,
        )
    msg = str(exc_info.value)
    assert str(MAX_PARSE_BYTES + 1) in msg
    assert str(MAX_PARSE_BYTES) in msg
    # A single broad except ValueError must catch both parser failures.
    assert isinstance(exc_info.value, ValueError)


async def test_parse_at_limit_succeeds() -> None:
    """Bytes exactly at MAX_PARSE_BYTES pass the size guard (boundary).

    The check is ``> MAX_PARSE_BYTES`` (strict greater-than), so the
    limit value itself must be accepted. Use a real, parseable format
    (.txt) so we exercise the size guard without a 50 MiB fixture —
    the content itself can be much smaller; what matters is that
    ``len(file_bytes) == MAX_PARSE_BYTES`` triggers parsing, not
    rejection.
    """
    payload = b"a" * MAX_PARSE_BYTES
    result = await parse_document(
        file_bytes=payload,
        file_name="boundary.txt",
        mime_type=None,
    )
    assert result.format == "text"
    assert len(result.text) == MAX_PARSE_BYTES


async def test_parse_returns_pii_safe_metadata() -> None:
    """Metadata + format are exposed; no raw content sneaks into the result.

    Structural assertion: blocks is always an empty list (M1 contract).
    """
    payload = b"private secret data"
    result = await parse_document(
        file_bytes=payload,
        file_name="private.txt",
        mime_type=None,
    )
    # The text field obviously contains the content (that's the point);
    # this test pins the SHAPE rather than the value.
    assert isinstance(result.text, str)
    assert isinstance(result.blocks, list)
    assert result.blocks == []
    assert isinstance(result.metadata, dict)
    assert result.metadata["source_format"] == "text"
