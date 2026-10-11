"""Cross-domain search service (Tier 3 Task 3.3).

Searches across three tenant-scoped domains in parallel:

* ``article``       — title ``ILIKE`` + latest version ``raw_text`` ``ILIKE``
* ``ticket``        — ``subject`` ``ILIKE``
* ``conversation``  — ``id`` / ``customer_external_id`` ``ILIKE``

Design choices
--------------

* **Per-domain cap, not global**. We split the requested ``limit``
  across the three domains (min 3 each when ``limit >= 9``). This
  keeps any single tenant sub-query cheap and avoids one domain
  starving the others.
* **No vector search**. KB vector search is reserved for the
  runtime RAG path. Admin search is for the *operator* finding a
  known record — ILIKE on indexed text columns is fast and gives
  them exact-match semantics they expect from a control panel.
* **Tenant isolation**. Every query carries ``tenant_id`` (from
  ``require_admin``). Anti-enumeration: cross-tenant matches
  don't appear.

PII discipline
--------------

``snippet`` is the matched substring, truncated to 200 chars. We
do NOT include raw message bodies (those are behind the conversation
detail endpoint). ``customer_external_id`` is the tenant's own
opaque ID for the customer — safe to surface in admin search.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.schemas.search import SearchResult, SearchResultsOut
from conversation.models import Conversation
from knowledge.models import Article, ArticleVersion
from ticket.enums import TicketStatus
from ticket.models import Ticket

# Hard cap on snippet length so we never leak a giant body to the UI.
_SNIPPET_MAX = 200

# Minimum per-domain cap when ``limit >= 9`` so a domain never
# gets 0 hits just because the operator asked for "few".
_PER_DOMAIN_MIN = 3


@dataclass(slots=True)
class _DomainLimits:
    article: int
    ticket: int
    conversation: int


def _split_limit(limit: int) -> _DomainLimits:
    """Split a global ``limit`` into per-domain caps.

    Divides by 3 with a min of ``_PER_DOMAIN_MIN`` when the global
    limit is large enough. Falls back to a 1/1/1 split for tiny
    requests so we still return at least one hit per domain on
    an exact match.
    """
    per = max(limit // 3, 1)
    if limit >= 9:
        per = max(per, _PER_DOMAIN_MIN)
    return _DomainLimits(article=per, ticket=per, conversation=per)


def _truncate(text: str, *, around: str, length: int = _SNIPPET_MAX) -> str:
    """Return ``text`` truncated to ``length``, centred on ``around``.

    If ``around`` isn't found we just take the head. We never
    raise — the caller passes user-supplied query strings and we
    don't want a malformed LIKE needle to break the search.
    """
    if not text:
        return ""
    if len(text) <= length:
        return text
    idx = text.lower().find(around.lower())
    if idx < 0:
        return text[:length]
    half = length // 2
    start = max(0, idx - half)
    end = min(len(text), start + length)
    return text[start:end]


class AdminSearchService:
    """Tenant-scoped cross-domain search."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def search(
        self, *, tenant_id: str, query: str, limit: int = 10
    ) -> SearchResultsOut:
        """Run the search and return grouped results."""
        needle = query.strip()
        if not needle:
            return SearchResultsOut(query=needle, total=0, results=[])

        caps = _split_limit(limit)
        results: list[SearchResult] = []

        article_q = await self._search_articles(
            tenant_id=tenant_id, needle=needle, limit=caps.article
        )
        results.extend(article_q)

        ticket_q = await self._search_tickets(
            tenant_id=tenant_id, needle=needle, limit=caps.ticket
        )
        results.extend(ticket_q)

        conversation_q = await self._search_conversations(
            tenant_id=tenant_id, needle=needle, limit=caps.conversation
        )
        results.extend(conversation_q)

        return SearchResultsOut(query=needle, total=len(results), results=results)

    async def _search_articles(
        self, *, tenant_id: str, needle: str, limit: int
    ) -> list[SearchResult]:
        """Title + latest-version raw_text ILIKE."""
        # Use DISTINCT to keep the row count sane if the same
        # article title + raw_text both match (it always will).
        title_match = func.lower(Article.title).contains(needle.lower())
        # We join to ArticleVersion to also match body text. There
        # can be N versions per article — we filter to the *current*
        # version so we don't return the same article N times.
        current_version_match = (
            func.lower(ArticleVersion.raw_text).contains(needle.lower())
        ) & (ArticleVersion.id == Article.current_version_id)

        stmt = (
            select(Article, ArticleVersion.raw_text)
            .join(
                ArticleVersion,
                ArticleVersion.id == Article.current_version_id,
                isouter=True,
            )
            .where(
                Article.tenant_id == tenant_id,
                or_(title_match, current_version_match),
            )
            .order_by(Article.updated_at.desc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        out: list[SearchResult] = []
        for article, raw_text in rows:
            snippet = _truncate(raw_text or "", around=needle) if raw_text else None
            out.append(
                SearchResult(
                    kind="article",
                    id=article.id,
                    title=article.title,
                    subtitle=f"KB · {article.status}",
                    snippet=snippet,
                    extra={"knowledge_base_id": article.knowledge_base_id},
                )
            )
        return out

    async def _search_tickets(
        self, *, tenant_id: str, needle: str, limit: int
    ) -> list[SearchResult]:
        """Ticket.subject ILIKE."""
        stmt = (
            select(Ticket)
            .where(
                Ticket.tenant_id == tenant_id,
                func.lower(Ticket.subject).contains(needle.lower()),
            )
            .order_by(Ticket.updated_at.desc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        out: list[SearchResult] = []
        for ticket in rows:
            status_value = (
                ticket.status.value
                if isinstance(ticket.status, TicketStatus)
                else str(ticket.status)
            )
            priority_value = (
                ticket.priority.value
                if hasattr(ticket.priority, "value")
                else str(ticket.priority)
            )
            out.append(
                SearchResult(
                    kind="ticket",
                    id=ticket.id,
                    title=ticket.subject,
                    subtitle=f"{status_value} · {priority_value}",
                    snippet=_truncate(ticket.subject, around=needle),
                    extra={"status": status_value, "priority": priority_value},
                )
            )
        return out

    async def _search_conversations(
        self, *, tenant_id: str, needle: str, limit: int
    ) -> list[SearchResult]:
        """Conversation id + customer_external_id ILIKE."""
        # Mirror the conversation inbox filter so the UX is
        # consistent — an operator who's been using the inbox q box
        # shouldn't suddenly get a different result set here.
        needle_lc = needle.lower()
        stmt = (
            select(Conversation)
            .where(
                Conversation.tenant_id == tenant_id,
                or_(
                    func.lower(Conversation.id).contains(needle_lc),
                    func.lower(Conversation.customer_external_id).contains(needle_lc),
                ),
            )
            .order_by(Conversation.last_activity_at.desc().nullslast())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        out: list[SearchResult] = []
        for conv in rows:
            status_value = (
                conv.status.value
                if hasattr(conv.status, "value")
                else str(conv.status)
            )
            out.append(
                SearchResult(
                    kind="conversation",
                    id=conv.id,
                    title=conv.customer_external_id or conv.id,
                    subtitle=f"{status_value}",
                    snippet=_truncate(
                        conv.customer_external_id or "", around=needle
                    ),
                    extra={"status": status_value},
                )
            )
        return out


__all__ = ["AdminSearchService"]