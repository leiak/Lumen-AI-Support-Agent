"""Tenant-scoped KnowledgeBase + Article business logic.

Layering
    :class:`KnowledgeBaseService` and :class:`ArticleService` sit
    between the API layer (``knowledge.api``) and the repository
    (``knowledge.repository``). The service is responsible for:

    * enforcing tenant scoping on every operation (a KB or article
      from another tenant must never leak through any of the
      read/write methods);
    * slug validation (regex + length) on KB create;
    * the create-article -> fire-and-forget indexer handoff;
    * the Qdrant-vector cleanup that the FK cascade can't perform;
    * returning ``None`` for not-found / cross-tenant access rather
      than raising, so the API layer can map to 404 uniformly.

Tenant isolation pattern
------------------------

Mirrors :class:`conversation.service.ConversationService`. Every
read or update starts with ``get_kb`` (or ``get_article``) which
does the WHERE on ``tenant_id`` at the repo layer. A cross-tenant
``kb_id`` / ``article_id`` simply returns ``None`` — there is no
"this exists but you can't see it" branch, so the API maps it
1:1 to 404 and an attacker cannot use response timing or body
shape to enumerate KBs across tenants.

PII discipline
--------------

Logs carry only opaque ULIDs, status names, and the exception class
name. NEVER ``name``, ``slug``, ``title``, ``raw_text``, or
``error_message`` (the column stores the class name only — never
its repr).
"""
from __future__ import annotations

import hashlib
import re

from sqlalchemy.exc import IntegrityError

from core.config import get_settings
from core.database import get_sessionmaker
from core.logging import get_logger
from knowledge.enums import ArticleSourceType, ArticleStatus
from knowledge.models import (
    Article,
    KnowledgeBase,
)
from knowledge.parser import (
    OversizeDocumentError,
    UnsupportedDocumentType,
    parse_document,
)
from knowledge.repository import (
    ArticleRepository,
    KnowledgeBaseRepository,
)
from knowledge.worker import (
    ReindexResult,
    delete_article_vectors,
    index_article,
    reindex_article,
)

log = get_logger(__name__)


# Service-layer duplicate of the schema slug regex. Defence in depth:
# if the API is bypassed (e.g. by an internal caller), this still
# enforces the format. Stage 6.x may move to a single source — kept
# here for now because the two layers need to fail closed
# independently.
_SLUG_REGEX = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")

# Service-layer duplicate of the ``articles.source_uri`` column cap.
# FastAPI's ``Form()`` does NOT enforce Pydantic ``max_length`` so a
# caller could otherwise POST a 10 MB ``source_uri`` and saturate the
# request body / ORM string column before any validation fires. The
# service is the right layer for this invariant — the API layer
# delegates the byte cap to ``_read_upload_capped`` and the string
# cap lives here next to the column-width it mirrors
# (``knowledge.models.Article.source_uri = String(2000)``).
SOURCE_URI_MAX_LEN = 2000


class KnowledgeBaseService:
    """Tenant-scoped CRUD for KnowledgeBase rows."""

    def __init__(
        self,
        repo: KnowledgeBaseRepository | None = None,
        article_service: ArticleService | None = None,
    ) -> None:
        self._repo = repo or KnowledgeBaseRepository()
        # Lazily construct an ArticleService so the cross-service
        # delete-kb cleanup can call ``list_ids_by_kb`` and the
        # per-article vector sweep. We pass ``self`` back into it
        # after construction to avoid an init-order cycle.
        self._article_service = article_service

    # ---- Read paths ----

    async def list_kbs(self, *, tenant_id: str) -> list[KnowledgeBase]:
        """List KBs for the tenant, newest-first.

        No pagination in M1 — the repo enforces a 100-row cap.
        """
        return await self._repo.list_by_tenant(tenant_id=tenant_id)

    async def get_kb(
        self, *, tenant_id: str, kb_id: str
    ) -> KnowledgeBase | None:
        """Return the KB if it belongs to ``tenant_id``; otherwise ``None``.

        Cross-tenant access returns ``None`` (not 403, not a
        different exception) — the API layer maps to 404. This is
        the same anti-enumeration pattern as
        :meth:`conversation.service.ConversationService.get`.
        """
        kb = await self._repo.get_by_id(tenant_id=tenant_id, kb_id=kb_id)
        if kb is None:
            log.warning(
                "knowledge.kb.lookup_miss",
                tenant_id=tenant_id,
                kb_id=kb_id,
            )
        return kb

    # ---- Mutations ----

    async def create_kb(
        self,
        *,
        tenant_id: str,
        name: str,
        slug: str,
        description: str | None = None,
        embedding_model: str | None = None,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
    ) -> KnowledgeBase:
        """Create a new KB. Raises ``ValueError`` on validation failures.

        The slug regex + length check is enforced HERE in addition to
        the Pydantic schema so internal callers can't bypass it.
        ``IntegrityError`` from the unique index is translated into a
        ``ValueError`` so the API layer maps cleanly to 409.
        """
        if not _SLUG_REGEX.match(slug):
            raise ValueError(
                "slug must match ^[a-z0-9][a-z0-9-]{0,99}$ "
                "(lowercase alnum + dash; no leading dash)"
            )
        if len(slug) > 100:
            raise ValueError("slug must be 100 characters or fewer")
        if name and (len(name) < 1 or len(name) > 200):
            raise ValueError("name must be 1-200 characters")
        # Cross-field chunk invariant. Defaults from the repo /
        # model are 800/100, so chunk_overlap < chunk_size holds
        # out of the box; we still re-check explicit inputs.
        effective_chunk_size = chunk_size if chunk_size is not None else 800
        effective_chunk_overlap = (
            chunk_overlap if chunk_overlap is not None else 100
        )
        if effective_chunk_overlap >= effective_chunk_size:
            raise ValueError("chunk_overlap must be strictly less than chunk_size")

        try:
            return await self._repo.create(
                tenant_id=tenant_id,
                name=name,
                slug=slug,
                description=description,
                embedding_model=(
                    embedding_model if embedding_model else get_settings().default_embedding_model
                ),
                chunk_size=effective_chunk_size,
                chunk_overlap=effective_chunk_overlap,
            )
        except IntegrityError:
            # The unique index ``uq_knowledge_bases_tenant_slug`` fired.
            # The API maps ``ValueError`` to a 4xx, so we surface this
            # as a domain-level conflict the API can map to 409.
            log.info(
                "knowledge.kb.slug_conflict",
                tenant_id=tenant_id,
                slug=slug,
            )
            raise ValueError(
                f"slug {slug!r} is already used by another knowledge base in this tenant"
            ) from None

    async def update_kb(
        self,
        *,
        tenant_id: str,
        kb_id: str,
        name: str | None = None,
        description: str | None = None,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
    ) -> KnowledgeBase | None:
        """Patch mutable fields on a tenant-owned KB.

        Slug is NOT mutable. Cross-field chunk invariant is checked
        here so the API doesn't need to express it via Pydantic
        validators. Returns ``None`` if the KB doesn't exist or
        belongs to a different tenant.
        """
        existing = await self.get_kb(tenant_id=tenant_id, kb_id=kb_id)
        if existing is None:
            return None

        effective_chunk_size = chunk_size if chunk_size is not None else existing.chunk_size
        effective_chunk_overlap = (
            chunk_overlap if chunk_overlap is not None else existing.chunk_overlap
        )
        if effective_chunk_overlap >= effective_chunk_size:
            raise ValueError("chunk_overlap must be strictly less than chunk_size")

        return await self._repo.update(
            kb=existing,
            name=name,
            description=description,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

    async def delete_kb(
        self, *, tenant_id: str, kb_id: str
    ) -> bool:
        """Delete a KB. Best-effort Qdrant cleanup runs first.

        Order matters: we MUST sweep Qdrant vectors for each article
        BEFORE the FK cascade deletes the ``ArticleVersion`` rows
        that ``delete_article_vectors`` discovers (the function
        looks up version IDs from the DB). After the DB delete the
        version rows are gone, so vector cleanup becomes a
        best-effort-by-collection walk.

        Returns True on success, False if the KB is not visible.
        """
        existing = await self.get_kb(tenant_id=tenant_id, kb_id=kb_id)
        if existing is None:
            return False

        # Build a sibling ArticleService (or reuse the cached one)
        # so we can list article IDs and sweep vectors. The
        # ArticleService doesn't need to know it's being used for
        # cleanup — it just exposes ``list_ids_by_kb`` and the
        # vector cleanup is a free function in ``knowledge.worker``.
        article_service = self._article_service or ArticleService()
        self._article_service = article_service

        article_ids = await article_service.list_article_ids(
            tenant_id=tenant_id, kb_id=kb_id
        )
        for aid in article_ids:
            # Best-effort. A failure here doesn't block the DB
            # delete — orphans in Qdrant self-heal on the next
            # operator sweep.
            try:
                await delete_article_vectors(article_id=aid)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning(
                    "knowledge.kb.delete_vector_cleanup_failed",
                    article_id=aid,
                    error_type=type(exc).__name__,
                )

        await self._repo.delete(kb=existing)
        log.info(
            "knowledge.kb.deleted",
            tenant_id=tenant_id,
            kb_id=kb_id,
            article_count=len(article_ids),
        )
        return True


class ArticleService:
    """Tenant-scoped CRUD for Article rows + indexer handoff."""

    def __init__(
        self,
        repo: ArticleRepository | None = None,
        kb_repo: KnowledgeBaseRepository | None = None,
    ) -> None:
        self._repo = repo or ArticleRepository()
        self._kb_repo = kb_repo or KnowledgeBaseRepository()

    # ---- Read paths ----

    async def list_articles(
        self,
        *,
        tenant_id: str,
        kb_id: str,
        status: ArticleStatus | None = None,
    ) -> list[Article] | None:
        """Return articles for a tenant-owned KB, optionally filtered.

        Returns ``None`` if the KB doesn't exist or belongs to
        another tenant — the API maps both cases to 404 so an
        attacker can't probe KB existence across tenants.
        """
        kb = await self._kb_repo.get_by_id(tenant_id=tenant_id, kb_id=kb_id)
        if kb is None:
            log.warning(
                "knowledge.article.list_kb_missing",
                tenant_id=tenant_id,
                kb_id=kb_id,
            )
            return None
        return await self._repo.list_by_kb(
            tenant_id=tenant_id,
            kb_id=kb_id,
            status=status,
        )

    async def get_article(
        self, *, tenant_id: str, article_id: str
    ) -> Article | None:
        """Return the article if it belongs to the tenant; otherwise ``None``."""
        article = await self._repo.get_by_id(tenant_id=tenant_id, article_id=article_id)
        if article is None:
            log.warning(
                "knowledge.article.lookup_miss",
                tenant_id=tenant_id,
                article_id=article_id,
            )
        return article

    async def list_article_ids(
        self, *, tenant_id: str, kb_id: str
    ) -> list[str]:
        """Return all article IDs for a tenant-owned KB.

        Thin pass-through over :meth:`ArticleRepository.list_ids_by_kb`
        kept on the service so the KB-delete cleanup branch
        (``KnowledgeBaseService.delete_kb``) doesn't have to reach
        into ``self._article_service._repo`` directly — service
        callers shouldn't know that an ArticleRepository exists.
        """
        return await self._repo.list_ids_by_kb(
            tenant_id=tenant_id, kb_id=kb_id
        )

    # ---- Mutations ----

    async def create_article(
        self,
        *,
        tenant_id: str,
        kb_id: str,
        title: str,
        source_type: ArticleSourceType,
        raw_text: str,
        source_uri: str | None = None,
    ) -> Article:
        """Create an Article + its v1 ArticleVersion, fire-and-forget the indexer.

        Returns the freshly-created Article. The indexer
        (``:func:`knowledge.worker.index_article```) is scheduled
        via ``asyncio.create_task`` in the API handler AFTER this
        service call returns — M1 doesn't have a worker queue, so
        the dispatch is a fire-and-forget coroutine bound to the
        app's event loop. Stage 7+ replaces this with an arq /
        celery / redis-backed dispatcher.

        Raises ``ValueError`` if the KB is not visible to the tenant
        (fail-loud at the boundary so a misconfigured caller is
        caught immediately rather than silently writing to the
        wrong tenant).
        """
        kb = await self._kb_repo.get_by_id(tenant_id=tenant_id, kb_id=kb_id)
        if kb is None:
            # Match the conversation service's pattern: raising at the
            # boundary because this branch means the caller passed
            # a kb_id that doesn't belong to them. The API maps to
            # 404.
            log.warning(
                "knowledge.article.create_kb_missing",
                tenant_id=tenant_id,
                kb_id=kb_id,
            )
            raise ValueError("knowledge base not found")

        article, _version = await self._repo.create(
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            title=title,
            source_type=source_type,
            raw_text=raw_text,
            source_uri=source_uri,
        )
        log.info(
            "knowledge.article.created",
            tenant_id=tenant_id,
            kb_id=kb_id,
            article_id=article.id,
        )
        return article

    async def update_article(
        self,
        *,
        tenant_id: str,
        article_id: str,
        title: str | None = None,
        source_uri: str | None = None,
    ) -> Article | None:
        """Patch mutable fields. NOT ``raw_text`` (re-uploads go via 6.10).

        Returns ``None`` if the article is not visible to the
        tenant.
        """
        existing = await self.get_article(tenant_id=tenant_id, article_id=article_id)
        if existing is None:
            return None
        return await self._repo.update(
            article=existing,
            title=title,
            source_uri=source_uri,
        )

    async def delete_article(
        self, *, tenant_id: str, article_id: str
    ) -> bool:
        """Delete an article. Best-effort Qdrant cleanup runs first.

        Order matters: ``delete_article_vectors`` discovers version
        IDs by querying the DB; if we delete the article first the
        versions are gone and the sweep becomes a blind collection
        walk. So we sweep first, then DB-delete.

        Returns True on success, False if not visible.
        """
        existing = await self.get_article(tenant_id=tenant_id, article_id=article_id)
        if existing is None:
            return False

        # Best-effort. A failure doesn't block the DB delete.
        try:
            await delete_article_vectors(article_id=article_id)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "knowledge.article.delete_vector_cleanup_failed",
                article_id=article_id,
                error_type=type(exc).__name__,
            )

        await self._repo.delete(article=existing)
        log.info(
            "knowledge.article.deleted",
            tenant_id=tenant_id,
            article_id=article_id,
        )
        return True

    async def reindex_article(
        self,
        *,
        tenant_id: str,
        article_id: str,
        force: bool = False,
    ) -> ReindexResult | None:
        """Run :func:`knowledge.worker.reindex_article` for a tenant-owned article.

        Returns the ``ReindexResult`` or ``None`` if the article is
        not visible to the tenant (the API maps to 404).
        """
        existing = await self.get_article(tenant_id=tenant_id, article_id=article_id)
        if existing is None:
            return None
        return await reindex_article(article_id=article_id, force=force)

    # ---- Upload (Task 6.10) ----

    async def upload_to_article(
        self,
        *,
        tenant_id: str,
        kb_id: str,
        file_bytes: bytes,
        file_name: str,
        mime_type: str | None,
        title: str | None = None,
        source_uri: str | None = None,
    ) -> Article:
        """Parse an uploaded file and create a new Article + v1 version.

        Flow:

        1. Resolve the KB (404 if not visible to the tenant).
        2. Default ``title`` from the filename (stripped, capped 500).
        3. ``parse_document`` → ``ParsedDocument.text``.
        4. ``ArticleRepository.create`` with ``source_type=UPLOAD``.

        The API handler schedules ``index_article`` via
        ``asyncio.create_task`` AFTER this returns — the service
        is purely synchronous from the caller's perspective.

        Raises
        ------
        ValueError
            * KB not visible to the tenant (mapped to 404 by the
              API).
            * ``OversizeDocumentError`` from the parser (mapped to
              422 by the API).
            * ``UnsupportedDocumentType`` from the parser (mapped
              to 422 by the API).
        """
        kb = await self._kb_repo.get_by_id(tenant_id=tenant_id, kb_id=kb_id)
        if kb is None:
            log.warning(
                "knowledge.article.upload_kb_missing",
                tenant_id=tenant_id,
                kb_id=kb_id,
            )
            raise ValueError("knowledge base not found")

        # FastAPI's ``Form()`` does NOT enforce Pydantic max_length,
        # so a 10 MB ``source_uri`` would otherwise be accepted here.
        # The column itself is ``String(2000)``, so the ORM would
        # fail at INSERT with an unhelpful DB error — validate up
        # front so the API can return a clean 422.
        _validate_source_uri(source_uri)

        effective_title = _title_from_filename(file_name) if not title else title.strip()[:500]

        parsed = await parse_document(
            file_bytes=file_bytes,
            file_name=file_name,
            mime_type=mime_type,
        )

        article, _version = await self._repo.create(
            tenant_id=tenant_id,
            knowledge_base_id=kb_id,
            title=effective_title,
            source_type=ArticleSourceType.UPLOAD,
            raw_text=parsed.text,
            source_uri=source_uri,
        )
        log.info(
            "knowledge.article.uploaded",
            tenant_id=tenant_id,
            kb_id=kb_id,
            article_id=article.id,
            byte_count=len(file_bytes),
            source_format=parsed.format,
        )
        return article

    async def reupload_article(
        self,
        *,
        tenant_id: str,
        article_id: str,
        file_bytes: bytes,
        file_name: str,
        mime_type: str | None,
        title: str | None = None,
        source_uri: str | None = None,
    ) -> ReindexResult | None:
        """Replace an article's contents by uploading a new file.

        Flow:

        1. ``parse_document`` → ``ParsedDocument.text``.
        2. Open a single transaction; acquire a row lock on the
           ``articles`` row via ``SELECT ... FOR UPDATE``. Two
           concurrent reuploads of the same article therefore
           serialize at the DB level — the second blocks until
           the first commits, then reads the freshly-committed
           ``latest_version`` and either hits the dedup
           short-circuit or appends with the correct next
           ``version_number``. Without this lock, both callers
           could compute the same ``max + 1`` and the loser
           would trip ``uq_article_versions_article_version``
           and surface as a 500.
        3. Compute ``content_hash`` of the parsed text.
        4. Compare against the latest ``ArticleVersion.content_hash``
           in the same session. On match: return
           ``ReindexResult(skipped=True, ...)`` without minting a
           new version.
        5. On mismatch: mint ``version_number = max + 1``, append
           the new ``ArticleVersion``, point the article at it,
           transition to ``INDEXING``. Commit. Then run
           ``index_article`` synchronously (mirrors
           ``reindex_article`` semantics — the response carries
           the final status + chunk count).
        6. Optionally patch the title + source_uri if the caller
           supplied overrides (separate short transaction after
           the lock is released — metadata is last-writer-wins,
           which is the existing pre-fix behavior).

        Returns ``None`` when the article is not visible to the
        tenant (API maps to 404).

        Raises ``ValueError`` on:

        * ``source_uri`` length exceeding :data:`SOURCE_URI_MAX_LEN`
          (API maps to 422).
        * ``OversizeDocumentError`` / ``UnsupportedDocumentType``
          from the parser (API maps to 422).
        """
        # ``source_uri`` cap is enforced here (service layer) rather
        # than in the API because ``Form()`` does not honour
        # Pydantic ``max_length`` — the value flows in as a raw
        # ``str`` and only the service knows the column width.
        _validate_source_uri(source_uri)

        parsed = await parse_document(
            file_bytes=file_bytes,
            file_name=file_name,
            mime_type=mime_type,
        )

        new_hash = hashlib.sha256(parsed.text.encode("utf-8")).hexdigest()

        # Open ONE session for the whole "read article under lock +
        # read latest version + append new version" sequence. The
        # sessionmaker comes from ``core.database`` (same instance
        # the per-method helpers use, so connection-pool reuse is
        # preserved). ``expire_on_commit=False`` means the
        # ``existing`` ORM instance remains usable after commit
        # for the log line below.
        sm = get_sessionmaker()
        async with sm() as session:
            existing = await self._repo.get_by_id_for_update(
                session=session,
                tenant_id=tenant_id,
                article_id=article_id,
            )
            if existing is None:
                # Cross-tenant or missing — let the caller map to
                # 404. Roll back so we don't leave an empty
                # transaction pinned on the pool.
                await session.rollback()
                return None

            latest = await self._repo.latest_version(
                session=session,
                article_id=article_id,
            )

            # Dedup short-circuit: identical content → no new version.
            # Because we hold the per-article row lock, a concurrent
            # reupload of the SAME content will block here, then
            # read the freshly-committed ``latest`` and ALSO
            # short-circuit — no UNIQUE-constraint race.
            if latest is not None and latest.content_hash == new_hash:
                await session.commit()
                log.info(
                    "knowledge.article.reupload_skipped",
                    tenant_id=tenant_id,
                    article_id=article_id,
                    version_number=latest.version_number,
                )
                return ReindexResult(
                    article_id=article_id,
                    skipped=True,
                    version_number=latest.version_number,
                    status=existing.status,
                    chunks_indexed=0,
                )

            # Mismatch: compute next version_number from the
            # latest_version row above (same transaction, so this
            # can't see stale data) and append in the same session
            # so the row lock is still held across the write.
            max_v = (
                latest.version_number if latest is not None else 0
            )
            new_version_number = max_v + 1
            new_version = await self._repo.append_version(
                session=session,
                article_id=article_id,
                raw_text=parsed.text,
                content_hash=new_hash,
                version_number=new_version_number,
            )

            log.info(
                "knowledge.article.reuploaded",
                tenant_id=tenant_id,
                article_id=article_id,
                article_version_id=new_version.id,
                version_number=new_version_number,
                source_format=parsed.format,
                byte_count=len(file_bytes),
            )

            await session.commit()
            # ``existing.status`` reflects INDEXING after append_version;
            # ``expire_on_commit=False`` keeps it readable here.

        # Optional metadata overrides — only persist when the
        # caller actually passed a non-empty value so PATCH stays
        # surgical. Separate short transaction after the lock is
        # released; metadata is last-writer-wins (acceptable for
        # the upload overlay surface).
        if title or source_uri is not None:
            await self._repo.update(
                article=existing,
                title=title.strip()[:500] if title else None,
                source_uri=source_uri,
            )

        # Run the indexer synchronously so the caller sees the
        # final status + chunk count in the response — same
        # contract as /articles/{id}/reindex.
        index_result = await index_article(article_id=article_id)
        return ReindexResult(
            article_id=article_id,
            skipped=False,
            version_number=new_version_number,
            status=index_result.status,
            chunks_indexed=index_result.chunks_indexed,
        )


__all__ = [
    "ArticleService",
    "KnowledgeBaseService",
]


# Sentinel import to make the type annotation on
# ``KnowledgeBaseService.__init__`` resolvable at module load.
# ``ArticleService`` is defined just below; the forward reference
# in the ``__init__`` signature is already a string, so this is
# only here to surface the symbol to static checkers.
_ = ArticleService


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _title_from_filename(file_name: str) -> str:
    """Derive a default article title from a filename.

    Strips the extension (``report.pdf`` → ``report``), trims
    surrounding whitespace, and caps the result at 500 chars to
    match :attr:`knowledge.models.Article.title`.

    Used by :meth:`ArticleService.upload_to_article` when the
    caller doesn't supply an explicit title.
    """
    if not file_name:
        return "upload"
    if "." in file_name:
        base = file_name.rsplit(".", 1)[0]
    else:
        base = file_name
    return base.strip()[:500] or "upload"


def _validate_source_uri(source_uri: str | None) -> None:
    """Enforce the ``articles.source_uri`` length cap at the service layer.

    ``FastAPI.Form()`` does not enforce Pydantic ``max_length``, so
    without this check a caller could supply an arbitrarily long
    ``source_uri`` (hundreds of MB) and the request would be
    accepted up to whatever body cap the ASGI server imposes —
    long before any Pydantic / ORM validator could refuse it.
    The column itself is ``String(2000)`` (see
    :class:`knowledge.models.Article`), so anything longer would
    also fail at INSERT time with a confusing DB error.

    Raises ``ValueError`` (which the API layer maps to 422) when
    the input exceeds :data:`SOURCE_URI_MAX_LEN`. ``None`` is
    allowed — the column is nullable.
    """
    if source_uri is not None and len(source_uri) > SOURCE_URI_MAX_LEN:
        raise ValueError(
            f"source_uri must be {SOURCE_URI_MAX_LEN} characters or fewer"
        )


# Re-export the parser exceptions so the API layer can catch them
# without re-importing from ``knowledge.parser``.
__all__ += ["OversizeDocumentError", "UnsupportedDocumentType"]