"""Cross-domain search schemas (Tier 3 Task 3.3).

Returned results are intentionally a single flat shape — the
frontend switches on ``kind`` to render per-domain tiles. Keeping
the shape flat avoids Pydantic discriminated-union ceremony for a
read-only response and lets us add new domains later by just
appending a ``kind`` literal.

PII discipline
--------------

``snippet`` is the literal substring that matched the query,
truncated to 200 chars. We return it so the operator can confirm
"this is the right hit" without clicking through. ``title`` is
``Article.title`` / ``Ticket.subject`` / the matched
``customer_external_id``. The endpoint never returns raw message
bodies or other customer PII — those stay behind per-resource
detail endpoints.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

SearchKind = Literal["article", "ticket", "conversation"]


class SearchResult(BaseModel):
    """One hit in a cross-domain search response."""

    kind: SearchKind
    id: str
    title: str
    subtitle: str | None = None
    snippet: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class SearchResultsOut(BaseModel):
    """Top-level search response envelope."""

    query: str
    total: int
    results: list[SearchResult]


__all__ = ["SearchKind", "SearchResult", "SearchResultsOut"]