"""Thin wrapper around :class:`AsyncQdrantClient` for knowledge base ops.

Currently exposes a single idempotent helper, :func:`ensure_collection`,
that guarantees a named Qdrant collection exists with the requested
vector schema. The helper is designed to be safe to call on every
process boot: a missing collection is created; a present one is left
alone; a real failure (Qdrant unreachable, auth refused, schema
rejected) is logged at WARNING and returns False so the health
endpoint can surface the degraded state.
"""
from __future__ import annotations

from qdrant_client.http import models as qmodels
from qdrant_client.http.exceptions import UnexpectedResponse

from core.logging import get_logger
from core.qdrant import get_qdrant_client

log = get_logger(__name__)

# Vector size for OpenAI text-embedding-3-small and text-embedding-ada-002.
# M1 default per spec 6.1. Keep as a public constant so callers can read
# it without importing qmodels.
DEFAULT_VECTOR_SIZE = 1536

# Default similarity metric. Cosine is the OpenAI recommendation for
# text-embedding-3-* and matches the spec.
DEFAULT_DISTANCE = "Cosine"

# Maps an embedding model name to its vector dimensionality. ``vector_size_for_model``
# consults this table and refuses to guess for unknown entries so a misconfigured
# ``DEFAULT_EMBEDDING_MODEL`` fails loudly at Qdrant collection-create time
# instead of silently truncating vectors at upsert. Extend this map when
# adding new providers (e.g. self-hosted Ollama, Cohere, Voyage, etc.).
EMBEDDING_VECTOR_SIZES: dict[str, int] = {
    # OpenAI text-embedding-3 family
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    # OpenAI legacy
    "text-embedding-ada-002": 1536,
    # Doubao / Volcano Engine Ark — OpenAI-compatible at /v3/embeddings
    "doubao-embedding": 1024,
    "doubao-embedding-large": 2048,
    # Doubao multimodal (text+image) — only verified dimension for vision variants.
    # Plain text still works via the OpenAI-compatible endpoint, but full
    # multimodal input requires code changes (Stage 10+).
    "doubao-embedding-vision": 2048,
}


def vector_size_for_model(model: str) -> int:
    """Return the expected vector dimensionality for an embedding model name.

    Raises ``ValueError`` (not ``KeyError``) so a startup-time misconfig
    surfaces as a clear ``ConfigError`` rather than crashing the Qdrant
    SDK with a low-level ``UnexpectedResponse``.
    """
    try:
        return EMBEDDING_VECTOR_SIZES[model]
    except KeyError as e:
        raise ValueError(
            f"Unknown embedding model {model!r}. Add it to "
            "knowledge.qdrant_client.EMBEDDING_VECTOR_SIZES so we know its "
            "vector dimensionality. Known models: "
            f"{sorted(EMBEDDING_VECTOR_SIZES)}"
        ) from e

# Default collection name for chunk embeddings. Single collection for M1;
# per-tenant / per-KB isolation will come via Qdrant payload filters or
# aliases (see spec 6.x).
DEFAULT_COLLECTION = "article_chunks"

# Maps the user-facing distance string to the qdrant_client enum. Unknown
# names fall back to Cosine so a bad config never crashes startup.
_DISTANCE_MAP: dict[str, qmodels.Distance] = {
    "Cosine": qmodels.Distance.COSINE,
    "Euclid": qmodels.Distance.EUCLID,
    "Dot": qmodels.Distance.DOT,
    "Manhattan": qmodels.Distance.MANHATTAN,
}


def _to_distance(distance: str) -> qmodels.Distance:
    """Coerce a string distance name to ``qmodels.Distance``.

    Falls back to Cosine on unknown input so the app can still boot.
    """
    return _DISTANCE_MAP.get(distance, qmodels.Distance.COSINE)


def _is_not_found(exc: UnexpectedResponse) -> bool:
    """Return True if the UnexpectedResponse corresponds to a 404.

    The qdrant-client library surfaces missing collections as
    UnexpectedResponse with status_code 404 and a "Not found" reason
    phrase. We check both the numeric status code (preferred — stable
    across qdrant versions) and a substring on the human-readable
    message (defensive — guards against future qdrant clients that
    might use a different status but the same wording).
    """
    if getattr(exc, "status_code", None) == 404:
        return True
    return "not found" in str(exc).lower()


def _is_already_exists(exc: UnexpectedResponse) -> bool:
    """Return True if the UnexpectedResponse corresponds to a 409.

    When two processes race to create the same collection, the loser's
    create call comes back as UnexpectedResponse status 409 with a
    "Already exists" reason. Treat that as success — the end state is
    what we wanted.
    """
    if getattr(exc, "status_code", None) == 409:
        return True
    return "already exists" in str(exc).lower()


async def ensure_collection(
    *,
    name: str,
    vector_size: int,
    distance: str = DEFAULT_DISTANCE,
) -> bool:
    """Ensure a Qdrant collection exists with the given schema.

    Returns:
        True if the collection already exists, was created by this call,
        or another process won the create race. False (with a WARNING
        log) on any other failure — for example, when the Qdrant URL is
        unreachable, the API key is rejected, or the request times out.

    The function is idempotent: repeated calls with the same arguments
    are safe and will only contact Qdrant twice (one ``get_collection``,
    one ``create_collection``) on the very first invocation. Subsequent
    calls hit only ``get_collection``.

    All log payloads are PII-safe: only the collection name, the
    exception class name, and (where relevant) the status code are
    emitted. The exception message is never logged because it can
    carry Qdrant URLs, error bodies, or auth hints.
    """
    try:
        client = get_qdrant_client()
    except Exception as exc:  # pragma: no cover - defensive; singleton rarely raises
        log.warning(
            "qdrant.collection.ensure_failed",
            collection=name,
            error_type=type(exc).__name__,
        )
        return False

    # 1. Probe: does the collection already exist?
    try:
        await client.get_collection(collection_name=name)
    except UnexpectedResponse as exc:
        if _is_not_found(exc):
            # Expected cold-boot case: fall through to create.
            log.debug("qdrant.collection.missing", collection=name)
        else:
            # Real Qdrant error (500, 401, 403, ...). Do NOT claim success.
            log.warning(
                "qdrant.collection.probe_failed",
                collection=name,
                status_code=getattr(exc, "status_code", None),
            )
            return False
    except Exception as exc:
        # Anything else — ConnectionError, timeout, DNS failure, etc.
        log.warning(
            "qdrant.collection.probe_error",
            collection=name,
            error_type=type(exc).__name__,
        )
        return False
    else:
        log.info("qdrant.collection.exists", collection=name)
        return True

    # 2. Create with the requested schema.
    try:
        await client.create_collection(
            collection_name=name,
            vectors_config=qmodels.VectorParams(
                size=vector_size,
                distance=_to_distance(distance),
            ),
        )
    except UnexpectedResponse as exc:
        if _is_already_exists(exc):
            # Race lost: another worker created the collection between
            # our probe and create. End state is what we wanted -> success.
            log.info("qdrant.collection.race_lost", collection=name)
            return True
        # Real create error (bad schema, vector size mismatch, etc.).
        log.warning(
            "qdrant.collection.create_failed",
            collection=name,
            status_code=getattr(exc, "status_code", None),
        )
        return False
    except Exception as exc:
        # ConnectionError / timeout / schema validation that bubbled
        # up as a non-UnexpectedResponse. Treat as hard failure.
        log.warning(
            "qdrant.collection.create_error",
            collection=name,
            error_type=type(exc).__name__,
        )
        return False

    log.info("qdrant.collection.created", collection=name)
    return True


# ---------------------------------------------------------------------------
# Upsert + delete helpers used by the worker (Task 6.7)
# ---------------------------------------------------------------------------


async def upsert_chunks(
    *,
    collection: str,
    points: list[qmodels.PointStruct],
) -> int:
    """Upsert N points to a Qdrant collection. Returns count upserted.

    Surfaces Qdrant errors via WARNING + return 0 so the caller can decide
    whether to fail the article (it should). Never raises.

    PII-safety: payload contents are forwarded as-is by Qdrant — we do
    NOT log point IDs, vectors, or payload text on the error path. The
    log payload carries only the collection name, point count, and the
    error class name.
    """
    if not points:
        log.info("qdrant.upsert.empty", collection=collection, point_count=0)
        return 0

    try:
        client = get_qdrant_client()
    except Exception as exc:  # pragma: no cover - defensive; singleton rarely raises
        log.warning(
            "qdrant.upsert.client_unavailable",
            collection=collection,
            point_count=len(points),
            error_type=type(exc).__name__,
        )
        return 0

    try:
        await client.upsert(
            collection_name=collection,
            points=points,
            # ``wait=True`` ensures the upsert is durable before we
            # commit the Chunk rows that reference it. The worker
            # pipeline relies on this ordering.
            wait=True,
        )
    except UnexpectedResponse as exc:
        log.warning(
            "qdrant.upsert.failed",
            collection=collection,
            point_count=len(points),
            status_code=getattr(exc, "status_code", None),
            error_type=type(exc).__name__,
        )
        return 0
    except Exception as exc:
        # ConnectionError, timeout, validation that bubbled up as a
        # non-UnexpectedResponse, etc.
        log.warning(
            "qdrant.upsert.error",
            collection=collection,
            point_count=len(points),
            error_type=type(exc).__name__,
        )
        return 0

    log.info(
        "qdrant.upsert.success",
        collection=collection,
        point_count=len(points),
    )
    return len(points)


async def delete_points_by_article_version(
    *,
    collection: str,
    article_version_id: str,
) -> int:
    """Delete all Qdrant points matching the article_version_id payload.

    Used by the indexer (Task 6.7) to clean stale vectors before
    upserting a freshly-chunked version. Returns the best-effort count
    deleted (Qdrant's delete API does not echo a number on success;
    we return ``1`` to indicate "operation succeeded, count unknown"
    on a successful call and ``0`` on any error). Never raises.

    PII-safety: logs carry only the collection name + the opaque
    article_version_id + error_type. No point IDs or payload contents.
    """
    try:
        client = get_qdrant_client()
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(
            "qdrant.delete.client_unavailable",
            collection=collection,
            error_type=type(exc).__name__,
        )
        return 0

    delete_filter = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="article_version_id",
                match=qmodels.MatchValue(value=article_version_id),
            )
        ]
    )

    try:
        await client.delete(
            collection_name=collection,
            points_selector=delete_filter,
            wait=True,
        )
    except UnexpectedResponse as exc:
        log.warning(
            "qdrant.delete.failed",
            collection=collection,
            status_code=getattr(exc, "status_code", None),
            error_type=type(exc).__name__,
        )
        return 0
    except Exception as exc:
        log.warning(
            "qdrant.delete.error",
            collection=collection,
            error_type=type(exc).__name__,
        )
        return 0

    log.info(
        "qdrant.delete.success",
        collection=collection,
    )
    return 1


# ---------------------------------------------------------------------------
# Tenant-scoped search helper used by the retriever (Task 6.11)
# ---------------------------------------------------------------------------


async def search_chunks(
    *,
    collection: str,
    query_vector: list[float],
    tenant_id: str,
    knowledge_base_id: str,
    top_k: int,
) -> list[qmodels.ScoredPoint]:
    """Run a Qdrant similarity search with mandatory tenant + KB payload filters.

    Returns a list of :class:`qmodels.ScoredPoint` (highest score first).
    Returns ``[]`` on no match or on any error — never raises.

    The two MUST-filter payload conditions make this the
    single, canonical tenant-isolation boundary for vector reads:

    * ``tenant_id`` — prevents cross-tenant leakage at the vector
      layer. Defense-in-depth alongside the DB-side WHERE clause
      added by :meth:`knowledge.repository.ChunkRepository.list_by_point_ids`.
    * ``knowledge_base_id`` — scopes the search to one KB within a
      tenant (the same tenant may host multiple KBs).

    qdrant-client API note
    ----------------------

    qdrant-client >= 1.14 unified the old ``search`` /
    ``search_batch`` / ``recommend`` / ``discover`` / ``scroll``
    entry points into a single ``query_points`` method that takes a
    ``query`` parameter (one of the supported query types — for our
    plain-vector case, the bare ``list[float]`` is interpreted as a
    nearest-neighbor query against the default vector). The older
    ``search`` method was removed in 1.19. We call ``query_points``
    directly so the helper works across the 1.14+ client range and
    so a future Stage 7+ provider switch (e.g. to ``recommend``)
    only changes this one call site.

    PII discipline
    --------------

    On error we log ``collection`` + ``top_k`` + ``error_type`` only.
    NEVER the query vector (high-dimensional floats are not PII per
    se, but logging them bloats the log line and risks leaking
    embedding artifacts) and NEVER the query text (passed in by the
    caller — the retriever already takes care to not log it).
    """
    flt = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="tenant_id",
                match=qmodels.MatchValue(value=tenant_id),
            ),
            qmodels.FieldCondition(
                key="knowledge_base_id",
                match=qmodels.MatchValue(value=knowledge_base_id),
            ),
        ]
    )

    try:
        client = get_qdrant_client()
    except Exception as exc:  # pragma: no cover - defensive; singleton rarely raises
        log.warning(
            "qdrant.search.client_unavailable",
            collection=collection,
            top_k=top_k,
            error_type=type(exc).__name__,
        )
        return []

    try:
        response = await client.query_points(
            collection_name=collection,
            # A bare ``list[float]`` is the nearest-neighbor form —
            # equivalent to the old ``search(query_vector=...)``
            # call. qdrant-client >= 1.14 unifies all query types
            # behind this single entry point.
            query=query_vector,
            query_filter=flt,
            limit=top_k,
            # We hydrate Chunk rows from the DB; Qdrant payload is
            # redundant for this call path and would only inflate the
            # wire payload (the worker writes it for scroll /
            # admin-side debugging).
            with_payload=False,
            with_vectors=False,
            # Score threshold is applied in :func:`retrieve_chunks` so
            # the operator-facing filter logic stays in one place.
            score_threshold=None,
        )
    except UnexpectedResponse as exc:
        log.warning(
            "qdrant.search.failed",
            collection=collection,
            top_k=top_k,
            status_code=getattr(exc, "status_code", None),
            error_type=type(exc).__name__,
        )
        return []
    except Exception as exc:
        # ConnectionError / timeout / anything that bubbled up as a
        # non-UnexpectedResponse. Never raise — the retriever treats
        # an empty result as "no hits", and we don't want a flaky
        # Qdrant to fail the whole request.
        log.warning(
            "qdrant.search.error",
            collection=collection,
            top_k=top_k,
            error_type=type(exc).__name__,
        )
        return []

    # ``query_points`` returns a QueryResponse; the ``points``
    # attribute holds the scored list (highest first). Defensive
    # ``list(...)`` so callers always get a real list even if a
    # future client version returns a generator.
    scored_points = list(response.points) if response.points else []

    log.info(
        "qdrant.search.success",
        collection=collection,
        top_k=top_k,
        hit_count=len(scored_points),
    )
    return scored_points
