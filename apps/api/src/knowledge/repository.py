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
from sqlalchemy.ext.asyncio import AsyncSession

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

    async def get_by_id_for_update(
        self,
        *,
        session: AsyncSession,
        tenant_id: str,
        article_id: str,
    ) -> Article | None:
        """Look up an Article scoped to ``tenant_id`` with ``SELECT ... FOR UPDATE``.

        Used by the re-upload path (Task 6.10 follow-up) so two
        concurrent reuploads of the same article serialize at the
        DB level: the second caller blocks on the row lock until
        the first commits, then reads the freshly-committed
        ``latest_version`` and observes the dedup short-circuit
        instead of racing to ``append_version`` and tripping the
        ``uq_article_versions_article_version`` UNIQUE constraint.

        The caller owns ``session`` and is responsible for
        committing / rolling back — this method only acquires the
        row lock within the caller's transaction.

        Returns ``None`` if the row doesn't exist OR belongs to a
        different tenant (same shape as :meth:`get_by_id`).
        """
        stmt = (
            select(Article)
            .where(
                Article.id == article_id,
                Article.tenant_id == tenant_id,
            )
            .with_for_update()
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

    async def list_ids_by_kb(
        self, *, tenant_id: str, kb_id: str
    ) -> list[str]:
        """Return all article IDs belonging to ``kb_id`` within ``tenant_id``.

        Defence-in-depth: ``tenant_id`` is in the WHERE clause so
        the repo can't accidentally cross tenants even if a future
        caller forgets the pre-check. The current caller
        (``KnowledgeBaseService.delete_kb``) already verifies
        ownership via ``get_kb`` first, so this is a belt to the
        existing braces.

        Used by the KB-delete cleanup branch so it can sweep Qdrant
        vectors for each article BEFORE the FK cascade wipes the
        ``ArticleVersion`` rows that ``delete_article_vectors``
        would otherwise need to discover.
        """
        async with get_session() as session:
            stmt = select(Article.id).where(
                Article.tenant_id == tenant_id,
                Article.knowledge_base_id == kb_id,
            )
            return list((await session.execute(stmt)).scalars().all())

    async def max_version_number(
        self, *, article_id: str, session: AsyncSession | None = None
    ) -> int:
        """Return the highest ``version_number`` for ``article_id``, or 0.

        Used by the re-upload path (Task 6.10) to compute the next
        ``version_number`` when minting a new ``ArticleVersion``.
        Returns 0 when the article has no versions yet (e.g. a future
        "draft without version" lifecycle) — callers add 1 to that
        for the new version number.

        No tenant scoping here: ``article_id`` is opaque (ULID) and
        the caller has already verified tenant ownership via
        :meth:`get_by_id`.

        When ``session`` is supplied, the query runs inside the
        caller's transaction (used by :meth:`ArticleService.reupload_article`
        to share the per-article row lock with the article read).
        Otherwise a fresh short-lived session is opened, matching
        the per-method pattern used by the rest of the repo.
        """
        stmt = (
            select(ArticleVersion.version_number)
            .where(ArticleVersion.article_id == article_id)
            .order_by(ArticleVersion.version_number.desc())
            .limit(1)
        )
        if session is not None:
            row = (await session.execute(stmt)).scalar_one_or_none()
            return int(row) if row is not None else 0
        async with get_session() as own_session:
            row = (await own_session.execute(stmt)).scalar_one_or_none()
            return int(row) if row is not None else 0

    async def latest_version(
        self,
        *,
        article_id: str,
        session: AsyncSession | None = None,
    ) -> ArticleVersion | None:
        """Return the highest-``version_number`` row for ``article_id``.

        Companion to :meth:`max_version_number` — callers that need
        the full row (e.g. to read ``content_hash`` for the re-upload
        dedup short-circuit) use this. Returns ``None`` when the
        article has no versions.

        When ``session`` is supplied, the query runs inside the
        caller's transaction so the read sees the locked article
        row + any newly-committed sibling writes from the same
        caller.
        """
        stmt = (
            select(ArticleVersion)
            .where(ArticleVersion.article_id == article_id)
            .order_by(ArticleVersion.version_number.desc())
            .limit(1)
        )
        if session is not None:
            return (await session.execute(stmt)).scalar_one_or_none()
        async with get_session() as own_session:
            return (await own_session.execute(stmt)).scalar_one_or_none()

    async def append_version(
        self,
        *,
        article_id: str,
        raw_text: str,
        content_hash: str,
        version_number: int,
        session: AsyncSession | None = None,
    ) -> ArticleVersion:
        """Insert a new ``ArticleVersion`` row and wire it as the article's current.

        Mirrors the article+version mint in :meth:`create` but for
        the re-upload path: an EXISTING article gets a NEW
        ``ArticleVersion`` row + ``current_version_id`` UPDATE.

        Ordering matters: the version row is flushed BEFORE the
        article UPDATE so the FK on ``articles.current_version_id``
        resolves on the UPDATE. Same transaction so a crash in
        between doesn't leave the article pointing at a non-existent
        version.

        When ``session`` is supplied, the caller owns the
        transaction (used by
        :meth:`ArticleService.reupload_article` so the article
        row lock acquired by
        :meth:`ArticleRepository.get_by_id_for_update` is held
        across both the read and this write — preventing the
        concurrent-reupload UNIQUE-constraint race on
        ``uq_article_versions_article_version``). The caller is
        then responsible for ``commit()``. When ``session`` is
        omitted, this method opens its own short-lived session and
        commits internally.

        Returns the freshly-inserted version row.
        """
        version_id = new_id()
        version = ArticleVersion(
            id=version_id,
            article_id=article_id,
            version_number=version_number,
            raw_text=raw_text,
            content_hash=content_hash,
        )

        async def _do(session: AsyncSession) -> ArticleVersion:
            session.add(version)
            await session.flush()  # ensure version.id is populated
            # Wire the article at the new version. status=INDEXING
            # mirrors ``reindex_article``'s recovery semantics: a
            # crash here leaves the article recoverable rather than
            # stuck in DRAFT.
            article = await session.get(Article, article_id)
            if article is None:
                raise ValueError("article disappeared mid-append")
            article.current_version_id = version_id
            article.status = ArticleStatus.INDEXING
            article.error_message = None
            await session.flush()
            await session.refresh(article)
            await session.refresh(version)
            return version

        if session is not None:
            return await _do(session)
        async with get_session() as own_session:
            v = await _do(own_session)
            await own_session.commit()
            return v


# ---------------------------------------------------------------------------
# Small helpers re-exported for service convenience.
# ---------------------------------------------------------------------------


__all__ = [
    "ArticleRepository",
    "KnowledgeBaseRepository",
]