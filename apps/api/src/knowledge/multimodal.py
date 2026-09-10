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

Public API
----------

* :func:`enrich_blocks` — the ONLY entry point callers should use.
  Dispatches on ``parsed.format`` and pulls any extra raw source bytes
  it needs from ``parsed.metadata['raw_source']``.
* :func:`strip_markdown_blocks` — used by the parser to keep code +
  image spans out of the linearized ``text`` field.

The internal ``_extract_*`` helpers below are private; they are
implementation details of :func:`enrich_blocks` and may change without
notice. The parser (Task 6.4 integration) calls :func:`enrich_blocks`
and never the private helpers directly.
"""
from __future__ import annotations

import re
from functools import cache
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


@cache
def _log_ocr_todo_once() -> None:
    """Log the OCR TODO exactly once per process (Stage 7+ will remove it)."""
    log.info("knowledge.multimodal.ocr_todo", note="image OCR not yet wired (Stage 7+)")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def enrich_blocks(
    *,
    parsed: ParsedDocument,
) -> list[dict[str, object]]:
    """Extract structured blocks from a parsed document.

    The ONLY public entry point for multimodal extraction. Dispatch
    happens on ``parsed.format``:

    * ``"markdown"`` — fenced code blocks + inline image references.
    * ``"html"`` — ``<img>`` (with optional ``<figcaption>`` caption)
      + ``<table>`` rows. Requires ``parsed.metadata['raw_source']`` to
      be the original bytes; the parser populates this field so the
      HTML source is available without an extra decode pass.
    * Anything else (``text`` / ``pdf`` / ``json`` / unknown) returns
      ``[]``.

    Returns a list of block dicts. Each block has at minimum a ``type``
    field; the rest of the shape depends on the type:

    * ``{"type": "code", "language": "python", "text": "..."}``
    * ``{"type": "image", "alt": "...", "src": "..."}``
      (markdown) or ``{"type": "image", "alt": "...", "src": "...",
      "caption": "..."}`` (HTML, caption from ``<figcaption>``).
    * ``{"type": "table", "rows": [[...], [...], ...]}`` (HTML only).

    The function never raises — malformed inputs return what we could
    extract + a warning log.
    """
    fmt = parsed.format
    metadata = parsed.metadata if isinstance(parsed.metadata, dict) else {}

    blocks: list[dict[str, object]]
    if fmt == "markdown":
        # For markdown, ``parsed.text`` already IS the raw source.
        blocks = _extract_markdown_blocks(parsed.text)
    elif fmt == "html":
        # HTML extraction operates on the original bytes, not the
        # linearized text. The parser is responsible for stuffing the
        # raw source into ``metadata['raw_source']``.
        raw_source = metadata.get("raw_source")
        if not isinstance(raw_source, (bytes, str)):
            # Defensive: a caller that bypassed the parser and forgot
            # to attach raw_source gets an empty list rather than a
            # confusing crash.
            log.warning(
                "knowledge.multimodal.html_missing_raw_source",
                format=fmt,
            )
            return []
        blocks = _extract_html_blocks(raw_source)
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
# - Body may be EMPTY (a ``` ``` block with no content is legal).
# - We use a non-anchored pattern; re.findall finds each block in order.
_MD_FENCED_CODE_RE = re.compile(
    r"^[ \t]{0,3}```([^\s`]*)[ \t]*\n"  # opening fence + optional language
    r"(.*?)"  # body (non-greedy, DOTALL via flag below; may be empty)
    r"(?:\n)?[ \t]{0,3}```[ \t]*(?:\n|$)",  # closing fence on its own line
    re.DOTALL | re.MULTILINE,
)

# Inline image: ![alt](src).
#
# We cannot use a single regex for the full image syntax: CommonMark
# allows balanced parens in ``src`` (e.g. ``![x](https://example.com/foo(bar).png)``)
# plus ``\)`` escapes. We split the work in two:
#
#   1. Find each ``![<alt>](`` opener.
#   2. Walk character-by-character from the opening ``(`` to the
#      matching close, respecting paren depth and ``\)`` escapes.
#
# The regex below only locates the opener; :func:`_scan_markdown_image_src`
# does the depth-aware src scan.
_MD_IMAGE_OPEN_RE = re.compile(r"!\[((?:\\.|[^\]\\])*)\]\(")


def _scan_markdown_image_src(text: str, start: int) -> tuple[str, int] | None:
    r"""Return ``(src, end_index)`` for the ``(...)`` body of a markdown image.

    ``start`` is the index immediately AFTER the opening ``(`` of an
    inline image link. Walks forward tracking paren depth, honoring
    ``\)`` escapes, and returns the captured src (without the parens)
    plus the index just past the closing ``)``. Returns ``None`` if
    the link is unterminated or empty.
    """
    depth = 1
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n and text[i + 1] in ("(", ")", "\\"):
            # Skip the escape pair.
            i += 2
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                src = text[start:i]
                return src, i + 1
        i += 1
    # Unterminated — treat as no match.
    return None


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

    # 2. Inline image references. Walk each ``![alt](`` opener and
    #    scan the src with paren-depth awareness.
    try:
        for m in _MD_IMAGE_OPEN_RE.finditer(text):
            alt = m.group(1)
            src_start = m.end()
            scanned = _scan_markdown_image_src(text, src_start)
            if scanned is None:
                # Unterminated link — skip silently. We don't log per
                # occurrence to keep PII safe; malformed input is
                # common and not actionable.
                continue
            src, _end = scanned
            if not src.strip():
                # Empty src yields no anchor for the chunker; skip.
                continue
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
    * For images we walk the ``![alt](src)`` openers (same logic as
      :func:`_extract_markdown_blocks`) and remove the full span,
      again handling balanced parens in the src.
    """
    out = text
    has_code = any(b.get("type") == "code" for b in blocks)
    has_image = any(b.get("type") == "image" for b in blocks)

    if has_code:
        out = _MD_FENCED_CODE_RE.sub("", out)
    if has_image:
        out = _strip_markdown_images(out)
    return out


def _strip_markdown_images(text: str) -> str:
    """Remove ``![alt](src)`` spans from ``text``, handling balanced parens."""
    out_parts: list[str] = []
    cursor = 0
    for m in _MD_IMAGE_OPEN_RE.finditer(text):
        out_parts.append(text[cursor:m.start()])
        src_start = m.end()
        scanned = _scan_markdown_image_src(text, src_start)
        if scanned is None:
            # Unterminated — keep the original opener in the output
            # rather than silently swallowing the rest of the document.
            out_parts.append(text[m.start():src_start])
            cursor = src_start
            continue
        _src, end = scanned
        cursor = end
    out_parts.append(text[cursor:])
    return "".join(out_parts)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


def _extract_html_blocks_from_soup(
    soup: BeautifulSoup,
) -> list[dict[str, object]]:
    """Extract ``<img>`` + ``<table>`` blocks from an EXISTING BeautifulSoup tree.

    Operates on a soup that the caller has already built. We deliberately
    do NOT decompose ``<img>`` or ``<table>`` here — the parser may
    want to decompose them itself when linearizing the text, and a
    second call into ``_extract_html_blocks_from_soup`` (e.g. from a
    future Stage 7+ pipeline) should be idempotent.
    """
    blocks: list[dict[str, object]] = []

    # Drop non-visible content so we never pick up images inside
    # <noscript> or similar. Mirrors the parser's own sanitization step.
    for tag_name in ("script", "style", "noscript"):
        for tag in soup(tag_name):
            tag.decompose()

    # 1. Images. Walk every <img> (not just the top level) so images
    #    nested inside <picture>, <figure>, <a>, etc. are still picked up.
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
            # We also decompose the entire <figure> so that its caption
            # text does NOT double-embed into the linearized text that
            # the parser produces from the same soup.
            parent_figure = img.find_parent("figure")
            if isinstance(parent_figure, Tag):
                cap = parent_figure.find("figcaption")
                if isinstance(cap, Tag):
                    caption_text = cap.get_text(" ", strip=True)
                    if caption_text:
                        block["caption"] = caption_text
                # Drop the figure AND the image itself: the caption is
                # captured on the block, and the image's alt would
                # otherwise leak into the linearized text.
                parent_figure.decompose()
            else:
                # No parent figure — decompose just the <img> so its
                # alt text doesn't double-embed.
                img.decompose()
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
    #    is dropped. Tables are decomposed so their text doesn't
    #    double-embed.
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
                table.decompose()
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(
            "knowledge.multimodal.html_table_failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )

    return blocks


def _extract_html_blocks(source: str | bytes) -> list[dict[str, object]]:
    """Extract ``<img>`` + ``<table>`` blocks from an HTML document.

    Convenience wrapper that builds a BeautifulSoup tree and then
    delegates to :func:`_extract_html_blocks_from_soup`. The parser
    prefers the from-soup variant so it can parse the HTML exactly
    once per ``parse_document`` call.

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
    return _extract_html_blocks_from_soup(soup)
