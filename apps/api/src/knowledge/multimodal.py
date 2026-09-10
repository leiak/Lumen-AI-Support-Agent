"""Multimodal block extraction for parsed knowledge-base documents.

Task 6.4 (M1): enrich a :class:`ParsedDocument` with structured blocks so
the chunker (Task 6.5) can keep code, images, and tables as standalone
chunks instead of mangling them into linearized prose.

Scope (M1 minimal)
------------------

* **Markdown** — fenced code blocks (with language tag) and inline-image
  references.
* **HTML** — ``<img>`` tags (with optional ``<figcaption>`` from the
  parent ``<figure>``) and ``<table>`` rows.
* **PDF / JSON / Plain text** — no extraction; returns ``[]``.

Out of scope for M1
-------------------

* **Image OCR.** Image files are not yet supported by the parser (see
  Task 6.3); the moment we ingest an actual ``.png`` / ``.jpg`` is
  Stage 7+. A TODO is logged below so future work is obvious.
* **HTML sanitization for image URLs.** HTML ``<img>`` ``src`` attributes
  could leak external URLs into embeddings. The current HTML parser
  already strips ``<script>`` / ``<style>`` / ``<noscript>``; for M1 we
  accept the leak and log a one-shot warning so it is visible in the
  worker logs. Future work: route through a sanitizer or whitelist.

Robustness contract
-------------------

* Malformed inputs MUST NOT crash. A markdown file with unbalanced
  backticks, or a partially-broken HTML fragment, returns whatever we
  could extract + a warning log (PII-safe: count + types only).
* Block ``text`` content is preserved EXACTLY — no line-ending
  normalization, no whitespace trimming inside a code block.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup, FeatureNotFound
from bs4.element import Tag

from core.logging import get_logger

if TYPE_CHECKING:
    from knowledge.parser import ParsedDocument

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# TODO markers
# ---------------------------------------------------------------------------

# TODO(stage-7): OCR + image-file ingestion. When Task 6.7 wires the
# worker for real .png/.jpg uploads, this module needs an extractor that
# calls e.g. ``pypdfium2`` for embedded images, or a vision-model call
# for standalone images. For M1 we only extract *references* (markdown
# ``![]()`` + HTML ``<img>``).
_OCR_TODO_LOGGED = False


def _log_ocr_todo_once() -> None:
    global _OCR_TODO_LOGGED
    if _OCR_TODO_LOGGED:
        return
    log.info("knowledge.multimodal.ocr_todo", note="image OCR not yet wired (Stage 7+)")
    _OCR_TODO_LOGGED = True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def enrich_blocks(
    *,
    parsed: ParsedDocument,
) -> list[dict[str, object]]:
    """Extract structured blocks from a parsed document.

    Returns a list of block dicts. Each block has at minimum a ``type``
    field; the rest of the shape depends on the type:

    * ``{"type": "code", "language": "python", "text": "..."}``
    * ``{"type": "image", "alt": "...", "src": "..."}``
      (markdown) or ``{"type": "image", "alt": "...", "src": "...",
      "caption": "..."}`` (HTML, caption from ``<figcaption>``).
    * ``{"type": "table", "rows": [[...], [...], ...]}`` (HTML only).

    For M1, only Markdown fenced code blocks + inline images, and HTML
    ``<img>`` + ``<table>`` elements, are extracted. PDF / JSON / plain
    text always return ``[]``.

    The function never raises — malformed inputs return what we could
    extract + a warning log.
    """
    fmt = parsed.format
    text = parsed.text
    metadata = parsed.metadata

    blocks: list[dict[str, object]]
    if fmt == "markdown":
        blocks = _extract_markdown_blocks(text)
    elif fmt == "html":
        # HTML extraction operates on the raw bytes (we need the tags
        # themselves, not the linearized text). Pull source from
        # metadata when present; fall back to re-using the text if not.
        source_html = metadata.get("raw_source") if isinstance(metadata, dict) else None
        if not isinstance(source_html, (str, bytes)):
            source_html = text
        blocks = _extract_html_blocks(source_html)
    else:
        # text / pdf / json / unknown → no multimodal extraction in M1
        return []

    if any(b.get("type") == "image" for b in blocks):
        # TODO(stage-7): remove this once image OCR is wired. The leak
        # of image URLs into embedding text is the reason the parser's
        # HTML stage still emits alt text today; once we sanitize, drop
        # this log.
        _log_ocr_todo_once()

    log.info(
        "knowledge.multimodal.enriched",
        format=fmt,
        block_count=len(blocks),
        block_types=sorted({str(b.get("type")) for b in blocks}),
    )
    return blocks


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

# Fenced code block: ``` optional-language \n body \n ```
# - Opening fence on its own line; optional language token right after ```.
# - Closing fence: ``` on its own line (allow trailing whitespace).
# - Non-greedy body capture.
# - We use a non-anchored pattern; re.findall finds each block in order.
_MD_FENCED_CODE_RE = re.compile(
    r"^[ \t]{0,3}```([^\s`]*)[ \t]*\n"  # opening fence + optional language
    r"(.*?)"  # body (non-greedy, DOTALL via flag below)
    r"\n[ \t]{0,3}```[ \t]*(?:\n|$)",  # closing fence on its own line
    re.DOTALL | re.MULTILINE,
)

# Inline image: ![alt](src). ``src`` may contain balanced parens
# (``http://example.com/foo(bar)`` is legal in CommonMark), so we match
# lazily and stop at the FIRST unescaped ``)``.
# Alt text may be empty (``![]()``) but cannot contain ``]``.
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def _extract_markdown_blocks(text: str) -> list[dict[str, object]]:
    """Extract fenced code blocks + inline images from Markdown text."""
    blocks: list[dict[str, object]] = []

    # 1. Fenced code blocks. Iterate matches in order; each match has
    #    language + body groups. Anything we can't parse we silently
    #    skip — unbalanced backticks won't match this regex.
    try:
        for lang, body in _MD_FENCED_CODE_RE.findall(text):
            language = lang.strip() or ""  # empty string is valid
            blocks.append(
                {
                    "type": "code",
                    "language": language,
                    "text": body,
                }
            )
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(
            "knowledge.multimodal.markdown_code_failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )

    # 2. Inline image references. ``re.findall`` returns tuples of
    #    (alt, src) for our two capture groups.
    try:
        for alt, src in _MD_IMAGE_RE.findall(text):
            blocks.append(
                {
                    "type": "image",
                    "alt": alt,
                    "src": src,
                }
            )
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(
            "knowledge.multimodal.markdown_image_failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )

    return blocks


def strip_markdown_blocks(
    text: str,
    blocks: list[dict[str, object]],
) -> str:
    """Remove code-block + image spans from a Markdown text body.

    Used by :mod:`knowledge.parser` to keep the parser's ``text`` field
    free of multimodal content (which lives on the ``blocks`` list).
    Only blocks whose ``type`` is ``"code"`` or ``"image"`` are
    stripped; everything else is left alone.

    Strategy
    --------

    * For each code block we re-run the fence regex over the text and
      remove every matched span (including fences + trailing newline).
      We do NOT key off the captured body because the body may appear
      in multiple blocks (identical snippets) and we want to remove
      each occurrence exactly once.
    * For images we substitute the original markdown syntax with an
      empty string. We re-derive the regex (with the same flags) so the
      removal matches what was extracted.
    """
    out = text
    has_code = any(b.get("type") == "code" for b in blocks)
    has_image = any(b.get("type") == "image" for b in blocks)

    if has_code:
        out = _MD_FENCED_CODE_RE.sub("", out)
    if has_image:
        out = _MD_IMAGE_RE.sub("", out)
    return out


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


def _extract_html_blocks(source: str | bytes) -> list[dict[str, object]]:
    """Extract ``<img>`` + ``<table>`` blocks from an HTML document.

    Defensive: malformed HTML returns ``[]`` + a warning rather than
    raising. BeautifulSoup is generally robust but a binary garbage
    payload can still blow up in obscure ways.
    """
    try:
        soup = BeautifulSoup(source, "html.parser")
    except FeatureNotFound as exc:
        log.warning(
            "knowledge.multimodal.html_parser_missing",
            error_type=type(exc).__name__,
        )
        return []
    except Exception as exc:
        log.warning(
            "knowledge.multimodal.html_parse_failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return []

    blocks: list[dict[str, object]] = []

    # Drop non-visible content so we never pick up images inside
    # <noscript> or similar. Mirrors the parser's own sanitization step.
    for tag_name in ("script", "style", "noscript"):
        for tag in soup(tag_name):
            tag.decompose()

    # 1. Images. Walk every <img> (not just the top level) so images
    #    nested inside <picture>, <figure>, etc. are still picked up.
    try:
        for img in soup.find_all("img"):
            if not isinstance(img, Tag):
                continue
            alt = img.get("alt")
            src = img.get("src")
            if src is None:
                # Skip images with no source — there's nothing for the
                # chunker/embedder to anchor on, and a missing src is
                # almost always a templating artifact.
                continue
            block: dict[str, object] = {
                "type": "image",
                "alt": "" if alt is None else str(alt),
                "src": str(src),
            }
            # Optional caption from a parent <figure>'s <figcaption>.
            parent_figure = img.find_parent("figure")
            if isinstance(parent_figure, Tag):
                cap = parent_figure.find("figcaption")
                if isinstance(cap, Tag):
                    caption_text = cap.get_text(" ", strip=True)
                    if caption_text:
                        block["caption"] = caption_text
            blocks.append(block)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(
            "knowledge.multimodal.html_image_failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )

    # 2. Tables. Best-effort row extraction: rows = list of lists of
    #    strings. We don't try to recover ``<thead>`` / ``<tbody>`` —
    #    that's a Stage 7+ concern. An empty row or one with no cells
    #    is dropped.
    try:
        for table in soup.find_all("table"):
            if not isinstance(table, Tag):
                continue
            rows: list[list[str]] = []
            for tr in table.find_all("tr"):
                if not isinstance(tr, Tag):
                    continue
                cells = [
                    cell.get_text(" ", strip=True)
                    for cell in tr.find_all(["th", "td"])
                    if isinstance(cell, Tag)
                ]
                if cells:
                    rows.append(cells)
            if rows:
                blocks.append({"type": "table", "rows": rows})
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(
            "knowledge.multimodal.html_table_failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )

    return blocks
