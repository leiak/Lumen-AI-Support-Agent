"""Enums for the knowledge base domain."""
from enum import StrEnum


class ArticleStatus(StrEnum):
    """Lifecycle of an Article through the indexing pipeline."""

    DRAFT = "draft"  # not yet submitted for indexing
    INDEXING = "indexing"  # chunker + embedder are running
    INDEXED = "indexed"  # chunks are stored + embedded in Qdrant
    FAILED = "failed"  # last indexing attempt errored; see error_message


class ArticleSourceType(StrEnum):
    """How the article's raw_text was originally captured."""

    UPLOAD = "upload"  # user uploaded a file (PDF, docx, md, ...)
    URL = "url"  # crawled/scraped from a public URL
    MANUAL = "manual"  # typed/pasted in by a human