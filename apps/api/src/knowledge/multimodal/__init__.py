"""Multimodal KB package (Stage 17 / M2.B) + legacy block extraction (M1 / Task 6.4).

This package replaces the previous ``knowledge/multimodal.py`` module file.
The old file's contents (markdown / HTML block extraction helpers) were
relocated to :mod:`knowledge.multimodal.blocks` and are re-exported here
so the existing ``from knowledge.multimodal import enrich_blocks`` /
``from knowledge.multimodal import _extract_html_blocks_from_soup`` call
sites in :mod:`knowledge.parser` + :mod:`knowledge.worker` keep working
unchanged.

Submodules:

* :mod:`knowledge.multimodal.blocks` — Markdown / HTML block extraction
  (M1 Task 6.4 — preserved here so legacy imports keep working).
* :mod:`knowledge.multimodal.storage` — S3-compatible object store (boto3).
* :mod:`knowledge.multimodal.embedder` — Vision embedder + Doubao.
"""
from knowledge.multimodal.blocks import (
    _extract_html_blocks,
    _extract_html_blocks_from_soup,
    _extract_markdown_blocks,
    _log_ocr_todo_once,
    _scan_markdown_image_src,
    _strip_markdown_images,
    enrich_blocks,
    strip_markdown_blocks,
)

__all__ = [
    # Stage 17 / M2.B — multimodal KB foundation
    # (no top-level re-exports yet; callers import from submodules directly)
    # Legacy M1 / Task 6.4 — markdown + HTML block extraction
    "enrich_blocks",
    "strip_markdown_blocks",
]