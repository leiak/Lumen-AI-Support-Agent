"""Unit tests for ``knowledge.multimodal``.

Covers Task 6.4 (M1 multimodal handling):

* Fenced code blocks (Markdown) — with + without language, multiple,
  inline-code preservation, HTML-inside-code preservation.
* Markdown inline images — with + without alt.
* HTML ``<img>`` — with alt + src, with parent ``<figure>`` /
  ``<figcaption>``.
* Edge cases — non-multimodal formats return ``[]``; malformed HTML
  does not crash.
"""
from __future__ import annotations

from knowledge.multimodal import (
    enrich_blocks,
    strip_markdown_blocks,
)
from knowledge.parser import ParsedDocument

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parsed(text: str, fmt: str, **metadata: object) -> ParsedDocument:
    """Build a minimal ParsedDocument for the helpers.

    For HTML, callers that want ``enrich_blocks`` to actually parse the
    source must pass ``raw_source=<bytes>`` as a kwarg. Tests that only
    assert empty-block behavior or non-HTML formats can omit it.
    """
    return ParsedDocument(
        text=text,
        blocks=[],
        metadata={"source_format": fmt, **metadata},
        format=fmt,
    )


# ---------------------------------------------------------------------------
# Code blocks (markdown)
# ---------------------------------------------------------------------------


async def test_extract_markdown_code_block_with_language() -> None:
    """A fenced block with a language tag is captured with that language."""
    md = (
        "Intro prose.\n"
        "\n"
        "```python\n"
        'print("hi")\n'
        "```\n"
        "\n"
        "More prose.\n"
    )
    blocks = await enrich_blocks(parsed=_parsed(md, "markdown"))
    codes = [b for b in blocks if b.get("type") == "code"]
    assert len(codes) == 1
    assert codes[0]["language"] == "python"
    assert 'print("hi")' in str(codes[0]["text"])


async def test_extract_markdown_code_block_without_language() -> None:
    """A fence without a language tag is captured with an empty language."""
    md = (
        "Prose.\n"
        "\n"
        "```\n"
        "raw content\n"
        "with two lines\n"
        "```\n"
        "\n"
        "Tail.\n"
    )
    blocks = await enrich_blocks(parsed=_parsed(md, "markdown"))
    codes = [b for b in blocks if b.get("type") == "code"]
    assert len(codes) == 1
    assert codes[0]["language"] == ""
    assert "raw content\nwith two lines" in str(codes[0]["text"])


async def test_extract_markdown_multiple_code_blocks() -> None:
    """Several fenced blocks are all captured, in source order."""
    md = (
        "```python\n"
        "a = 1\n"
        "```\n"
        "\n"
        "Some prose between.\n"
        "\n"
        "```javascript\n"
        "const x = 2;\n"
        "```\n"
        "\n"
        "End.\n"
        "\n"
        "```\n"
        "no language\n"
        "```\n"
    )
    blocks = await enrich_blocks(parsed=_parsed(md, "markdown"))
    codes = [b for b in blocks if b.get("type") == "code"]
    assert len(codes) == 3
    assert [c["language"] for c in codes] == ["python", "javascript", ""]
    assert "a = 1" in str(codes[0]["text"])
    assert "const x = 2;" in str(codes[1]["text"])
    assert "no language" in str(codes[2]["text"])


async def test_inline_code_is_not_extracted_as_block() -> None:
    """Single-backtick inline code stays in prose; only fenced blocks count."""
    md = (
        "Use `print()` to log `42` and the value of `x`.\n"
        "\n"
        "```python\n"
        "print(42)\n"
        "```\n"
    )
    blocks = await enrich_blocks(parsed=_parsed(md, "markdown"))
    codes = [b for b in blocks if b.get("type") == "code"]
    assert len(codes) == 1
    assert codes[0]["language"] == "python"
    # No stray inline-code block.
    assert all("print()" not in str(c.get("text", "")) for c in codes)


async def test_code_block_with_html_inside_is_preserved_verbatim() -> None:
    """HTML / angle brackets inside a code block are kept as-is.

    The block extractor MUST NOT do any HTML stripping — the body is
    raw bytes of the fence, period. This guards against future
    refactors that might accidentally run the body through a sanitizer.
    """
    md = (
        "```html\n"
        "<div class='foo'>&amp;copy;</div>\n"
        "<script>alert('xss')</script>\n"
        "```\n"
    )
    blocks = await enrich_blocks(parsed=_parsed(md, "markdown"))
    codes = [b for b in blocks if b.get("type") == "code"]
    assert len(codes) == 1
    body = str(codes[0]["text"])
    assert "<div class='foo'>" in body
    assert "&amp;copy;" in body
    assert "<script>alert('xss')</script>" in body


async def test_markdown_with_unbalanced_backticks_does_not_crash() -> None:
    """A stray ``` without a closing fence must not raise; it just yields no block."""
    md = (
        "Some prose.\n"
        "\n"
        "```python\n"
        "this fence is never closed\n"
        "\n"
        "More prose with no code.\n"
    )
    # Should NOT raise.
    blocks = await enrich_blocks(parsed=_parsed(md, "markdown"))
    # We may or may not capture anything; the contract is just
    # "don't crash and don't lie about what we found".
    codes = [b for b in blocks if b.get("type") == "code"]
    assert isinstance(codes, list)


# ---------------------------------------------------------------------------
# Images (markdown)
# ---------------------------------------------------------------------------


async def test_extract_markdown_image_with_alt() -> None:
    """``![alt](src)`` is captured as an image block with both fields."""
    md = "Here is a diagram:\n\n![Architecture overview](diagram.png)\n"
    blocks = await enrich_blocks(parsed=_parsed(md, "markdown"))
    images = [b for b in blocks if b.get("type") == "image"]
    assert len(images) == 1
    assert images[0]["alt"] == "Architecture overview"
    assert images[0]["src"] == "diagram.png"


async def test_extract_markdown_image_without_alt() -> None:
    """``![](src)`` is captured with an empty alt (valid per CommonMark)."""
    md = "An image with no alt: ![](logo.svg)\n"
    blocks = await enrich_blocks(parsed=_parsed(md, "markdown"))
    images = [b for b in blocks if b.get("type") == "image"]
    assert len(images) == 1
    assert images[0]["alt"] == ""
    assert images[0]["src"] == "logo.svg"


# ---------------------------------------------------------------------------
# strip_markdown_blocks helper
# ---------------------------------------------------------------------------


async def test_strip_markdown_blocks_removes_code_and_image_spans() -> None:
    """strip_markdown_blocks deletes both fenced code + inline images."""
    md = (
        "Intro.\n"
        "\n"
        "```python\n"
        "secret_code()\n"
        "```\n"
        "\n"
        "Look at ![diagram](d.png) please.\n"
    )
    blocks = await enrich_blocks(parsed=_parsed(md, "markdown"))
    cleaned = strip_markdown_blocks(md, blocks)
    assert "secret_code" not in cleaned
    assert "```" not in cleaned
    assert "![diagram]" not in cleaned
    assert "diagram.png" not in cleaned
    assert "Intro." in cleaned
    assert "Look at" in cleaned
    assert "please." in cleaned


# ---------------------------------------------------------------------------
# Images (HTML)
# ---------------------------------------------------------------------------


async def test_extract_html_image_with_alt_and_src() -> None:
    """A standalone ``<img>`` yields an image block with alt + src."""
    html = b'<html><body><img alt="Logo" src="logo.png" /></body></html>'
    blocks = await enrich_blocks(parsed=_parsed(html.decode(), "html", raw_source=html))
    images = [b for b in blocks if b.get("type") == "image"]
    assert len(images) == 1
    assert images[0]["alt"] == "Logo"
    assert images[0]["src"] == "logo.png"
    # No caption on a standalone image.
    assert "caption" not in images[0]


async def test_extract_html_image_inside_figure_with_caption() -> None:
    """An ``<img>`` inside ``<figure><figcaption>`` picks up the caption."""
    html = (
        b"<html><body>"
        b'<figure><img src="chart.png" alt="Sales chart" />'
        b"<figcaption>Q4 sales overview</figcaption></figure>"
        b"</body></html>"
    )
    blocks = await enrich_blocks(parsed=_parsed(html.decode(), "html", raw_source=html))
    images = [b for b in blocks if b.get("type") == "image"]
    assert len(images) == 1
    assert images[0]["alt"] == "Sales chart"
    assert images[0]["src"] == "chart.png"
    assert images[0]["caption"] == "Q4 sales overview"


async def test_html_anchor_with_text_is_not_extracted() -> None:
    """Plain ``<a>link text</a>`` is NOT an image block — only ``<img>`` is."""
    html = (
        b"<html><body>"
        b'<p>See <a href="https://example.com">our docs</a> for more.</p>'
        b"</body></html>"
    )
    blocks = await enrich_blocks(parsed=_parsed(html.decode(), "html", raw_source=html))
    assert blocks == []


async def test_html_image_inside_noscript_is_ignored() -> None:
    """``<img>`` inside ``<noscript>`` is stripped by the parser, not extracted."""
    html = (
        b"<html><body>"
        b"<noscript><img src='fallback.png' alt='fallback' /></noscript>"
        b'<img src="real.png" alt="real" />'
        b"</body></html>"
    )
    blocks = await enrich_blocks(parsed=_parsed(html.decode(), "html", raw_source=html))
    images = [b for b in blocks if b.get("type") == "image"]
    # Only the real one survives; the <noscript> one is decomposed first.
    assert len(images) == 1
    assert images[0]["src"] == "real.png"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


async def test_extract_from_text_format_returns_empty() -> None:
    """Plain text never produces multimodal blocks in M1."""
    blocks = await enrich_blocks(
        parsed=_parsed("some plain text content\nwith two lines", "text")
    )
    assert blocks == []


async def test_extract_from_json_format_returns_empty() -> None:
    """JSON never produces multimodal blocks in M1."""
    payload = '{"key": "value", "list": [1, 2, 3]}'
    blocks = await enrich_blocks(parsed=_parsed(payload, "json"))
    assert blocks == []


async def test_extract_from_pdf_format_returns_empty() -> None:
    """PDF never produces multimodal blocks in M1 (OCR is a Stage 7 concern)."""
    blocks = await enrich_blocks(parsed=_parsed("", "pdf", page_count=3))
    assert blocks == []


async def test_extract_handles_malformed_html_gracefully() -> None:
    """Garbage HTML bytes do NOT raise — returns what it can + warns."""
    # Mismatched / partial tags. BeautifulSoup is forgiving; this
    # exercises the defensive try/except around _extract_html_blocks.
    html = b"<html><body><img src='a.png' alt='a'><p>unclosed paragraph"
    blocks = await enrich_blocks(parsed=_parsed(html.decode(), "html", raw_source=html))
    # We don't care WHAT it returns, only that it doesn't raise.
    assert isinstance(blocks, list)
    images = [b for b in blocks if b.get("type") == "image"]
    # The <img> is well-formed enough that the parser still sees it.
    assert any(img.get("src") == "a.png" for img in images)


async def test_extract_handles_binary_garbage_html_gracefully() -> None:
    """Truly binary bytes passed as 'html' do NOT crash extraction."""
    # NUL bytes + random binary — would normally be rejected by the
    # parser's format sniff, but the multimodal helper should still be
    # defensive on its own.
    garbage = b"\x00\x01\x02\xff\xfe<html><body><img src='x' alt='y'></body></html>\x00"
    blocks = await enrich_blocks(
        parsed=_parsed(garbage.decode("utf-8", "replace"), "html", raw_source=garbage)
    )
    assert isinstance(blocks, list)


# ---------------------------------------------------------------------------
# Integration with parser
# ---------------------------------------------------------------------------


async def test_parser_populates_blocks_for_markdown() -> None:
    """``parse_document`` for a .md file populates ``blocks`` automatically.

    Documents the chosen integration (option a: parser calls
    ``enrich_blocks`` internally so callers never need to do a second
    step).
    """
    from knowledge.parser import parse_document

    md = (
        b"# Title\n"
        b"\n"
        b"Some prose.\n"
        b"\n"
        b"```python\n"
        b"print('hi')\n"
        b"```\n"
        b"\n"
        b"A diagram: ![diagram](d.png)\n"
    )
    result = await parse_document(file_bytes=md, file_name="doc.md", mime_type=None)
    types = {b.get("type") for b in result.blocks}
    assert "code" in types
    assert "image" in types
    # The code body and the image src both appear in blocks (not text).
    code_block = next(b for b in result.blocks if b.get("type") == "code")
    assert "print('hi')" in str(code_block["text"])
    img_block = next(b for b in result.blocks if b.get("type") == "image")
    assert img_block["src"] == "d.png"
    # And the linearized text does NOT carry the code body or the
    # image syntax — those were stripped.
    assert "print('hi')" not in result.text
    assert "d.png" not in result.text


async def test_parser_populates_blocks_for_html() -> None:
    """``parse_document`` for an .html file populates ``blocks`` automatically."""
    from knowledge.parser import parse_document

    html = (
        b"<html><body>"
        b"<h1>Welcome</h1>"
        b"<p>Intro paragraph.</p>"
        b'<img src="logo.png" alt="Logo" />'
        b"</body></html>"
    )
    result = await parse_document(file_bytes=html, file_name="page.html", mime_type=None)
    types = {b.get("type") for b in result.blocks}
    assert "image" in types
    # The intro paragraph still lives in the linearized text.
    assert "Intro paragraph." in result.text
    # But the image src / alt don't double-appear.
    assert "logo.png" not in result.text


# ---------------------------------------------------------------------------
# Important 2 — Markdown images with balanced parens in src
# ---------------------------------------------------------------------------


async def test_markdown_image_with_balanced_parens_in_src() -> None:
    """A markdown image whose src contains balanced parens captures the FULL url.

    CommonMark allows ``![alt](https://example.com/foo(bar).png)``.
    The earlier regex ``[^)]+`` would stop at the first ``)`` and
    capture only ``https://example.com/foo(bar``. This test guards the
    depth-aware src scan.
    """
    md = (
        "Look at this chart:\n\n"
        "![sales chart](https://example.com/charts/sales(q4).png)\n"
    )
    blocks = await enrich_blocks(parsed=_parsed(md, "markdown"))
    images = [b for b in blocks if b.get("type") == "image"]
    assert len(images) == 1
    assert images[0]["alt"] == "sales chart"
    assert images[0]["src"] == "https://example.com/charts/sales(q4).png"


async def test_markdown_image_with_escaped_paren_in_src() -> None:
    """An escaped ``\\)`` inside the src does NOT terminate the link."""
    md = "x\n\n![alt](https://example.com/a\\)b.png)\n"
    blocks = await enrich_blocks(parsed=_parsed(md, "markdown"))
    images = [b for b in blocks if b.get("type") == "image"]
    assert len(images) == 1
    assert images[0]["src"] == "https://example.com/a\\)b.png"


async def test_strip_markdown_blocks_removes_image_with_balanced_parens() -> None:
    """``strip_markdown_blocks`` removes the entire image span, parens and all."""
    md = (
        "Intro.\n\n"
        "![sales chart](https://example.com/charts/sales(q4).png)\n\n"
        "Tail.\n"
    )
    blocks = await enrich_blocks(parsed=_parsed(md, "markdown"))
    cleaned = strip_markdown_blocks(md, blocks)
    assert "https://example.com/charts/sales(q4).png" not in cleaned
    assert "![sales chart]" not in cleaned
    assert "Intro." in cleaned
    assert "Tail." in cleaned


# ---------------------------------------------------------------------------
# Important 3 — enrich_blocks html path is live when raw_source is present
# ---------------------------------------------------------------------------


async def test_enrich_blocks_html_with_raw_source_metadata() -> None:
    """An HTML ParsedDocument carrying ``raw_source`` populates blocks via enrich_blocks.

    Important 3 fix: this path used to be dead code because the parser
    never set ``metadata['raw_source']``. Now that the parser does, an
    external caller can build a ParsedDocument with the raw bytes and
    call ``enrich_blocks`` directly without going through the parser.
    """
    html_bytes = (
        b"<html><body>"
        b"<p>Intro paragraph.</p>"
        b'<img src="logo.png" alt="Logo" />'
        b"</body></html>"
    )
    doc = ParsedDocument(
        text="Intro paragraph.",  # linearized text — NOT what we extract from
        blocks=[],
        metadata={"source_format": "html", "raw_source": html_bytes},
        format="html",
    )
    blocks = await enrich_blocks(parsed=doc)
    images = [b for b in blocks if b.get("type") == "image"]
    assert len(images) == 1
    assert images[0]["src"] == "logo.png"
    assert images[0]["alt"] == "Logo"


async def test_enrich_blocks_html_without_raw_source_returns_empty() -> None:
    """An HTML ParsedDocument missing ``raw_source`` returns ``[]`` (defensive)."""
    doc = ParsedDocument(
        text="Intro paragraph.",
        blocks=[],
        metadata={"source_format": "html"},  # no raw_source
        format="html",
    )
    blocks = await enrich_blocks(parsed=doc)
    assert blocks == []


# ---------------------------------------------------------------------------
# Important 5 — <figcaption> must not double-embed into the linearized text
# ---------------------------------------------------------------------------


async def test_html_figcaption_text_does_not_double_embed_in_linearized_text() -> None:
    """A ``<figure><img/><figcaption>...</figcaption></figure>`` puts the caption
    on the block AND keeps it OUT of the linearized text.
    """
    from knowledge.parser import parse_document

    html = (
        b"<html><body>"
        b"<p>Before the figure.</p>"
        b'<figure><img src="chart.png" alt="Sales chart" />'
        b"<figcaption>Q4 sales overview</figcaption></figure>"
        b"<p>After the figure.</p>"
        b"</body></html>"
    )
    result = await parse_document(file_bytes=html, file_name="page.html", mime_type=None)

    # The image block carries the caption.
    images = [b for b in result.blocks if b.get("type") == "image"]
    assert len(images) == 1
    assert images[0]["caption"] == "Q4 sales overview"

    # But the caption does NOT leak into the linearized text — the
    # entire <figure> was decomposed after capture.
    assert "Q4 sales overview" not in result.text

    # Surrounding prose is preserved.
    assert "Before the figure." in result.text
    assert "After the figure." in result.text


async def test_html_image_inside_anchor_is_extracted() -> None:
    """An ``<img>`` nested inside ``<a>`` is still extracted as an image block."""
    html = (
        b"<html><body>"
        b'<a href="https://example.com">'
        b'<img src="thumb.png" alt="Thumbnail" />'
        b"</a>"
        b"</body></html>"
    )
    blocks = await enrich_blocks(parsed=_parsed(html.decode(), "html", raw_source=html))
    images = [b for b in blocks if b.get("type") == "image"]
    assert len(images) == 1
    assert images[0]["alt"] == "Thumbnail"
    assert images[0]["src"] == "thumb.png"


async def test_html_image_inside_picture_is_extracted() -> None:
    """An ``<img>`` nested inside ``<picture>`` is still extracted as an image block."""
    html = (
        b"<html><body>"
        b"<picture>"
        b'<source srcset="big.png" media="(min-width: 800px)" />'
        b'<img src="small.png" alt="Responsive" />'
        b"</picture>"
        b"</body></html>"
    )
    blocks = await enrich_blocks(parsed=_parsed(html.decode(), "html", raw_source=html))
    images = [b for b in blocks if b.get("type") == "image"]
    assert len(images) == 1
    assert images[0]["src"] == "small.png"
    assert images[0]["alt"] == "Responsive"


# ---------------------------------------------------------------------------
# Minor — Empty code-block body edge case
# ---------------------------------------------------------------------------


async def test_markdown_empty_code_block_body_is_captured() -> None:
    """An empty fenced code block yields a code block with an empty body.

    `````\\n`````` (just two fences, nothing between) is a legal CommonMark
    code block; we must capture it as a code block with an empty body
    rather than skipping it.
    """
    md = (
        "Intro prose.\n"
        "\n"
        "```\n"
        "```\n"
        "\n"
        "Tail prose.\n"
    )
    blocks = await enrich_blocks(parsed=_parsed(md, "markdown"))
    codes = [b for b in blocks if b.get("type") == "code"]
    assert len(codes) == 1
    assert codes[0]["language"] == ""
    assert str(codes[0]["text"]) == ""


# ---------------------------------------------------------------------------
# Private helpers are no longer imported cross-module (Important 1)
# ---------------------------------------------------------------------------


def test_private_helpers_are_underscore_prefixed() -> None:
    """The parser must NOT import the private ``_extract_*`` helpers directly.

    Guards against regressions of the "Parser imports private helpers
    cross-module" issue.
    """
    import inspect

    from knowledge import parser

    source = inspect.getsource(parser)
    assert "_extract_markdown_blocks" not in source, (
        "parser.py must not import the private _extract_markdown_blocks — "
        "route through enrich_blocks(parsed=...) instead."
    )
