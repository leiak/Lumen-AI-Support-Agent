"""KnowledgeBase + Article CRUD REST API (Task 6.9).

All routes under ``/api/v1/knowledge``, all behind JWT auth.

Auth
----
We use ``auth.dependencies.get_current_user`` which decodes the Bearer
JWT and returns the full payload (sub, tenant_id, role, iat, exp).
Tenant context comes from the ``tenant_id`` claim — there is no way
for a caller to act on a different tenant's KBs/articles.

Authorization
-------------
For M1 we accept any authenticated tenant user (admin / owner /
agent / viewer). The Article + KB CRUD is a low-risk surface and
viewers will need to read KBs to use the chat surface later.
Stage 7+ can tighten roles per endpoint.

Endpoints
---------
KBs:
    GET    /knowledge-bases
    POST   /knowledge-bases
    GET    /knowledge-bases/{kb_id}
    PATCH  /knowledge-bases/{kb_id}
    DELETE /knowledge-bases/{kb_id}

Articles:
    GET    /knowledge-bases/{kb_id}/articles
    POST   /knowledge-bases/{kb_id}/articles
    POST   /knowledge-bases/{kb_id}/articles/upload  (Task 6.10)
    GET    /articles/{article_id}
    PATCH  /articles/{article_id}
    DELETE /articles/{article_id}
    POST   /articles/{article_id}/reindex
    POST   /articles/{article_id}/upload             (Task 6.10)

The create-article endpoint schedules ``index_article`` via
``asyncio.create_task`` AFTER returning the response — the worker
queue is a Stage 7+ concern, so for M1 the dispatch is a
fire-and-forget coroutine bound to the app's event loop.
"""
from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)

from auth.dependencies import get_current_user
from core.logging import get_logger
from knowledge.enums import ArticleStatus
from knowledge.models import Article, ArticleVersion, KnowledgeBase
from knowledge.parser import MAX_PARSE_BYTES
from knowledge.schemas import (
    ArticleCreateIn,
    ArticleListOut,
    ArticleOut,
    ArticleUpdateIn,
    ArticleVersionOut,
    ArticleWithVersionOut,
    KnowledgeBaseCreateIn,
    KnowledgeBaseListOut,
    KnowledgeBaseOut,
    KnowledgeBaseUpdateIn,
    ReindexRequestIn,
    ReindexResultOut,
)
from knowledge.service import (
    ArticleService,
    KnowledgeBaseService,
    OversizeDocumentError,
    UnsupportedDocumentType,
)
from knowledge.worker import index_article

log = get_logger(__name__)

# Upload chunk size for the cap-check read loop. 1 MiB keeps
# memory bounded while keeping the loop tight (~50 iterations for
# a 50 MiB payload). FastAPI's ``UploadFile.read(size)`` reads at
# most ``size`` bytes from the spooled buffer / async file.
_UPLOAD_READ_CHUNK = 1 * 1024 * 1024

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])


# ---------------------------------------------------------------------------
# Mappers (ORM -> Pydantic)
# ---------------------------------------------------------------------------


def _kb_out(kb: KnowledgeBase) -> KnowledgeBaseOut:
    return KnowledgeBaseOut(
        id=kb.id,
        tenant_id=kb.tenant_id,
        name=kb.name,
        slug=kb.slug,
        description=kb.description,
        embedding_model=kb.embedding_model,
        chunk_size=kb.chunk_size,
        chunk_overlap=kb.chunk_overlap,
        created_at=kb.created_at,
        updated_at=kb.updated_at,
    )


def _article_out(article: Article) -> ArticleOut:
    return ArticleOut(
        id=article.id,
        tenant_id=article.tenant_id,
        knowledge_base_id=article.knowledge_base_id,
        title=article.title,
        source_type=article.source_type,
        source_uri=article.source_uri,
        status=article.status,
        current_version_id=article.current_version_id,
        error_message=article.error_message,
        created_at=article.created_at,
        updated_at=article.updated_at,
    )


def _article_with_version_out(
    article: Article, version: ArticleVersion | None
) -> ArticleWithVersionOut:
    base = _article_out(article)
    if version is None:
        return ArticleWithVersionOut(**base.model_dump(), version=None)
    return ArticleWithVersionOut(
        **base.model_dump(),
        version=ArticleVersionOut(
            id=version.id,
            article_id=version.article_id,
            version_number=version.version_number,
            content_hash=version.content_hash,
            created_at=version.created_at,
            raw_text=version.raw_text,
        ),
    )


# ---------------------------------------------------------------------------
# Service factories
# ---------------------------------------------------------------------------


def _kb_service() -> KnowledgeBaseService:
    return KnowledgeBaseService()


def _article_service() -> ArticleService:
    return ArticleService()


# ---------------------------------------------------------------------------
# KnowledgeBase routes
# ---------------------------------------------------------------------------


@router.get("/knowledge-bases", response_model=KnowledgeBaseListOut)
async def list_knowledge_bases(
    claims: Annotated[dict[str, Any], Depends(get_current_user)],
) -> KnowledgeBaseListOut:
    """List KBs for the caller's tenant (newest-first, capped at 100)."""
    items = await _kb_service().list_kbs(tenant_id=claims["tenant_id"])
    return KnowledgeBaseListOut(items=[_kb_out(k) for k in items])


@router.post(
    "/knowledge-bases",
    response_model=KnowledgeBaseOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_knowledge_base(
    body: KnowledgeBaseCreateIn,
    claims: Annotated[dict[str, Any], Depends(get_current_user)],
) -> KnowledgeBaseOut:
    """Create a new KB. 409 on slug conflict within tenant."""
    try:
        kb = await _kb_service().create_kb(
            tenant_id=claims["tenant_id"],
            name=body.name,
            slug=body.slug,
            description=body.description,
            embedding_model=body.embedding_model,
            chunk_size=body.chunk_size,
            chunk_overlap=body.chunk_overlap,
        )
    except ValueError as exc:
        # The service raises ``ValueError`` for both validation and
        # the slug-conflict branch (mapped to 409). The
        # distinguishing substring keeps the response accurate.
        msg = str(exc)
        if "already used" in msg:
            raise HTTPException(status_code=409, detail=msg) from exc
        raise HTTPException(status_code=422, detail=msg) from exc
    return _kb_out(kb)


@router.get("/knowledge-bases/{kb_id}", response_model=KnowledgeBaseOut)
async def get_knowledge_base(
    kb_id: str,
    claims: Annotated[dict[str, Any], Depends(get_current_user)],
) -> KnowledgeBaseOut:
    """Fetch a single KB. 404 on missing or cross-tenant."""
    kb = await _kb_service().get_kb(tenant_id=claims["tenant_id"], kb_id=kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return _kb_out(kb)


@router.patch("/knowledge-bases/{kb_id}", response_model=KnowledgeBaseOut)
async def update_knowledge_base(
    kb_id: str,
    body: KnowledgeBaseUpdateIn,
    claims: Annotated[dict[str, Any], Depends(get_current_user)],
) -> KnowledgeBaseOut:
    """Patch mutable fields (name, description, chunk params). Slug is immutable."""
    try:
        kb = await _kb_service().update_kb(
            tenant_id=claims["tenant_id"],
            kb_id=kb_id,
            name=body.name,
            description=body.description,
            chunk_size=body.chunk_size,
            chunk_overlap=body.chunk_overlap,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return _kb_out(kb)


@router.delete(
    "/knowledge-bases/{kb_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_knowledge_base(
    kb_id: str,
    claims: Annotated[dict[str, Any], Depends(get_current_user)],
) -> None:
    """Delete a KB. Cascade handles articles/versions/chunks; Qdrant is best-effort swept first."""
    deleted = await _kb_service().delete_kb(
        tenant_id=claims["tenant_id"], kb_id=kb_id
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    # 204 No Content — FastAPI suppresses the body automatically when
    # the response_model is unset and we return None.


# ---------------------------------------------------------------------------
# Article routes
# ---------------------------------------------------------------------------


async def _run_indexing(article_id: str) -> None:
    """Fire-and-forget wrapper around ``index_article``.

    M1 has no worker queue, so the API schedules this coroutine via
    ``asyncio.create_task`` AFTER returning the create response.
    Exceptions are caught + logged at WARNING (class name only —
    never the raw ``repr(exc)`` because exception args can carry
    PII / infra hints).

    Stage 7+ replaces this whole helper with an arq / celery / redis
    dispatcher that retries on failure.
    """
    try:
        await index_article(article_id=article_id)
    except Exception as exc:
        # PII discipline: log ONLY the class name. The full repr(exc)
        # is never emitted because exception args can carry PII
        # (OpenAI keys, file paths, infra hints). The ``index_article``
        # pipeline already writes ``error_message`` (class name) +
        # transitions status to FAILED — this catch is the safety
        # net for unexpected bugs above that layer.
        log.warning(
            "knowledge.article.index_task_failed",
            article_id=article_id,
            error_type=type(exc).__name__,
        )


@router.get(
    "/knowledge-bases/{kb_id}/articles",
    response_model=ArticleListOut,
)
async def list_articles(
    kb_id: str,
    claims: Annotated[dict[str, Any], Depends(get_current_user)],
    status_filter: Annotated[
        ArticleStatus | None, Query(alias="status")
    ] = None,
) -> ArticleListOut:
    """List articles in a KB, optionally filtered by status.

    The query param is exposed as ``status`` (an alias) so the
    Python parameter can stay ``status_filter`` without shadowing
    ``fastapi.status`` at module scope.
    """
    items = await _article_service().list_articles(
        tenant_id=claims["tenant_id"],
        kb_id=kb_id,
        status=status_filter,
    )
    if items is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return ArticleListOut(items=[_article_out(a) for a in items])


@router.post(
    "/knowledge-bases/{kb_id}/articles",
    response_model=ArticleOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_article(
    kb_id: str,
    body: ArticleCreateIn,
    claims: Annotated[dict[str, Any], Depends(get_current_user)],
) -> ArticleOut:
    """Create a new article in a KB. Fire-and-forget the indexer.

    The indexer is scheduled via ``asyncio.create_task`` AFTER this
    function returns the 201 response — M1 has no worker queue, so
    the dispatch is bound to the app's event loop. The task captures
    its own exceptions (see ``_run_indexing``), so this endpoint
    cannot raise from indexing failures.

    The article is created with ``status=DRAFT``; the indexer
    transitions it to ``INDEXING`` -> ``INDEXED`` (or ``FAILED``).
    """
    try:
        article = await _article_service().create_article(
            tenant_id=claims["tenant_id"],
            kb_id=kb_id,
            title=body.title,
            source_type=body.source_type,
            source_uri=body.source_uri,
            raw_text=body.raw_text,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # M1 (Task 6.9): schedule the indexer as a fire-and-forget task.
    # Stage 7+ replaces this with a proper worker queue (arq /
    # celery / redis). The coroutine is bound to the app's event
    # loop; it captures its own exceptions so a failure here cannot
    # surface to the caller. The ``noqa`` silences RUF006 (store a
    # reference to the return value) because we DO want the task to
    # run independently — keeping a reference would only matter if
    # we wanted to ``await`` or inspect it, which we don't.
    asyncio.create_task(_run_indexing(article.id))  # noqa: RUF006

    return _article_out(article)


# ---------------------------------------------------------------------------
# Upload routes (Task 6.10)
# ---------------------------------------------------------------------------


async def _read_upload_capped(file: UploadFile, *, cap: int) -> bytes:
    """Read an ``UploadFile`` into memory with a hard byte cap.

    The cap protects against DoS: FastAPI's ``UploadFile`` doesn't
    impose its own limit, so without this an attacker could stream
    gigabytes into the request before any 413 fires. We read in
    1 MiB chunks and abort as soon as ``running_total`` would
    exceed ``cap``.

    Returns the concatenated bytes when the file is at or below
    the cap. Raises ``HTTPException(413, ...)`` as soon as the
    cap is exceeded — the underlying stream is left in whatever
    state the read loop left it in, but the request is rejected
    so the connection can be closed.

    PII discipline: this helper is a thin byte-loop. Filenames
    and parsed content are NEVER logged here; the caller's
    handler is responsible for the high-level audit log.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"file exceeds the {cap // (1024 * 1024)} MiB upload limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(
    "/knowledge-bases/{kb_id}/articles/upload",
    response_model=ArticleOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_article(
    kb_id: str,
    file: Annotated[UploadFile, File(...)],
    title: Annotated[str | None, Form()] = None,
    source_uri: Annotated[str | None, Form()] = None,
    claims: Annotated[dict[str, Any], Depends(get_current_user)] = None,
) -> ArticleOut:
    """Create a new article by uploading a file (Task 6.10).

    Multipart body:

    * ``file`` — the uploaded file (required). M1 supports the
      formats wired into :func:`knowledge.parser.parse_document`:
      txt, md, html, pdf, json. Max size: 50 MiB (matches
      :data:`knowledge.parser.MAX_PARSE_BYTES`).
    * ``title`` (optional) — overrides the filename-derived
      default title.
    * ``source_uri`` (optional) — caller-supplied provenance URL /
      path string. Free-form, max 2000 chars (enforced at the
      service layer).

    Status codes:

    * 201 — article created. Returns ``ArticleOut`` (status starts
      as ``DRAFT``; the indexer runs fire-and-forget).
    * 400 — file is missing or empty.
    * 401 — missing / invalid bearer token.
    * 404 — KB is not visible to the tenant.
    * 413 — file exceeds 50 MiB.
    * 422 — parser rejected the bytes (``UnsupportedDocumentType``)
      or oversize payload that slipped past the API cap
      (defense-in-depth).
    """
    if claims is None:
        # ``Annotated[..., Depends(get_current_user)]`` already
        # raises 401 before this branch can fire, but the static
        # type checker needs the explicit ``= None`` default.
        raise HTTPException(401, "missing bearer token")

    file_bytes = await _read_upload_capped(file, cap=MAX_PARSE_BYTES)
    if not file_bytes:
        raise HTTPException(status_code=400, detail="uploaded file is empty")

    try:
        article = await _article_service().upload_to_article(
            tenant_id=claims["tenant_id"],
            kb_id=kb_id,
            file_bytes=file_bytes,
            file_name=file.filename or "upload",
            mime_type=file.content_type,
            title=title,
            source_uri=source_uri,
        )
    except OversizeDocumentError as exc:
        # Parser saw bytes > MAX_PARSE_BYTES. The API-level cap
        # above should prevent this in normal flow; this branch
        # is defense-in-depth.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except UnsupportedDocumentType as exc:
        # Bytes look like binary / can't be detected as a supported
        # format. The user gets a 422 with the parser's own message
        # (already PII-safe: format list + file_name only).
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        # The service raises ``ValueError`` for two reasons:
        # 1. KB not visible to the tenant → 404.
        # 2. ``source_uri`` exceeds the 2000-char cap → 422
        #    (validation failure, not a missing resource).
        # The distinguishing substring keeps the response accurate.
        msg = str(exc)
        if "knowledge base not found" in msg:
            raise HTTPException(status_code=404, detail=msg) from exc
        raise HTTPException(status_code=422, detail=msg) from exc

    # Fire-and-forget indexer. Same pattern as create_article —
    # ``_run_indexing`` captures its own exceptions so a failure
    # here can never surface to the caller.
    asyncio.create_task(_run_indexing(article.id))  # noqa: RUF006

    return _article_out(article)


@router.post(
    "/articles/{article_id}/upload",
    response_model=ReindexResultOut,
)
async def reupload_article(
    article_id: str,
    file: Annotated[UploadFile, File(...)],
    title: Annotated[str | None, Form()] = None,
    source_uri: Annotated[str | None, Form()] = None,
    claims: Annotated[dict[str, Any], Depends(get_current_user)] = None,
) -> ReindexResultOut:
    """Replace an article's contents by uploading a new file.

    The bytes are parsed; if the resulting text's sha256 matches
    the latest ``ArticleVersion.content_hash``, the call returns
    ``skipped=True`` without minting a new version. Otherwise a
    new ``ArticleVersion`` (next ``version_number``) is inserted,
    ``article.current_version_id`` is updated, and the indexer
    runs synchronously (same contract as ``/reindex``) so the
    response carries the final status + chunk count.

    Status codes mirror :func:`upload_article` plus the
    ``ReindexResult`` body. 404 covers missing / cross-tenant
    articles.
    """
    if claims is None:  # pragma: no cover - Depends always wins
        raise HTTPException(401, "missing bearer token")

    file_bytes = await _read_upload_capped(file, cap=MAX_PARSE_BYTES)
    if not file_bytes:
        raise HTTPException(status_code=400, detail="uploaded file is empty")

    try:
        result = await _article_service().reupload_article(
            tenant_id=claims["tenant_id"],
            article_id=article_id,
            file_bytes=file_bytes,
            file_name=file.filename or "upload",
            mime_type=file.content_type,
            title=title,
            source_uri=source_uri,
        )
    except OversizeDocumentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except UnsupportedDocumentType as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        # The service's ValueError paths are all client-input
        # validation failures (e.g. ``source_uri`` too long).
        # ``result is None`` (cross-tenant / missing article)
        # is handled separately below as 404.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if result is None:
        raise HTTPException(status_code=404, detail="article not found")
    return ReindexResultOut(
        article_id=result.article_id,
        skipped=result.skipped,
        version_number=result.version_number,
        status=result.status,
        chunks_indexed=result.chunks_indexed,
    )


@router.get("/articles/{article_id}", response_model=ArticleWithVersionOut)
async def get_article(
    article_id: str,
    claims: Annotated[dict[str, Any], Depends(get_current_user)],
) -> ArticleWithVersionOut:
    """Fetch an article + its current ArticleVersion in one round-trip.

    Returns 404 on missing or cross-tenant.
    """
    article = await _article_service().get_article(
        tenant_id=claims["tenant_id"], article_id=article_id
    )
    if article is None:
        raise HTTPException(status_code=404, detail="article not found")
    version: ArticleVersion | None = None
    if article.current_version_id:
        from core.database import get_session  # local import to avoid top-level cycle

        async with get_session() as session:
            version = await session.get(ArticleVersion, article.current_version_id)
    return _article_with_version_out(article, version)


@router.patch("/articles/{article_id}", response_model=ArticleOut)
async def update_article(
    article_id: str,
    body: ArticleUpdateIn,
    claims: Annotated[dict[str, Any], Depends(get_current_user)],
) -> ArticleOut:
    """Patch mutable metadata fields. ``raw_text`` re-uploads go via Stage 6.10."""
    article = await _article_service().update_article(
        tenant_id=claims["tenant_id"],
        article_id=article_id,
        title=body.title,
        source_uri=body.source_uri,
    )
    if article is None:
        raise HTTPException(status_code=404, detail="article not found")
    return _article_out(article)


@router.delete(
    "/articles/{article_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_article(
    article_id: str,
    claims: Annotated[dict[str, Any], Depends(get_current_user)],
) -> None:
    """Delete an article. Best-effort Qdrant cleanup runs first."""
    deleted = await _article_service().delete_article(
        tenant_id=claims["tenant_id"], article_id=article_id
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="article not found")


@router.post(
    "/articles/{article_id}/reindex",
    response_model=ReindexResultOut,
)
async def reindex_article_endpoint(
    article_id: str,
    body: ReindexRequestIn,
    claims: Annotated[dict[str, Any], Depends(get_current_user)],
) -> ReindexResultOut:
    """Reindex an article. ``force=True`` mints a new version even when unchanged.

    404 on missing or cross-tenant.
    """
    result = await _article_service().reindex_article(
        tenant_id=claims["tenant_id"],
        article_id=article_id,
        force=body.force,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="article not found")
    return ReindexResultOut(
        article_id=result.article_id,
        skipped=result.skipped,
        version_number=result.version_number,
        status=result.status,
        chunks_indexed=result.chunks_indexed,
    )


__all__ = ["router"]