"""Length-based chunking for the knowledge base ingestion pipeline.

Task 6.5 (M1): split a :class:`knowledge.parser.ParsedDocument` into
:class:`ChunkCandidate` objects ready for embedding + persistence.

Scope (M1 minimal)
------------------

* **Length-based chunking** with configurable window + overlap, taken
  from :class:`knowledge.models.KnowledgeBase` (``chunk_size``,
  ``chunk_overlap``).
* **Structured blocks first.** Each block in
  ``parsed.blocks`` becomes a standalone :class:`ChunkCandidate` (code,
  image, table) so the embedder never splits a code listing across two
  vectors.
* **Linearized text.** Everything in ``parsed.text`` is split into
  overlapping length-based windows.
* **Simple token counting.** ``token_count`` is the whitespace-split
  word count (``len(text.split())``). Real tokenizer (tiktoken) is
  Stage 7+.

Out of scope for M1
-------------------

* Semantic / sentence-aware splitting.
* Recursive / hierarchical chunking.
* tiktoken-grade token counting.

Design constraints
------------------

* **Deterministic.** Same input produces the same output — no
  randomness, no time-of-day influence. This is required so re-indexing
  the same ``article_version`` is a no-op at the chunk layer.
* **PII-safe logs.** Only counts + format are logged; never chunk text.
* **Async signature.** ``chunk_document`` is ``async def`` so the
  worker (Task 6.7) can ``await`` it directly alongside
  ``parse_document``.
* **No new heavy dependencies.** No tiktoken, no nltk. ``str.split()``
  is good enough for M1.

Public API
----------

* :class:`ChunkCandidate` — the value type produced by the chunker.
* :func:`chunk_document` — the only entry point callers should use.
* :func:`chunk_text` — the lower-level length-based splitter. Exposed
  for unit testing and (Stage 7+) for recursive sub-chunking of
  oversized structured blocks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.logging import get_logger

if TYPE_CHECKING:
    from knowledge.parser import ParsedDocument

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ChunkCandidate:
    """A chunk ready for embedding + persistence.

    Attributes
    ----------
    text:
        The chunk's textual content. Empty strings are never emitted.
    block_index:
        0-based index into ``ParsedDocument.blocks`` if this chunk was
        derived from a structured block (code / image / table). ``None``
        for linearized text chunks.
    token_count:
        Whitespace-separated word count (M1 only). For oversized
        sub-chunks that exceed ``chunk_size`` words because a single
        word is too long, this value reflects the actual word count.
    chunk_index:
        0-based position in the emission sequence. Assigned in
        iteration order: blocks first (in ``parsed.blocks`` order),
        then text chunks.
    metadata:
        Free-form dict. Always contains ``source_format`` and
        ``block_type`` (``"code"`` / ``"image"`` / ``"table"`` /
        ``None``). Other fields depend on block type — e.g. tables
        carry a ``row_count``, images may carry ``alt`` / ``src``.
    """

    text: str
    block_index: int | None
    token_count: int
    chunk_index: int
    metadata: dict[str, object]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def chunk_document(
    *,
    parsed: ParsedDocument,
    chunk_size: int = 800,
    chunk_overlap: int = 100,
) -> list[ChunkCandidate]:
    """Split a parsed document into chunk candidates.

    Parameters
    ----------
    parsed:
        Output of :func:`knowledge.parser.parse_document`. Both
        ``text`` and ``blocks`` are honored.
    chunk_size:
        Target window size in **words** (whitespace-split). Must be
        > 0. Note: M1 measures in words, not tokens — real token
        counting is Stage 7+.
    chunk_overlap:
        Number of words repeated at the start of the next window.
        Must be strictly less than ``chunk_size`` so a chunk always
        makes forward progress.

    Returns
    -------
    List of :class:`ChunkCandidate` in emission order:
    blocks (in ``parsed.blocks`` order), then text chunks.

    Raises
    ------
    ValueError
        If ``chunk_size <= 0`` or ``chunk_overlap >= chunk_size``.
    """
    _validate_config(chunk_size=chunk_size, chunk_overlap=chunk_overlap)

    candidates: list[ChunkCandidate] = []
    source_format = str(parsed.metadata.get("source_format", parsed.format))

    # 1. Structured blocks first, in source order. Each block becomes
    #    exactly ONE ChunkCandidate unless its text is larger than
    #    chunk_size, in which case we recursively split it.
    for idx, block in enumerate(parsed.blocks):
        block_chunks = _chunk_block(
            block=block,
            block_index=idx,
            source_format=source_format,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
        candidates.extend(block_chunks)

    # 2. Linearized text — split into overlapping windows. Done last
    #    so block chunks keep their original order at the head of the
    #    candidate list.
    text_chunks = chunk_text(
        text=parsed.text,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        source_format=source_format,
    )
    candidates.extend(text_chunks)

    # 3. Re-stamp chunk_index so the emission order is monotonic
    #    regardless of any sub-chunking that happened in step 1.
    for i, c in enumerate(candidates):
        c.chunk_index = i

    if parsed.text or parsed.blocks:
        log.info(
            "knowledge.chunk.done",
            source_format=source_format,
            block_count=len(parsed.blocks),
            text_word_count=len(parsed.text.split()) if parsed.text else 0,
            chunk_count=len(candidates),
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

    return candidates


def chunk_text(
    *,
    text: str,
    chunk_size: int = 800,
    chunk_overlap: int = 100,
    source_format: str = "",
) -> list[ChunkCandidate]:
    """Split a plain string into overlapping length-based windows.

    Each emitted :class:`ChunkCandidate` has ``block_index = None``
    (linearized text) and ``metadata["block_type"] = None``.

    Word-based counting. The first window is up to ``chunk_size``
    words; subsequent windows start ``chunk_size - chunk_overlap``
    words after the previous window's start and contain up to
    ``chunk_size`` words.

    The function is deterministic — same input always yields the same
    output.

    Parameters
    ----------
    text:
        The text to split. Empty / whitespace-only text returns ``[]``.
    chunk_size:
        Window size in words. Must be > 0.
    chunk_overlap:
        Overlap between consecutive windows in words. Must be
        strictly less than ``chunk_size``.
    source_format:
        Forwarded into ``metadata["source_format"]``.

    Returns
    -------
    List of :class:`ChunkCandidate`. Always deterministic.

    Raises
    ------
    ValueError
        If ``chunk_size <= 0`` or ``chunk_overlap >= chunk_size``.
    """
    _validate_config(chunk_size=chunk_size, chunk_overlap=chunk_overlap)

    if not text or not text.strip():
        return []

    # NOTE: M1 uses whitespace splitting as a stand-in for real token
    # counting. ``str.split()`` collapses runs of whitespace and drops
    # empty strings, which is exactly what we want here — punctuation
    # attaches to its word and newlines/parens become word boundaries.
    words = text.split()
    if not words:
        return []

    stride = chunk_size - chunk_overlap
    candidates: list[ChunkCandidate] = []
    i = 0
    n = len(words)
    while i < n:
        window = words[i : i + chunk_size]
        joined = " ".join(window)
        candidates.append(
            ChunkCandidate(
                text=joined,
                block_index=None,
                token_count=len(window),
                # chunk_index is assigned by the caller (chunk_document);
                # we leave 0 here so it's obvious if anyone forgets.
                chunk_index=0,
                metadata={
                    "source_format": source_format,
                    "block_type": None,
                },
            )
        )
        # If this window didn't reach chunk_size, we've hit the tail
        # and there's no point advancing further — break so we don't
        # emit an empty final window.
        if len(window) < chunk_size:
            break
        i += stride

    return candidates


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _validate_config(*, chunk_size: int, chunk_overlap: int) -> None:
    """Raise ``ValueError`` for invalid (chunk_size, chunk_overlap) pairs.

    M1 invariants:

    * ``chunk_size`` must be > 0.
    * ``chunk_overlap`` must be >= 0 AND strictly less than
      ``chunk_size`` so a chunk always advances by at least one word.
    """
    if chunk_size <= 0:
        raise ValueError(
            f"chunk_size must be > 0, got {chunk_size}"
        )
    if chunk_overlap < 0:
        raise ValueError(
            f"chunk_overlap must be >= 0, got {chunk_overlap}"
        )
    if chunk_overlap >= chunk_size:
        raise ValueError(
            f"chunk_overlap ({chunk_overlap}) must be < chunk_size "
            f"({chunk_size}) so windows always make forward progress"
        )


def _chunk_block(
    *,
    block: dict[str, Any],
    block_index: int,
    source_format: str,
    chunk_size: int,
    chunk_overlap: int,
) -> list[ChunkCandidate]:
    """Build chunk candidate(s) for a single structured block.

    Dispatches on ``block["type"]``:

    * ``"code"`` — ``text = block["text"]``.
    * ``"image"`` — ``text = "Image: {alt} ({src})"`` (possibly with
      a caption appended, when present).
    * ``"table"`` — ``text = linearize_table_rows(block["rows"])``.
    * Anything else — treated as plain text from ``block["text"]`` if
      present.

    If the resulting text exceeds ``chunk_size`` words, it is split
    using :func:`chunk_text` so we don't emit a single oversized
    vector. All sub-chunks preserve the same ``block_index`` so the
    relationship back to ``parsed.blocks[i]`` is intact.

    Empty blocks (zero word count) yield a single chunk with empty
    text — this is intentional so callers can still index "an image
    with no alt" without losing the block anchor.
    """
    block_type = str(block.get("type", ""))
    text = _block_to_text(block, block_type)
    base_metadata: dict[str, object] = {
        "source_format": source_format,
        "block_type": block_type or None,
    }
    # Forward useful, non-PII fields so downstream (embedder, UI
    # preview) can render the block without re-parsing.
    if block_type == "code":
        lang = block.get("language")
        if lang:
            base_metadata["language"] = lang
    elif block_type == "image":
        for key in ("alt", "src", "caption"):
            if key in block:
                base_metadata[key] = block[key]
    elif block_type == "table":
        rows = block.get("rows")
        if isinstance(rows, list):
            base_metadata["row_count"] = len(rows)

    word_count = len(text.split()) if text else 0

    if word_count <= chunk_size:
        # Common path: block fits in one chunk. token_count reflects
        # the actual word count, including 0 for empty blocks.
        return [
            ChunkCandidate(
                text=text,
                block_index=block_index,
                token_count=word_count,
                # chunk_index is reassigned by chunk_document.
                chunk_index=0,
                metadata=dict(base_metadata),
            )
        ]

    # Oversized block — split its text using the same length-based
    # chunker. Sub-chunks all share block_index so a downstream
    # consumer can still navigate back to parsed.blocks[i].
    sub = chunk_text(
        text=text,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        source_format=source_format,
    )
    result: list[ChunkCandidate] = []
    for c in sub:
        result.append(
            ChunkCandidate(
                text=c.text,
                block_index=block_index,
                token_count=c.token_count,
                chunk_index=0,
                metadata=dict(base_metadata),
            )
        )
    return result


def _block_to_text(block: dict[str, Any], block_type: str) -> str:
    """Render a structured block as a string for embedding.

    Code blocks use the body verbatim. Images use a stable
    ``"Image: {alt} ({src})"`` shape (caption appended when present)
    so the embedder has something textual to chew on — M1 doesn't OCR
    image content. Tables join rows by newlines and cells by tabs.
    """
    if block_type == "code":
        text = block.get("text", "")
        return text if isinstance(text, str) else ""

    if block_type == "image":
        alt = block.get("alt", "")
        src = block.get("src", "")
        if not isinstance(alt, str):
            alt = str(alt)
        if not isinstance(src, str):
            src = str(src)
        out = f"Image: {alt} ({src})"
        caption = block.get("caption")
        if isinstance(caption, str) and caption.strip():
            out = f"{out}\n{caption}"
        return out

    if block_type == "table":
        rows = block.get("rows")
        if not isinstance(rows, list):
            return ""
        return linearize_table_rows(rows)

    # Unknown block type — fall back to a text field if present.
    text = block.get("text")
    if isinstance(text, str):
        return text
    return ""


def linearize_table_rows(rows: list[list[str]]) -> str:
    """Render a 2-D table (list of rows of cell strings) as text.

    Rows are joined by newlines; cells within a row are joined by a
    tab so the result is line-oriented (good for embedding) while
    still distinguishing columns visually. Empty rows are skipped.

    Non-string cells are coerced via ``str()``.
    """
    lines: list[str] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        cells = [str(c) for c in row if c is not None]
        if not cells:
            continue
        lines.append("\t".join(cells))
    return "\n".join(lines)
