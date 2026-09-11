"""Pydantic request/response schemas for the KnowledgeBase + Article API.

Validation lives at TWO layers:

* **Schema layer (here)** — field-level shape, types, length limits,
  regexes. The API raises 422 on any violation.
* **Service layer** — cross-field invariants (e.g. ``chunk_overlap <
  chunk_size``) + tenant-scoped uniqueness checks. The API
  translates ``ValueError`` raised here into a 4xx response.

PII discipline
--------------

No field carries PII by design. ``raw_text`` is the article body —
the schema accepts it on input but does NOT echo it back in
``ArticleOut``. Use ``GET /articles/{id}`` + the version payload
(:class:`ArticleVersionOut`) to read it back; the spec explicitly
defines that the version row carries the bytes.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field, field_validator

from knowledge.enums import ArticleSourceType, ArticleStatus

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# URL-safe identifier regex. Lowercase alnum + dash, must start with
# an alphanumeric (no leading dash, no spaces, no uppercase). Length
# caps come from the model column (``String(200)``); we tighten to 100
# in the schema for snappier URLs.
_SLUG_REGEX = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")

# Hard cap on raw_text at the HTTP layer. Smaller than
# ``knowledge.parser.MAX_PARSE_BYTES`` (50 MiB) to keep the DoS
# surface tight at the API edge — a 10 MB blob hitting the parser
# is not the same threat model as a 10 MB JSON request hitting
# FastAPI. Stage 6.10's file upload endpoint will enforce its own
# limit on top of this one.
_MAX_RAW_TEXT_BYTES = 10 * 1024 * 1024  # 10 MiB

# Soft cap on the listing endpoints. M1 doesn't paginate; 100 is
# generous for a typical tenant and matches the repo default.
_MAX_LIST_LIMIT = 100


# ---------------------------------------------------------------------------
# KnowledgeBase schemas
# ---------------------------------------------------------------------------


class KnowledgeBaseOut(BaseModel):
    """Public KnowledgeBase representation."""

    id: str
    tenant_id: str
    name: str
    slug: str
    description: str | None
    embedding_model: str
    chunk_size: int
    chunk_overlap: int
    created_at: datetime
    updated_at: datetime


class KnowledgeBaseCreateIn(BaseModel):
    """POST /knowledge-bases body."""

    name: Annotated[str, Field(min_length=1, max_length=200)]
    slug: Annotated[str, Field(min_length=1, max_length=100)]
    description: str | None = Field(default=None, max_length=2000)
    embedding_model: str | None = Field(default=None, max_length=100)
    chunk_size: int | None = Field(default=None, ge=1, le=4000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=4000)

    @field_validator("slug")
    @classmethod
    def _slug_shape(cls, v: str) -> str:
        """Lowercase alnum + dash; must start with an alphanumeric.

        The service layer also validates this (defence in depth) — the
        schema check gives a fast 422 at the API edge with a clear
        error path.
        """
        if not _SLUG_REGEX.match(v):
            raise ValueError(
                "slug must match ^[a-z0-9][a-z0-9-]{0,99}$ "
                "(lowercase alnum + dash; no leading dash)"
            )
        return v

    @field_validator("chunk_overlap")
    @classmethod
    def _overlap_less_than_size(cls, v: int | None, info: Any) -> int | None:
        """``chunk_overlap`` must be strictly less than ``chunk_size``.

        ``field_validator`` doesn't natively express cross-field
        constraints in Pydantic v2 without ``model_validator``; we
        keep this as a safety net. The service layer re-validates
        and is the authoritative source of truth.
        """
        # Imported here to avoid a circular at module load.
        # NOTE: ``info.data`` is the standard Pydantic v2 mechanism for
        # reading sibling fields inside ``field_validator``. Returning
        # ``v`` unchanged keeps the value flowing through normally.
        if v is None:
            return v
        size = info.data.get("chunk_size") if hasattr(info, "data") else None
        if size is not None and v >= size:
            raise ValueError("chunk_overlap must be strictly less than chunk_size")
        return v


class KnowledgeBaseUpdateIn(BaseModel):
    """PATCH /knowledge-bases/{id} body. Slug is NOT mutable."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    chunk_size: int | None = Field(default=None, ge=1, le=4000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=4000)

    @field_validator("chunk_overlap")
    @classmethod
    def _overlap_less_than_size(cls, v: int | None, info: Any) -> int | None:
        if v is None:
            return v
        size = info.data.get("chunk_size") if hasattr(info, "data") else None
        if size is not None and v >= size:
            raise ValueError("chunk_overlap must be strictly less than chunk_size")
        return v


class KnowledgeBaseListOut(BaseModel):
    """GET /knowledge-bases response envelope."""

    items: list[KnowledgeBaseOut]


# ---------------------------------------------------------------------------
# Article schemas
# ---------------------------------------------------------------------------


class ArticleVersionOut(BaseModel):
    """Hydrated ArticleVersion details — returned alongside an Article GET.

    The API does NOT echo ``raw_text`` in list responses. This is the
    only place a caller can read the bytes back; the version row
    carries the canonical snapshot.
    """

    id: str
    article_id: str
    version_number: int
    content_hash: str
    created_at: datetime
    raw_text: str


class ArticleOut(BaseModel):
    """Public Article representation (no version body inline).

    For the version body, see :class:`ArticleWithVersionOut` — the
    single-article GET endpoint returns that richer shape so the
    caller doesn't have to make a second request.
    """

    id: str
    tenant_id: str
    knowledge_base_id: str
    title: str
    source_type: ArticleSourceType
    source_uri: str | None
    status: ArticleStatus
    current_version_id: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class ArticleWithVersionOut(ArticleOut):
    """``ArticleOut`` + the hydrated current ArticleVersion.

    Returned by ``GET /articles/{id}`` so the caller gets the full
    picture (status + body) in one round-trip. ``version`` is
    ``None`` when the article has no current version (shouldn't
    happen in practice — every Article is created with v1 — but
    kept defensive for forward-compat with a future "article in
    DRAFT, no version yet" lifecycle).
    """

    version: ArticleVersionOut | None = None


class ArticleCreateIn(BaseModel):
    """POST /knowledge-bases/{id}/articles body."""

    title: Annotated[str, Field(min_length=1, max_length=500)]
    source_type: ArticleSourceType
    source_uri: str | None = Field(default=None, max_length=2000)
    raw_text: str = Field(..., min_length=1)

    @field_validator("raw_text")
    @classmethod
    def _raw_text_size(cls, v: str) -> str:
        """Cap raw_text at 10 MB at the HTTP layer.

        Smaller than ``knowledge.parser.MAX_PARSE_BYTES`` to keep
        the FastAPI request-validation path fast. Stage 6.10's file
        upload endpoint will enforce its own limit on top of this
        one.
        """
        if len(v.encode("utf-8")) > _MAX_RAW_TEXT_BYTES:
            raise ValueError(
                f"raw_text exceeds the {_MAX_RAW_TEXT_BYTES}-byte HTTP limit "
                f"({_MAX_RAW_TEXT_BYTES // (1024 * 1024)} MiB); use the file "
                "upload endpoint for larger documents"
            )
        return v


class ArticleUpdateIn(BaseModel):
    """PATCH /articles/{id} body. raw_text is NOT mutable here."""

    title: str | None = Field(default=None, min_length=1, max_length=500)
    source_uri: str | None = Field(default=None, max_length=2000)


class ArticleListOut(BaseModel):
    """GET /articles response envelope."""

    items: list[ArticleOut]


# ---------------------------------------------------------------------------
# Reindex
# ---------------------------------------------------------------------------


class ReindexRequestIn(BaseModel):
    """POST /articles/{id}/reindex body."""

    force: bool = False


class ReindexResultOut(BaseModel):
    """POST /articles/{id}/reindex response. Mirrors :class:`knowledge.worker.ReindexResult`."""

    article_id: str
    skipped: bool
    version_number: int
    status: ArticleStatus
    chunks_indexed: int


__all__ = [
    "ArticleCreateIn",
    "ArticleListOut",
    "ArticleOut",
    "ArticleUpdateIn",
    "ArticleVersionOut",
    "ArticleWithVersionOut",
    "KnowledgeBaseCreateIn",
    "KnowledgeBaseListOut",
    "KnowledgeBaseOut",
    "KnowledgeBaseUpdateIn",
    "ReindexRequestIn",
    "ReindexResultOut",
]