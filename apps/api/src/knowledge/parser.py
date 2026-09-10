"""Document parsing for the knowledge base ingestion pipeline.

Converts uploaded file bytes (txt / md / html / pdf / json) into a
:class:`ParsedDocument` carrying the plain text + minimal metadata. The
downstream chunker (Task 6.5) and embedder (Task 6.6) consume ``text``
verbatim.

Design constraints
------------------

* **Small dependencies only.** No ``unstructured`` / ``markitdown`` /
  ``pdfplumber`` / ``PyMuPDF``. We deliberately stay on the smallest
  viable libraries: ``beautifulsoup4`` for HTML, ``pypdf`` for PDF,
  regex for Markdown. This keeps install size and cold-start time down
  for the worker process.
* **Never silently corrupt data.** Unknown formats raise
  :class:`UnsupportedDocumentType`. We never return a best-effort empty
  string for unrecognized input — the worker can decide whether to
  mark the article ``FAILED`` with a clear error.
* **PII-safe logs.** Logs carry ``file_name`` + ``format`` +
  ``char_count`` only — never raw content.
* **Async signature.** ``parse_document`` is ``async def`` so the worker
  (Task 6.7) can ``await`` it directly. The heavy parsers run inside
  ``asyncio.to_thread`` so the event loop is never blocked, even for
  large PDFs.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from core.logging import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

# M1-supported source formats. Keep this list in sync with the dispatch
# table in ``_detect_format`` and the private ``_parse_*`` helpers below.
SUPPORTED_FORMATS: frozenset[str] = frozenset({"text", "markdown", "html", "pdf", "json"})

# How many U+FFFD replacement chars are "too many" before we assume a
# byte string isn't really UTF-8 text. ~1% is a generous heuristic that
# still catches a real binary blob (PDF, image header, gzip magic).
_MAX_REPLACEMENT_RATIO = 0.01


class UnsupportedDocumentType(ValueError):  # noqa: N818 - name fixed by M1 spec (Task 6.3)
    """Raised when the parser cannot recognize the document format.

    This is a :class:`ValueError` so generic ``except ValueError`` blocks
    catch it without leaking framework-specific types, but the worker
    pipeline specifically checks for this class to map to a clean
    ``ArticleStatus.FAILED`` + ``error_message`` update.
    """


@dataclass(slots=True)
class ParsedDocument:
    """The parser's output.

    Attributes
    ----------
    text:
        Clean plain text suitable for chunking + embedding. Never None;
        may be empty (e.g. a scanned PDF with no OCR layer).
    blocks:
        Optional structured blocks. For M1 we always emit ``[]``; the
        richer block-stream is a Stage 7+ concern (LangChain
        ``Document`` / ``unstructured`` integration). Kept on the
        dataclass so callers (chunker, UI preview) can already code
        against the future shape.
    metadata:
        Free-form dict. Always contains ``source_format`` and
        ``char_count``. PDFs additionally carry ``page_count``.
    format:
        Canonical source format string (matches a member of
        ``SUPPORTED_FORMATS``).
    """

    text: str
    blocks: list[dict[str, object]] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)
    format: str = ""


# ---------------------------------------------------------------------------
# Public dispatch
# ---------------------------------------------------------------------------


async def parse_document(
    *,
    file_bytes: bytes,
    file_name: str,
    mime_type: str | None = None,
) -> ParsedDocument:
    """Dispatch to the right format parser based on mime_type or file_name suffix.

    Parameters
    ----------
    file_bytes:
        Raw file contents.
    file_name:
        Original filename; used as the primary hint when ``mime_type``
        is missing or uninformative, and also for log context (PII-safe
        when used with file_name only, no content).
    mime_type:
        Optional MIME type as reported by the uploader. Takes
        precedence over the filename suffix when both are present.

    Returns
    -------
    :class:`ParsedDocument` with ``text``, ``metadata``, and ``format``.

    Raises
    ------
    UnsupportedDocumentType
        If the format cannot be determined from ``mime_type`` or
        ``file_name``, and the bytes don't look like UTF-8 text.
    """
    fmt = _detect_format(file_name=file_name, mime_type=mime_type, file_bytes=file_bytes)

    log.info(
        "knowledge.parse.start",
        file_name=file_name,
        mime_type=mime_type,
        detected_format=fmt,
        byte_count=len(file_bytes),
    )

    # Parse off the loop. PDF + HTML can be slow on large files; the
    # trivial text/markdown/json parsers are essentially instant but
    # running them in a thread is free and keeps the dispatch uniform.
    try:
        result = await asyncio.to_thread(_parse_sync, file_bytes, fmt, file_name)
    except UnsupportedDocumentType:
        # Re-raise without wrapping so callers see the original message.
        raise
    except Exception as exc:  # pragma: no cover - defensive
        # The per-format parsers are responsible for their own error
        # shape; this is a last-resort guard so a stray exception never
        # crashes the worker. Re-raise as UnsupportedDocumentType with
        # enough context to debug from logs alone (no raw content).
        log.error(
            "knowledge.parse.unexpected_error",
            file_name=file_name,
            detected_format=fmt,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        raise UnsupportedDocumentType(
            f"Failed to parse {file_name} as {fmt}: {type(exc).__name__}"
        ) from exc

    log.info(
        "knowledge.parse.success",
        file_name=file_name,
        format=result.format,
        char_count=len(result.text),
    )
    return result


# ---------------------------------------------------------------------------
# Sync dispatch (runs in a thread)
# ---------------------------------------------------------------------------


def _parse_sync(file_bytes: bytes, fmt: str, file_name: str) -> ParsedDocument:
    """Synchronous dispatcher; runs inside ``asyncio.to_thread``."""
    if fmt == "text":
        return _parse_text(file_bytes)
    if fmt == "markdown":
        return _parse_markdown(file_bytes)
    if fmt == "html":
        return _parse_html(file_bytes)
    if fmt == "pdf":
        return _parse_pdf(file_bytes)
    if fmt == "json":
        return _parse_json(file_bytes)
    # Defensive — _detect_format should never produce an unknown fmt,
    # but if it does (e.g. someone extends the table without the
    # dispatch) we want a clear failure rather than a silent fallthrough.
    raise UnsupportedDocumentType(
        f"Internal error: no parser wired for format {fmt!r} (file: {file_name})"
    )


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


# Map a lowercase file suffix to a canonical format.
_SUFFIX_TO_FORMAT: dict[str, str] = {
    "txt": "text",
    "md": "markdown",
    "markdown": "markdown",
    "html": "html",
    "htm": "html",
    "pdf": "pdf",
    "json": "json",
}


def _detect_format(file_name: str, mime_type: str | None, file_bytes: bytes) -> str:
    """Return the canonical format string for a file.

    Order of precedence:

    1. ``mime_type`` (when set + recognized).
    2. File suffix (when set + recognized).
    3. UTF-8 text sniff — if the bytes decode with very few
       replacement chars, assume ``text``.
    4. Otherwise raise :class:`UnsupportedDocumentType`.
    """
    if mime_type:
        fmt = _detect_format_from_mime(mime_type)
        if fmt is not None:
            return fmt

    suffix = ""
    if "." in file_name:
        suffix = file_name.rsplit(".", 1)[-1].lower()
    if suffix in _SUFFIX_TO_FORMAT:
        return _SUFFIX_TO_FORMAT[suffix]

    # Last resort: bytes-level sniff for UTF-8 text. This is what
    # catches a plain ``readme`` or ``notes`` with no extension.
    if _looks_like_utf8_text(file_bytes):
        return "text"

    raise UnsupportedDocumentType(
        f"Cannot determine document format from file_name={file_name!r} "
        f"and mime_type={mime_type!r}. Supported formats: "
        f"{sorted(SUPPORTED_FORMATS)}."
    )


def _detect_format_from_mime(mime_type: str) -> str | None:
    """Map a MIME type to a canonical format. Returns None if unrecognized."""
    mt = mime_type.strip().lower()
    if mt.startswith("text/markdown") or mt == "text/x-markdown":
        return "markdown"
    if mt.startswith("text/html") or mt == "application/xhtml+xml":
        return "html"
    if mt == "application/pdf":
        return "pdf"
    if mt == "application/json" or mt.endswith("+json"):
        return "json"
    if mt.startswith("text/"):
        return "text"
    return None


def _looks_like_utf8_text(data: bytes, threshold: float = _MAX_REPLACEMENT_RATIO) -> bool:
    """Heuristic: does ``data`` look like a UTF-8 text document?

    Returns ``False`` for empty bytes (caller should treat that as
    "explicitly empty text" rather than "text" — empty content is a
    valid input for a text file but we don't want to invent a format
    out of nothing).
    """
    if not data:
        return False
    decoded = data.decode("utf-8", errors="replace")
    if not decoded:
        return False
    # A real text file shouldn't have raw NULs; PDFs and other binaries do.
    if "\x00" in decoded:
        return False
    replacement_count = decoded.count("�")
    return (replacement_count / len(decoded)) <= threshold


# ---------------------------------------------------------------------------
# Per-format parsers (all sync — run inside asyncio.to_thread)
# ---------------------------------------------------------------------------


def _parse_text(content: bytes) -> ParsedDocument:
    """Decode UTF-8 with error replacement; nothing else to do for plain text."""
    text = content.decode("utf-8", errors="replace")
    return ParsedDocument(
        text=text,
        blocks=[],
        metadata={"source_format": "text", "char_count": len(text)},
        format="text",
    )


# Markdown → plain text via regex normalization. We deliberately do NOT
# render to HTML (saves a dep + a rendering pass) and we do NOT preserve
# every markdown feature (tables, footnotes, ...) — chunking + embedding
# benefit from linearized prose.
_MD_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
_MD_HEADER_PREFIX_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MD_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
# Blockquote marker at line start: '> ' → ''.
_MD_BLOCKQUOTE_RE = re.compile(r"^>\s*", re.MULTILINE)
# Horizontal rules: ---, ***, ___ on their own line.
_MD_HR_RE = re.compile(r"^\s*([-*_])\s*\1\s*\1[\s\1]*$", re.MULTILINE)


def _parse_markdown(content: bytes) -> ParsedDocument:
    """Convert Markdown to plain text using regex normalization.

    Stripping order matters:

    1. Code fences first (preserves inner text from further mangling).
    2. Headers (keep the text, drop the ``#`` prefix).
    3. Bold/italic (unbalanced markers are left alone — better to keep
       stray ``*`` than to lose real text).
    4. Links ``[text](url)`` → ``text (url)``.
    5. Inline code → keep the text.
    6. Images → drop (alt text would be noise; URLs are not useful in
       embeddings).
    7. Blockquotes + horizontal rules.
    """
    text = content.decode("utf-8", errors="replace")

    # 1. Code fences: keep inner content, drop the ``` markers.
    text = _MD_CODE_FENCE_RE.sub(lambda m: m.group(0).replace("```", ""), text)

    # 2. Headers.
    text = _MD_HEADER_PREFIX_RE.sub("", text)

    # 3. Bold + italic.
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_ITALIC_RE.sub(r"\1", text)

    # 4. Links: [text](url) → text (url)
    text = _MD_LINK_RE.sub(r"\1 (\2)", text)

    # 5. Inline code.
    text = _MD_INLINE_CODE_RE.sub(r"\1", text)

    # 6. Images.
    text = _MD_IMAGE_RE.sub("", text)

    # 7. Blockquotes + horizontal rules.
    text = _MD_BLOCKQUOTE_RE.sub("", text)
    text = _MD_HR_RE.sub("", text)

    text = text.strip()
    return ParsedDocument(
        text=text,
        blocks=[],
        metadata={"source_format": "markdown", "char_count": len(text)},
        format="markdown",
    )


def _parse_html(content: bytes) -> ParsedDocument:
    """Extract visible text from HTML.

    Security: ``<script>`` and ``<style>`` tags (and ``<noscript>``) are
    decomposed entirely before text extraction — we must NEVER carry
    inline JS into the embedding pipeline.
    """
    soup = BeautifulSoup(content, "html.parser")
    # Drop executable / non-visible content. decompose() removes the
    # node AND its text, which is what we want for security.
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    # separator keeps block boundaries visible to the chunker; strip=True
    # removes the noisy leading/trailing whitespace each tag introduces.
    text = soup.get_text(separator="\n", strip=True)
    return ParsedDocument(
        text=text,
        blocks=[],
        metadata={"source_format": "html", "char_count": len(text)},
        format="html",
    )


def _parse_pdf(content: bytes) -> ParsedDocument:
    """Extract text from a PDF, page by page.

    Corrupt or empty PDFs return empty text rather than raising — the
    worker should still create the article version (so the user sees
    a ``FAILED`` status with a clear message rather than a stuck
    ``INDEXING``). The dispatcher catches truly unexpected exceptions
    and surfaces them as :class:`UnsupportedDocumentType`.
    """
    try:
        reader = PdfReader(io.BytesIO(content))
        pages = list(reader.pages)
    except PdfReadError as exc:
        log.warning(
            "knowledge.parse.pdf.corrupt",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return ParsedDocument(
            text="",
            blocks=[],
            metadata={
                "source_format": "pdf",
                "page_count": 0,
                "char_count": 0,
                "corrupt": True,
            },
            format="pdf",
        )

    page_texts: list[str] = []
    for page in pages:
        try:
            page_text = page.extract_text() or ""
        except Exception as exc:
            # Single-page failure shouldn't sink the whole parse —
            # log + skip + continue. Common with scanned PDFs where
            # extract_text() can blow up on a malformed stream.
            log.warning(
                "knowledge.parse.pdf.page_error",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            continue
        if page_text:
            page_texts.append(page_text)

    text = "\n\n".join(page_texts)
    return ParsedDocument(
        text=text,
        blocks=[],
        metadata={
            "source_format": "pdf",
            "page_count": len(pages),
            "char_count": len(text),
        },
        format="pdf",
    )


def _parse_json(content: bytes) -> ParsedDocument:
    """Pretty-print the parsed JSON tree as text.

    The output preserves Unicode (``ensure_ascii=False``) because
    downstream chunking + embedding work better with the original
    characters than with ``\\uXXXX`` escapes.
    """
    obj = json.loads(content.decode("utf-8", errors="replace"))
    text = json.dumps(obj, indent=2, ensure_ascii=False)
    return ParsedDocument(
        text=text,
        blocks=[],
        metadata={"source_format": "json", "char_count": len(text)},
        format="json",
    )
