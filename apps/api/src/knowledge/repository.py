"""Repository layer for KnowledgeBase + Article + ArticleVersion.

Each method opens its own short-lived session via :func:`core.database.get_session`.
This mirrors the per-method pattern used in
``conversation/repository.py`` and ``channel/repository.py``.

Tenant isolation note
---------------------

The repository takes ``tenant_id`` as a parameter on every read so the
WHERE clause can be expressed in one place. Tenant isolation is NOT
enforced here — the **service** layer (``knowledge.service``) is the
gatekeeper, returning ``None`` for cross-tenant access. Keeping the
repo permissive means future admin tooling (e.g. a "list every KB
across all tenants" super-user view) can reuse the same queries
without rewriting them.

PII discipline
--------------

Logs and exception messages carry only opaque IDs (ULIDs), status
names, and the exception class name. NEVER ``raw_text``, ``name``, or
``slug`` — the latter two can carry tenant-meaningful identifiers
that an operator log-search might leak.
"""
from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.database import get_session
from core.id_gen import new_id
from knowledge.enums import ArticleSourceType, ArticleStatus
from knowledge.models import (
    Article,
    ArticleVersion,
    KnowledgeBase,
)

# Default values for a fresh KnowledgeBase. Kept here so the repo
# owns its own persistence defaults — the service layer never needs
# to know them. Matches the model defaults (``models.py``) and the
# M1 spec.
_DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
_DEFAULT_CHUNK_SIZE = 800
_DEFAULT_CHUNK_OVERLAP = 100


def _sha256_hex(text: str) -> str:
    """sha256 hex digest of utf-8 encoded ``text``."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class KnowledgeBaseRepository:
    """CRUD for KnowledgeBase rows.

    Tenant isolation is the SERVICE layer's responsibility; this repo
    accepts ``tenant_id`` only as a convenience filter parameter so
    call sites can express "find this row IF it belongs to this
    tenant" in one query.
    """

    async def create(
        self,
        *,
        tenant_id: str,
        name: str,
        slug: str,
        description: str | None = None,
        embedding_model: str = _DEFAULT_EMBEDDING_MODEL,
        chunk_size: int = _DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = _DEFAULT_CHUNK_OVERLAP,
    ) -> KnowledgeBase:
        """Insert a new KnowledgeBase. Raises ``IntegrityError`` on slug conflict.

        The unique constraint ``uq_knowledge_bases_tenant_slug`` (see
        ``knowledge.models.KnowledgeBase.__table_args__``) prevents
        two KBs with the same slug within one tenant. The caller
        (service) is responsible for translating that error into a
        409 — this layer only surfaces the raw DB exception.
        """
        kb = KnowledgeBase(
            id=new_id(),
            tenant_id=tenant_id,
            name=name,
            slug=slug,
            description=description,
            embedding_model=embedding_model,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
        async with get_session() as session:
            session.add(kb)
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                raise
            await session.refresh(kb)
            await session.commit()
            return kb

    async def get_by_id(
        self, *, tenant_id: str, kb_id: str
    ) -> KnowledgeBase | None:
        """Look up a KB scoped to ``tenant_id``. Returns ``None`` if not visible.

        A cross-tenant lookup returns ``None`` (same shape as
        "missing"). The service layer relies on this — its
        ``get_kb`` does NOT do an additional cross-tenant check
        because the WHERE clause already enforces tenant scoping.
        """
        async with get_session() as session:
            stmt = select(KnowledgeBase).where(
                KnowledgeBase.id == kb_id,
                KnowledgeBase.tenant_id == tenant_id,
            )
            return (await session.execute(stmt)).scalar_one_or_none()

    async def list_by_tenant(
        self, *, tenant_id: str, limit: int = 100
    ) -> list[KnowledgeBase]:
        """List KBs for a tenant, newest-first by ``created_at``.

        Hard cap of 100 — pagination is out of scope for M1. The
        service layer further clamps this if needed; the repo
        default is just defensive.
        """
        async with get_session() as session:
            stmt = (
                select(KnowledgeBase)
                .where(KnowledgeBase.tenant_id == tenant_id)
                .order_by(KnowledgeBase.created_at.desc())
                .limit(limit)
            )
            return list((await session.execute(stmt)).scalars().all())

    async def update(
        self,
        *,
        kb: KnowledgeBase,
        name: str | None = None,
        description: str | None = None,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
    ) -> KnowledgeBase:
        """Apply the supplied patches and persist.

        Loads the row by primary key so detached-instance edits don't
        silently no-op (matches the ConversationRepository.update
        pattern). Slug is NOT mutable — the PATCH endpoint in
        ``api.py`` rejects slug changes before they reach here.
        """
        async with get_session() as session:
            existing = await session.get(KnowledgeBase, kb.id)
            if existing is None:
                # Row vanished between the read and the update —
                # surface as a missing row (caller should treat
                # as 404). We don't raise; the service converts.
                return kb  # unchanged; caller can detect via refresh
            if name is not None:
                existing.name = name
            if description is not None:
                existing.description = description
            if chunk_size is not None:
                existing.chunk_size = chunk_size
            if chunk_overlap is not None:
                existing.chunk_overlap = chunk_overlap
            await session.flush()
            await session.refresh(existing)
            await session.commit()
            # Mirror the persisted state onto the caller's instance
            # so downstream mappers see the post-update values.
            kb.id = existing.id
            kb.name = existing.name
            kb.description = existing.description
            kb.chunk_size = existing.chunk_size
            kb.chunk_overlap = existing.chunk_overlap
            kb.embedding_model = existing.embedding_model
            kb.created_at = existing.created_at
            kb.updated_at = existing.updated_at
            kb.tenant_id = existing.tenant_id
            return kb

    async def delete(self, *, kb: KnowledgeBase) -> None:
        """Delete the KB. Cascade removes its articles/versions/chunks.

        Caller (service) is responsible for first cleaning up
        Qdrant vectors via ``delete_article_vectors`` — the FK
        cascade does NOT touch Qdrant.
        """
        async with get_session() as session:
            existing = await session.get(KnowledgeBase, kb.id)
            if existing is None:
                return  # idempotent
            await session.delete(existing)
            await session.commit()


class ArticleRepository:
    """CRUD for Article + ArticleVersion rows.

    The :meth:`create` method mints both rows in a single
    transaction (per the spec in
    ``knowledge.models.Article.__doc__``):

    1. INSERT the article with ``current_version_id=NULL``.
    2. INSERT the version row (version_number=1, content_hash from
       raw_text).
    3. UPDATE ``article.current_version_id`` to point at the new
       version.

    The column is NULL at insert time so no FK deferral is needed.
    """

    async def create(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        title: str,
        source_type: ArticleSourceType,
        raw_text: str,
        source_uri: str | None = None,
        content_hash: str | None = None,
    ) -> tuple[Article, ArticleVersion]:
        """Insert an Article + its v1 ArticleVersion in one transaction.

        The caller passes the pre-computed ``content_hash`` so the
        hash algorithm stays centralized in ``worker._compute_content_hash``.
        We don't duplicate it here.

        Returns ``(article, version)``. The article's
        ``current_version_id`` is wired to the new version inside the
        same transaction.
        """
        if content_hash is None:
            content_hash = _sha256_hex(raw_text)

        article_id = new_id()
        version_id = new_id()
        article = Article(
            id=article_id,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            title=title,
            source_uri=source_uri,
            source_type=source_type,
            status=ArticleStatus.DRAFT,
            current_version_id=None,  # wired below
            error_message=None,
        )
        version = ArticleVersion(
            id=version_id,
            article_id=article_id,
            version_number=1,
            raw_text=raw_text,
            content_hash=content_hash,
        )

        async with get_session() as session:
            session.add(article)
            await session.flush()  # ensures article.id is usable for the FK
            session.add(version)
            await session.flush()  # ensures version.id is populated
            # Wire the version onto the article. Same transaction so
            # the FK resolution is guaranteed.
            article.current_version_id = version_id
            await session.flush()
            await session.refresh(article)
            await session.refresh(version)
            await session.commit()
            return article, version

    async def get_by_id(
        self, *, tenant_id: str, article_id: str
    ) -> Article | None:
        """Look up an Article scoped to ``tenant_id``."""
        async with get_session() as session:
            stmt = select(Article).where(
                Article.id == article_id,
                Article.tenant_id == tenant_id,
            )
            return (await session.execute(stmt)).scalar_one_or_none()

    async def list_by_kb(
        self,
        *,
        tenant_id: str,
        kb_id: str,
        status: ArticleStatus | None = None,
        limit: int = 100,
    ) -> list[Article]:
        """List articles in a KB, optionally filtered by status.

        Newest-first by ``created_at``. ``limit`` defaults to 100 —
        pagination is out of scope for M1.
        """
        async with get_session() as session:
            stmt = (
                select(Article)
                .where(
                    Article.tenant_id == tenant_id,
                    Article.knowledge_base_id == kb_id,
                )
                .order_by(Article.created_at.desc())
                .limit(limit)
            )
            if status is not None:
                stmt = stmt.where(Article.status == status)
            return list((await session.execute(stmt)).scalars().all())

    async def update(
        self,
        *,
        article: Article,
        title: str | None = None,
        source_uri: str | None = None,
    ) -> Article:
        """Patch mutable metadata fields. NOT ``raw_text`` (re-uploads go via 6.10)."""
        async with get_session() as session:
            existing = await session.get(Article, article.id)
            if existing is None:
                return article  # unchanged
            if title is not None:
                existing.title = title
            if source_uri is not None:
                existing.source_uri = source_uri
            await session.flush()
            await session.refresh(existing)
            await session.commit()
            # Mirror post-update state onto the caller's instance so
            # downstream mappers see fresh values.
            article.id = existing.id
            article.title = existing.title
            article.source_uri = existing.source_uri
            article.source_type = existing.source_type
            article.status = existing.status
            article.current_version_id = existing.current_version_id
            article.error_message = existing.error_message
            article.knowledge_base_id = existing.knowledge_base_id
            article.tenant_id = existing.tenant_id
            article.created_at = existing.created_at
            article.updated_at = existing.updated_at
            return article

    async def delete(self, *, article: Article) -> None:
        """Delete the Article. Cascade removes its ArticleVersion + Chunk rows.

        Caller (service) MUST have already called
        ``delete_article_vectors`` for best-effort Qdrant cleanup —
        the FK cascade does not touch Qdrant.
        """
        async with get_session() as session:
            existing = await session.get(Article, article.id)
            if existing is None:
                return  # idempotent
            await session.delete(existing)
            await session.commit()

    async def list_ids_by_kb(self, *, kb_id: str) -> list[str]:
        """Return all article IDs belonging to ``kb_id``.

        Used by the KB-delete cleanup branch (service.delete_kb) so
        it can sweep Qdrant vectors for each article BEFORE the FK
        cascade wipes the ``ArticleVersion`` rows that
        ``delete_article_vectors`` would otherwise need to discover.
        """
        async with get_session() as session:
            stmt = select(Article.id).where(Article.knowledge_base_id == kb_id)
            return list((await session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# Small helpers re-exported for service convenience.
# ---------------------------------------------------------------------------


__all__ = [
    "ArticleRepository",
    "KnowledgeBaseRepository",
]