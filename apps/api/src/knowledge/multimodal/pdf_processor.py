"""PDF processor: extract text chunks + screenshot key pages.

Strategy (M2.B):
- Text extraction: pdfplumber, page-by-page, chunked by ~500 chars
- Key-page detection: page has embedded images OR contains tables
  (heuristic via pdfplumber.find_tables())
- Screenshots: pdf2image (poppler) -> PNG bytes for each key page

Degrades gracefully on poppler-not-installed: pdf2image raises
PDFInfoNotInstalledError -> caught, warning logged, no screenshots
returned (text extraction still works).

PII contract: log lines MUST NOT include raw PDF text, file bytes,
or exception repr. Allowed fields: ``page``, ``error_type``.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass

import pdfplumber

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExtractedPdf:
    text_chunks: list[str]
    screenshot_pages: list[tuple[int, bytes]]  # (page_number_1_indexed, png_bytes)


class PdfProcessor:
    """Extract text chunks + key-page screenshots from PDF bytes.

    Never raises on processing errors — partial results returned.
    """

    def __init__(self, chunk_size: int = 500, key_page_dpi: int = 150) -> None:
        self._chunk_size = chunk_size
        self._key_page_dpi = key_page_dpi

    def extract(self, pdf_bytes: bytes) -> ExtractedPdf:
        """Extract text chunks + key-page screenshots.

        Never raises; on fatal pdfplumber error returns partial result
        (text_chunks + screenshots collected so far).
        """
        text_chunks: list[str] = []
        screenshot_pages: list[tuple[int, bytes]] = []

        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page_idx, page in enumerate(pdf.pages, start=1):
                    # 1. Text extraction + chunking
                    try:
                        page_text = (page.extract_text() or "").strip()
                    except Exception as e:
                        logger.warning(
                            "pdf.text_extract_failed",
                            extra={
                                "page": page_idx,
                                "error_type": type(e).__name__,
                            },
                        )
                        page_text = ""

                    if page_text:
                        for i in range(0, len(page_text), self._chunk_size):
                            chunk = page_text[i : i + self._chunk_size]
                            if chunk.strip():
                                text_chunks.append(chunk)

                    # 2. Key-page detection (images OR tables)
                    try:
                        has_image = bool(page.images)
                        has_table = bool(page.find_tables())
                        is_key_page = has_image or has_table
                    except Exception as e:
                        logger.warning(
                            "pdf.keypage_detection_failed",
                            extra={
                                "page": page_idx,
                                "error_type": type(e).__name__,
                            },
                        )
                        is_key_page = False

                    if is_key_page:
                        png_bytes = self._screenshot_page(pdf_bytes, page_idx)
                        if png_bytes is not None:
                            screenshot_pages.append((page_idx, png_bytes))
        except Exception as e:
            logger.warning(
                "pdf.extract_failed",
                extra={"error_type": type(e).__name__},
            )
            # Return partial result

        return ExtractedPdf(text_chunks=text_chunks, screenshot_pages=screenshot_pages)

    def _screenshot_page(self, pdf_bytes: bytes, page_num_1_indexed: int) -> bytes | None:
        """Render one page to PNG via pdf2image (poppler).

        Returns None on failure (poppler missing, page out of range, etc.).
        """
        try:
            from pdf2image import convert_from_bytes

            images = convert_from_bytes(
                pdf_bytes,
                first_page=page_num_1_indexed,
                last_page=page_num_1_indexed,
                dpi=self._key_page_dpi,
            )
            if not images:
                return None
            buf = io.BytesIO()
            images[0].save(buf, format="PNG")
            return buf.getvalue()
        except Exception as e:
            # PDFInfoNotInstalledError, Image.open issues, etc.
            logger.warning(
                "pdf.screenshot_failed",
                extra={
                    "page": page_num_1_indexed,
                    "error_type": type(e).__name__,
                },
            )
            return None
