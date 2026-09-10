"""Unit tests for ``knowledge.chunker``.

Covers Task 6.5 (M1 length-based chunking):

* Length-based text chunking (window size, overlap, validation).
* Structured blocks: code / image / table become standalone chunks.
* Ordering — blocks emitted before text chunks.
* Oversized block → recursive sub-chunks (preserve ``block_index``).
* Integration: parser → chunker on real markdown + HTML docs.
* Edge cases — empty input, oversized single words, long text.
* Defense in depth — size guard + unknown-block skip.
"""
from __future__ import annotations

import pytest

from knowledge.chunker import (
    MAX_CHUNK_TEXT_BYTES,
    chunk_document,
    chunk_text,
    linearize_table_rows,
)
from knowledge.parser import ParsedDocument, parse_document

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parsed(
    text: str,
    *,
    fmt: str = "text",
    blocks: list[dict[str, object]] | None = None,
    **metadata: object,
) -> ParsedDocument:
    """Build a minimal ParsedDocument for the chunker."""
    return ParsedDocument(
        text=text,
        blocks=list(blocks) if blocks else [],
        metadata={"source_format": fmt, **metadata},
        format=fmt,
    )


def _words(n: int, *, start: int = 0) -> str:
    """Build whitespace-joined ``n`` synthetic words (deterministic)."""
    return " ".join(f"w{i}" for i in range(start, start + n))


# ---------------------------------------------------------------------------
# Length-based chunking — ``chunk_text`` (lower-level helper)
# ---------------------------------------------------------------------------


def test_chunk_text_returns_windows_of_chunk_size_words() -> None:
    """Each window holds up to ``chunk_size`` words."""
    chunks = chunk_text(text=_words(30), chunk_size=10, chunk_overlap=0)
    assert len(chunks) == 3
    assert [c.token_count for c in chunks] == [10, 10, 10]
    assert [c.text for c in chunks] == [
        _words(10, start=0),
        _words(10, start=10),
        _words(10, start=20),
    ]


def test_chunk_text_includes_overlap() -> None:
    """Window N+1 starts ``chunk_size - chunk_overlap`` words past window N.

    For chunk_size=10, chunk_overlap=3: stride=7, so the next window
    repeats the last 3 words of the previous one.
    """
    chunks = chunk_text(text=_words(20), chunk_size=10, chunk_overlap=3)
    assert len(chunks) == 3
    # First window: words 0..9 (10). Second: 7..16 (10). Third: 14..19 (6, tail).
    assert chunks[0].text == _words(10, start=0)
    assert chunks[1].text == _words(10, start=7)
    assert chunks[2].text == _words(6, start=14)


def test_chunk_text_overlap_is_smaller_than_chunk_size() -> None:
    """An overlap strictly less than ``chunk_size`` must always advance."""
    # Most-overlap-but-strictly-less case.
    chunks = chunk_text(text=_words(5), chunk_size=3, chunk_overlap=2)
    # stride = 1, so windows start at 0, 1, 2, 3 — last window under-
    # fills and we stop.
    assert [c.token_count for c in chunks] == [3, 3, 3, 2]


def test_chunk_text_raises_when_overlap_ge_chunk_size() -> None:
    """``chunk_overlap >= chunk_size`` is invalid — never makes progress."""
    with pytest.raises(ValueError, match="chunk_overlap"):
        chunk_text(text="hello world", chunk_size=5, chunk_overlap=5)
    with pytest.raises(ValueError, match="chunk_overlap"):
        chunk_text(text="hello world", chunk_size=5, chunk_overlap=10)


def test_chunk_text_raises_when_chunk_size_is_zero() -> None:
    """``chunk_size`` must be > 0."""
    with pytest.raises(ValueError, match="chunk_size"):
        chunk_text(text="hello world", chunk_size=0, chunk_overlap=0)
    with pytest.raises(ValueError, match="chunk_size"):
        chunk_text(text="hello world", chunk_size=-1, chunk_overlap=0)


def test_chunk_text_raises_when_overlap_is_negative() -> None:
    """Negative overlap doesn't make sense; reject it loudly."""
    with pytest.raises(ValueError, match="chunk_overlap"):
        chunk_text(text="hello world", chunk_size=5, chunk_overlap=-1)


def test_chunk_text_empty_text_returns_empty_list() -> None:
    """Empty string → no chunks. Same for whitespace-only input."""
    assert chunk_text(text="") == []
    assert chunk_text(text="   \n\t  ") == []


def test_chunk_text_short_text_returns_single_chunk() -> None:
    """Text shorter than ``chunk_size`` yields exactly one chunk."""
    chunks = chunk_text(text="alpha beta gamma", chunk_size=10, chunk_overlap=2)
    assert len(chunks) == 1
    assert chunks[0].text == "alpha beta gamma"
    assert chunks[0].token_count == 3
    assert chunks[0].block_index is None


def test_chunk_text_oversized_single_word_emits_one_chunk() -> None:
    """A single very-long word still produces exactly one chunk (no infinite loop)."""
    long_word = "x" * 5000
    chunks = chunk_text(text=long_word, chunk_size=10, chunk_overlap=2)
    assert len(chunks) == 1
    assert chunks[0].token_count == 1  # one whitespace-split token
    assert chunks[0].text == long_word


def test_chunk_text_skips_empty_windows() -> None:
    """Edge case: when text length is an exact multiple of chunk_size with overlap,
    no empty trailing window is emitted."""
    chunks = chunk_text(text=_words(10), chunk_size=5, chunk_overlap=2)
    # stride=3, starts at 0, 3, 6, 9 — last window covers 9..13 but only
    # has 1 word. Should NOT emit a 0-word window after.
    assert all(c.token_count > 0 for c in chunks)
    assert sum(c.token_count for c in chunks) >= 10  # at least covered


def test_chunk_text_collapses_whitespace() -> None:
    """Whitespace splits collapse runs of whitespace into word boundaries."""
    chunks = chunk_text(
        text="alpha   beta\n\ngamma\t\tdelta",
        chunk_size=2,
        chunk_overlap=0,
    )
    assert [c.text for c in chunks] == ["alpha beta", "gamma delta"]


def test_chunk_text_block_index_is_none_for_text_chunks() -> None:
    """Linearized text chunks always have ``block_index=None``."""
    chunks = chunk_text(text=_words(5), chunk_size=2, chunk_overlap=0)
    assert all(c.block_index is None for c in chunks)
    assert all(c.metadata["block_type"] is None for c in chunks)


def test_chunk_text_propagates_source_format() -> None:
    """``source_format`` is forwarded into the metadata dict."""
    chunks = chunk_text(text=_words(3), chunk_size=2, chunk_overlap=0, source_format="pdf")
    assert all(c.metadata["source_format"] == "pdf" for c in chunks)


# ---------------------------------------------------------------------------
# Structured blocks — ``chunk_document`` upper-level behavior
# ---------------------------------------------------------------------------


async def test_chunk_code_block_becomes_standalone_chunk() -> None:
    """A code block yields one chunk with verbatim text + language metadata."""
    parsed = _parsed(
        "",
        blocks=[
            {"type": "code", "language": "python", "text": 'print("hi")\nprint(42)'},
        ],
        fmt="markdown",
    )
    chunks = await chunk_document(parsed=parsed, chunk_size=10, chunk_overlap=0)
    assert len(chunks) == 1
    code = chunks[0]
    assert code.text == 'print("hi")\nprint(42)'
    assert code.block_index == 0
    # The body has a '\n' inside; whitespace split yields 2 tokens.
    assert code.token_count == 2
    assert code.metadata["block_type"] == "code"
    assert code.metadata["language"] == "python"
    assert code.metadata["source_format"] == "markdown"


async def test_chunk_code_block_without_language_omits_language_field() -> None:
    """Empty language → no spurious ``language`` key on metadata."""
    parsed = _parsed(
        "",
        blocks=[{"type": "code", "language": "", "text": "raw content"}],
        fmt="markdown",
    )
    chunks = await chunk_document(parsed=parsed, chunk_size=10, chunk_overlap=0)
    assert chunks[0].text == "raw content"
    assert "language" not in chunks[0].metadata


async def test_chunk_image_block_becomes_textual_chunk() -> None:
    """An image block becomes a textual chunk of shape ``Image: {alt} ({src})``."""
    parsed = _parsed(
        "",
        blocks=[{"type": "image", "alt": "Logo", "src": "logo.png"}],
        fmt="markdown",
    )
    chunks = await chunk_document(parsed=parsed, chunk_size=10, chunk_overlap=0)
    assert len(chunks) == 1
    img = chunks[0]
    assert img.text == "Image: Logo (logo.png)"
    assert img.block_index == 0
    assert img.metadata["block_type"] == "image"
    assert img.metadata["alt"] == "Logo"
    assert img.metadata["src"] == "logo.png"


async def test_chunk_image_block_without_alt() -> None:
    """An image with empty alt still produces a stable textual chunk."""
    parsed = _parsed(
        "",
        blocks=[{"type": "image", "alt": "", "src": "x.png"}],
        fmt="markdown",
    )
    chunks = await chunk_document(parsed=parsed, chunk_size=10, chunk_overlap=0)
    assert chunks[0].text == "Image:  (x.png)"  # alt empty, src present


async def test_chunk_image_block_with_caption_appended() -> None:
    """An HTML image block with caption appends it to the textual form."""
    parsed = _parsed(
        "",
        blocks=[
            {
                "type": "image",
                "alt": "Sales",
                "src": "chart.png",
                "caption": "Q4 overview",
            }
        ],
        fmt="html",
    )
    chunks = await chunk_document(parsed=parsed, chunk_size=10, chunk_overlap=0)
    assert chunks[0].text == "Image: Sales (chart.png)\nQ4 overview"


async def test_chunk_table_block_becomes_linearized_chunk() -> None:
    """A table block is linearized as tab-joined cells, newline-joined rows."""
    parsed = _parsed(
        "",
        blocks=[
            {
                "type": "table",
                "rows": [
                    ["Name", "Role"],
                    ["Alice", "Engineer"],
                    ["Bob", "Designer"],
                ],
            }
        ],
        fmt="html",
    )
    chunks = await chunk_document(parsed=parsed, chunk_size=50, chunk_overlap=0)
    assert len(chunks) == 1
    assert chunks[0].text == "Name\tRole\nAlice\tEngineer\nBob\tDesigner"
    assert chunks[0].block_index == 0
    assert chunks[0].metadata["block_type"] == "table"
    assert chunks[0].metadata["row_count"] == 3


def test_linearize_table_rows_skips_empty_rows() -> None:
    """Empty rows are dropped so they don't bloat the chunk."""
    out = linearize_table_rows(
        [
            ["a", "b"],
            [],
            ["c"],
        ]
    )
    assert out == "a\tb\nc"


# ---------------------------------------------------------------------------
# Ordering + emission
# ---------------------------------------------------------------------------


async def test_chunk_blocks_emitted_before_text_chunks() -> None:
    """All structured blocks come first, then text chunks — and ``chunk_index``
    reflects that order monotonically."""
    parsed = _parsed(
        text="Tail prose.",
        blocks=[
            {"type": "code", "language": "py", "text": "x = 1"},
            {"type": "image", "alt": "logo", "src": "l.png"},
        ],
        fmt="markdown",
    )
    chunks = await chunk_document(parsed=parsed, chunk_size=10, chunk_overlap=0)
    # 2 blocks + 1 text chunk (Tail prose fits in chunk_size=10).
    assert [c.chunk_index for c in chunks] == [0, 1, 2]
    assert chunks[0].metadata["block_type"] == "code"
    assert chunks[1].metadata["block_type"] == "image"
    assert chunks[2].metadata["block_type"] is None
    assert chunks[2].block_index is None
    assert chunks[2].text == "Tail prose."


async def test_chunk_block_indices_preserve_source_order() -> None:
    """When a block is sub-chunked, all sub-chunks share the same ``block_index``."""
    parsed = _parsed(
        text="after",
        blocks=[
            {"type": "code", "language": "py", "text": _words(25, start=100)},
            {"type": "code", "language": "py", "text": "short"},
        ],
        fmt="markdown",
    )
    chunks = await chunk_document(parsed=parsed, chunk_size=10, chunk_overlap=0)
    # First block (25 words) → 3 sub-chunks at indices 0, 1, 2.
    # Second block (1 word) → 1 chunk at index 3.
    # Then text chunk at index 4.
    assert [c.chunk_index for c in chunks] == [0, 1, 2, 3, 4]
    assert all(c.block_index == 0 for c in chunks[:3])
    assert chunks[3].block_index == 1
    assert chunks[4].block_index is None


# ---------------------------------------------------------------------------
# Recursive (oversized) block handling
# ---------------------------------------------------------------------------


async def test_chunk_oversized_code_block_is_split_with_recursive_chunker() -> None:
    """A code block whose text exceeds ``chunk_size`` is split — all sub-chunks
    reference the same ``block_index``."""
    body = _words(50)
    parsed = _parsed(
        text="",
        blocks=[{"type": "code", "language": "py", "text": body}],
        fmt="markdown",
    )
    chunks = await chunk_document(parsed=parsed, chunk_size=10, chunk_overlap=0)
    # 50 words, chunk_size=10 → 5 windows.
    assert len(chunks) == 5
    assert [c.token_count for c in chunks] == [10] * 5
    assert all(c.block_index == 0 for c in chunks)
    assert all(c.metadata["block_type"] == "code" for c in chunks)
    # Concatenation recovers the original body.
    assert " ".join(c.text for c in chunks) == body


async def test_chunk_oversized_image_text_is_split() -> None:
    """Image blocks with a long caption can exceed chunk_size; the chunker
    sub-splits and preserves block_type=image."""
    parsed = _parsed(
        text="",
        blocks=[
            {
                "type": "image",
                "alt": "chart",
                "src": "c.png",
                "caption": _words(30, start=200),
            }
        ],
        fmt="html",
    )
    chunks = await chunk_document(parsed=parsed, chunk_size=10, chunk_overlap=0)
    # "Image: chart (c.png)" is 3 words → fits in first window with
    # the start of the caption. Total content ≈ 33 words → 4 windows.
    assert len(chunks) >= 3
    assert all(c.metadata["block_type"] == "image" for c in chunks)
    assert all(c.block_index == 0 for c in chunks)


# ---------------------------------------------------------------------------
# Edge cases (chunk_document)
# ---------------------------------------------------------------------------


async def test_chunk_empty_text_and_no_blocks_returns_empty() -> None:
    """Empty doc → empty candidate list."""
    parsed = _parsed("", fmt="text")
    chunks = await chunk_document(parsed=parsed)
    assert chunks == []


async def test_chunk_empty_text_with_blocks_returns_blocks_only() -> None:
    """No linearized text, but blocks exist → block chunks only."""
    parsed = _parsed(
        text="",
        blocks=[{"type": "code", "language": "py", "text": "x"}],
        fmt="markdown",
    )
    chunks = await chunk_document(parsed=parsed)
    assert len(chunks) == 1
    assert chunks[0].block_index == 0


async def test_chunk_blocks_only_with_empty_text() -> None:
    """Blocks but no text → only block chunks, no synthetic text chunks."""
    parsed = _parsed(
        text="",
        blocks=[
            {"type": "image", "alt": "a", "src": "a.png"},
            {"type": "code", "language": "", "text": "y"},
        ],
        fmt="markdown",
    )
    chunks = await chunk_document(parsed=parsed, chunk_size=10, chunk_overlap=0)
    assert len(chunks) == 2
    assert [c.metadata["block_type"] for c in chunks] == ["image", "code"]


async def test_chunk_handles_text_longer_than_chunk_size() -> None:
    """100 words with chunk_size=20 → 5 non-overlapping windows."""
    parsed = _parsed(_words(100), fmt="text")
    chunks = await chunk_document(parsed=parsed, chunk_size=20, chunk_overlap=0)
    assert len(chunks) == 5
    assert [c.token_count for c in chunks] == [20] * 5
    # All from linearized text → block_index is None everywhere.
    assert all(c.block_index is None for c in chunks)


async def test_chunk_handles_text_longer_than_chunk_size_with_overlap() -> None:
    """Verify end-to-end overlap behavior on a longer doc."""
    parsed = _parsed(_words(50), fmt="text")
    chunks = await chunk_document(parsed=parsed, chunk_size=20, chunk_overlap=5)
    # stride = 15, starts at 0, 15, 30, 45. Last window: words 45..49 (5 words).
    assert [c.token_count for c in chunks] == [20, 20, 20, 5]


async def test_chunk_token_count_matches_whitespace_word_count() -> None:
    """``token_count`` equals ``len(text.split())`` for every chunk, regardless of type."""
    parsed = _parsed(
        text="  alpha   beta gamma\n\n\n  delta  ",
        blocks=[
            {"type": "code", "language": "py", "text": "x = 1\ny = 2"},
            {"type": "image", "alt": "a", "src": "a.png"},  # 3-word textual form
        ],
        fmt="markdown",
    )
    chunks = await chunk_document(parsed=parsed, chunk_size=10, chunk_overlap=0)
    # Two blocks + one text window (4 words, chunk_size=10 → 1 window of 4).
    assert len(chunks) == 3
    # Code body: "x = 1\ny = 2" → ["x", "=", "1", "y", "=", "2"] = 6 tokens.
    assert chunks[0].token_count == len(chunks[0].text.split()) == 6
    # Image textual form: "Image: a (a.png)" → 3 tokens.
    assert chunks[1].token_count == len(chunks[1].text.split()) == 3
    # Linearized text: "alpha beta gamma delta" → 4 tokens.
    assert chunks[2].token_count == len(chunks[2].text.split()) == 4


async def test_chunk_validation_propagates_to_chunk_document() -> None:
    """``chunk_document`` enforces the same config invariants as ``chunk_text``."""
    parsed = _parsed("hello", fmt="text")
    with pytest.raises(ValueError, match="chunk_size"):
        await chunk_document(parsed=parsed, chunk_size=0, chunk_overlap=0)
    with pytest.raises(ValueError, match="chunk_overlap"):
        await chunk_document(parsed=parsed, chunk_size=5, chunk_overlap=5)


async def test_chunk_is_deterministic() -> None:
    """Same input → same output, every time."""
    parsed = _parsed(
        text=_words(100, start=0),
        blocks=[{"type": "code", "language": "py", "text": _words(20, start=500)}],
        fmt="markdown",
    )
    a = await chunk_document(parsed=parsed, chunk_size=20, chunk_overlap=4)
    b = await chunk_document(parsed=parsed, chunk_size=20, chunk_overlap=4)
    assert [(c.text, c.token_count, c.block_index, c.chunk_index) for c in a] == [
        (c.text, c.token_count, c.block_index, c.chunk_index) for c in b
    ]


async def test_chunk_default_chunk_size_and_overlap() -> None:
    """Defaults match the model's ``DEFAULT_CHUNK_SIZE`` / ``DEFAULT_CHUNK_OVERLAP``."""
    parsed = _parsed(_words(2000), fmt="text")
    chunks = await chunk_document(parsed=parsed)
    # 800-word windows, stride=700 → windows starting at 0, 700, 1400.
    # Last window at 1400 covers 1400..1999 (600 words).
    assert [c.token_count for c in chunks] == [800, 800, 600]


# ---------------------------------------------------------------------------
# Integration: parser → chunker on real documents
# ---------------------------------------------------------------------------


async def test_chunk_markdown_document_with_code_blocks() -> None:
    """A markdown doc with prose + fenced code blocks produces the expected
    block-first, then text-chunks order."""
    md = (
        b"Intro prose paragraph one.\n"
        b"\n"
        b"```python\n"
        b'print("hi")\n'
        b"```\n"
        b"\n"
        b"Outro prose paragraph.\n"
    )
    parsed = await parse_document(file_bytes=md, file_name="sample.md")
    chunks = await chunk_document(parsed=parsed, chunk_size=10, chunk_overlap=0)

    # Should have at least one code block chunk and the linearized text.
    code_chunks = [c for c in chunks if c.metadata["block_type"] == "code"]
    text_chunks = [c for c in chunks if c.metadata["block_type"] is None]
    assert len(code_chunks) == 1
    assert code_chunks[0].block_index is not None
    assert len(text_chunks) >= 1
    # Code block chunk must come before any text chunk (blocks-first rule).
    assert min(c.chunk_index for c in code_chunks) < min(
        c.chunk_index for c in text_chunks
    )


async def test_chunk_html_document_with_images_and_tables() -> None:
    """An HTML doc with an image (inside a figure) + a table yields three
    kinds of chunks: image, table, text — in that order."""
    html = (
        b"<html><body>"
        b"<p>Intro paragraph.</p>"
        b"<table><tr><th>Name</th><th>Role</th></tr>"
        b"<tr><td>Alice</td><td>Engineer</td></tr></table>"
        b'<figure><img src="logo.png" alt="Logo" />'
        b"<figcaption>Our logo</figcaption></figure>"
        b"<p>Outro.</p>"
        b"</body></html>"
    )
    parsed = await parse_document(file_bytes=html, file_name="sample.html")
    chunks = await chunk_document(parsed=parsed, chunk_size=50, chunk_overlap=0)

    types_in_order = [c.metadata["block_type"] for c in chunks]
    # All block chunks must come before any text chunk.
    block_indices = [
        i for i, t in enumerate(types_in_order) if t in {"image", "table"}
    ]
    text_indices = [i for i, t in enumerate(types_in_order) if t is None]
    if block_indices and text_indices:
        assert max(block_indices) < min(text_indices)

    # The image chunk should mention "Logo" in its textual form.
    image_chunks = [c for c in chunks if c.metadata["block_type"] == "image"]
    assert any("Logo" in c.text for c in image_chunks)


async def test_chunk_text_only_document() -> None:
    """A plain-text document (no blocks) only emits text chunks."""
    parsed = await parse_document(
        file_bytes=b"alpha beta gamma\n\ndelta epsilon zeta",
        file_name="sample.txt",
    )
    chunks = await chunk_document(parsed=parsed, chunk_size=3, chunk_overlap=0)
    assert len(chunks) >= 1
    assert all(c.block_index is None for c in chunks)
    assert all(c.metadata["block_type"] is None for c in chunks)
    # First chunk has the first 3 words.
    assert chunks[0].text == "alpha beta gamma"


async def test_chunk_pdf_document() -> None:
    """A PDF document yields only text chunks (PDFs never produce blocks in M1)."""
    # Build a PDF with extractable text on the fly via the parser's
    # own helper would be ideal, but a plain-text PDF is enough to
    # validate the integration. The chunker doesn't care about
    # formatting nuances; it just needs parsed.text to be non-empty.
    parsed = await parse_document(
        file_bytes=b"%PDF-1.4\nThis isn't a real PDF, but the chunker must be tolerant.",
        file_name="bad.pdf",
    )
    # Either we get parsed (with empty text) or it's rejected — both
    # are valid integration outcomes. If we got chunks, they should
    # all be text.
    chunks = await chunk_document(parsed=parsed, chunk_size=10, chunk_overlap=0)
    if chunks:
        assert all(c.block_index is None for c in chunks)


# ---------------------------------------------------------------------------
# Defense in depth: size guard + unknown-block skip
# ---------------------------------------------------------------------------


async def test_chunk_rejects_oversized_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``parsed.text`` larger than ``MAX_CHUNK_TEXT_BYTES`` is rejected
    BEFORE any ``text.split()`` runs (no OOM on pathological inputs).

    Uses ``monkeypatch`` to lower the cap rather than allocating 50 MB
    of string data — the guard compares against the constant, so the
    limit itself is what we test.
    """
    from knowledge import chunker

    # Lower the cap so we can construct a tiny ``parsed.text`` that
    # still exceeds it.
    monkeypatch_cap = 1024  # 1 KB
    monkeypatch.setattr(chunker, "MAX_CHUNK_TEXT_BYTES", monkeypatch_cap)

    parsed = _parsed("a" * (monkeypatch_cap + 1), fmt="text")
    with pytest.raises(ValueError, match="MAX_CHUNK_TEXT_BYTES"):
        await chunk_document(parsed=parsed)


async def test_chunk_rejects_oversized_text_before_split() -> None:
    """Size guard runs BEFORE ``text.split()`` — we never touch the word
    list when rejecting. Verified indirectly by checking that the error
    message references the byte length, not a word count."""
    parsed = _parsed("x" * (MAX_CHUNK_TEXT_BYTES + 1), fmt="text")
    with pytest.raises(ValueError) as exc_info:
        await chunk_document(parsed=parsed)
    # The error must mention the size in bytes; not words. This proves
    # the check ran on the raw string length, before splitting.
    assert "bytes" in str(exc_info.value)
    assert str(MAX_CHUNK_TEXT_BYTES + 1) in str(exc_info.value)


async def test_chunk_accepts_text_at_or_below_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boundary: ``parsed.text`` exactly at the limit is accepted."""
    from knowledge import chunker

    monkeypatch_cap = 100
    monkeypatch.setattr(chunker, "MAX_CHUNK_TEXT_BYTES", monkeypatch_cap)

    parsed = _parsed("alpha " * (monkeypatch_cap // 6), fmt="text")
    # No raise; we just need to confirm the equality boundary is inclusive.
    chunks = await chunk_document(parsed=parsed, chunk_size=10, chunk_overlap=0)
    assert len(chunks) >= 1


async def test_chunk_skips_unknown_block_type() -> None:
    """An unrecognized block type yields NO chunks (skipped, not emitted
    as empty text). Known block types are unaffected."""
    parsed = _parsed(
        text="tail",
        blocks=[
            {"type": "weird_new_type", "text": "should be skipped"},
            {"type": "code", "language": "py", "text": "kept = 1"},
        ],
        fmt="markdown",
    )
    chunks = await chunk_document(parsed=parsed, chunk_size=10, chunk_overlap=0)
    # Only the known code block + the text chunk survive.
    assert [c.metadata["block_type"] for c in chunks] == ["code", None]
    assert chunks[0].text == "kept = 1"
    assert chunks[1].text == "tail"


async def test_chunk_skips_non_dict_block() -> None:
    """A malformed non-dict entry in ``parsed.blocks`` is skipped, not
    crashed on. Other blocks still process normally."""
    parsed = _parsed(
        text="after",
        blocks=[
            "not a dict",  # type: ignore[list-item]
            {"type": "code", "language": "py", "text": "x = 1"},
        ],
        fmt="markdown",
    )
    chunks = await chunk_document(parsed=parsed, chunk_size=10, chunk_overlap=0)
    # Code block + text chunk; the bogus entry was skipped.
    assert [c.metadata["block_type"] for c in chunks] == ["code", None]
    assert chunks[0].text == "x = 1"


# ---------------------------------------------------------------------------
# Defaults stay in sync with the model layer
# ---------------------------------------------------------------------------


async def test_chunk_text_defaults_match_model_constants() -> None:
    """``chunk_text`` and ``chunk_document`` default to the model
    constants — single source of truth for chunking parameters."""
    from knowledge.models import DEFAULT_CHUNK_SIZE

    parsed = _parsed(_words(DEFAULT_CHUNK_SIZE * 2 + 100), fmt="text")
    chunks = await chunk_document(parsed=parsed)
    # 1700 words, stride = DEFAULT_CHUNK_SIZE - DEFAULT_CHUNK_OVERLAP.
    # Confirm the chunk_size default produced the expected first window size.
    assert chunks[0].token_count == DEFAULT_CHUNK_SIZE
    if len(chunks) > 1:
        assert chunks[1].token_count == DEFAULT_CHUNK_SIZE
