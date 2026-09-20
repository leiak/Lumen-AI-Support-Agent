"""Startup-time Qdrant collection initialization.

Thin orchestration layer called from the FastAPI lifespan: reads the
desired collection name / vector size / distance (currently fixed for
M1, will move to per-KB settings later) and delegates to
:func:`knowledge.qdrant_client.ensure_collection`. Failures are logged
but never re-raised — the API must boot even if Qdrant is briefly
unavailable, and the health endpoint will report the degraded state.

Stage 17 / M2.B Task 6 — adds :func:`ensure_image_collection` for the
new ``kb_image_vectors`` collection that holds vision-embedder
outputs (Doubao ``doubao-embedding-vision`` 1024-dim by default).
"""
from __future__ import annotations

from qdrant_client import AsyncQdrantClient

from core.config import get_settings
from core.logging import get_logger
from knowledge.qdrant_client import (
    DEFAULT_COLLECTION,
    DEFAULT_DISTANCE,
    vector_size_for_model,
    ensure_collection,
)

log = get_logger(__name__)


# Stage 17 / M2.B Task 6 — the new image-vector collection. Single
# collection per env for now (mirrors the M1 ``article_chunks``
# pattern); per-tenant / per-KB isolation comes from the MUST-filter
# payload conditions (``tenant_id`` + ``kb_slug``) added at every
# read / write site.
IMAGE_COLLECTION = "kb_image_vectors"

# Vector dimensionality for ``doubao-embedding-vision``. The class
# attr ``VisionEmbedder.dimension`` declares the same constant at
# 1024 (see ``knowledge/multimodal/embedder.py``); we keep this
# constant here too so the startup hook doesn't have to construct a
# throwaway embedder instance just to read its dimension.
#
# MUST match ``VisionEmbedder.dimension``. Update both if the
# model ever changes — the Qdrant collection is sized off this
# number at create time, and a mismatch silently corrupts search
# results.
DEFAULT_VISION_DIMENSION = 1024


async def ensure_qdrant_collection() -> bool:
    """Ensure the M1 ``article_chunks`` Qdrant collection is ready.

    Returns True if the collection exists (or was created), False on
    any failure. The return value is informational — callers should
    not abort startup on False; the /health endpoint will surface the
    real status.

    Vector size is derived from the configured embedding model via
    :func:`knowledge.qdrant_client.vector_size_for_model`. An unknown
    model raises ``ValueError`` so a misconfigured
    ``DEFAULT_EMBEDDING_MODEL`` fails loud at boot, not silent later.
    """
    settings = get_settings()
    vector_size = vector_size_for_model(settings.default_embedding_model)
    log.info(
        "qdrant.collection.starting",
        collection=DEFAULT_COLLECTION,
        vector_size=vector_size,
        distance=DEFAULT_DISTANCE,
        embedding_model=settings.default_embedding_model,
    )
    success = await ensure_collection(
        name=DEFAULT_COLLECTION,
        vector_size=vector_size,
        distance=DEFAULT_DISTANCE,
    )
    if success:
        log.info("qdrant.collection.ready", collection=DEFAULT_COLLECTION)
    else:
        # WARNING is also emitted by ensure_collection itself; this
        # is the startup-orchestration level signal.
        log.warning("qdrant.collection.startup_failed", collection=DEFAULT_COLLECTION)
    return success


async def ensure_image_collection(
    qdrant_client: AsyncQdrantClient,
    *,
    dimension: int = DEFAULT_VISION_DIMENSION,
) -> bool:
    """Ensure the ``kb_image_vectors`` collection exists.

    Stage 17 / M2.B Task 6 — counterpart to
    :func:`ensure_qdrant_collection` for vision embeddings. Called
    once at app startup AND defensively inside the upload handler
    (a fresh Qdrant after a restart may not have the collection yet
    when the very first upload lands).

    Returns ``True`` on success / already-exists, ``False`` on any
    failure. Never raises — the upload handler downgrades a missing
    collection to a 503 (Qdrant is degraded) so the API never crashes
    the caller.

    PII discipline
    --------------
    The collection name + the dimension + the exception class name
    are the only fields in the log payload. We never log the model
    name from the embedder here (it would be redundant — the
    ``upload_multimodal`` log line already carries it).
    """
    try:
        if await qdrant_client.collection_exists(IMAGE_COLLECTION):
            log.info(
                "qdrant.image_collection.exists",
                collection=IMAGE_COLLECTION,
            )
            return True
        await qdrant_client.create_collection(
            collection_name=IMAGE_COLLECTION,
            vectors_config={
                "size": dimension,
                "distance": "Cosine",
            },
        )
        log.info(
            "qdrant.image_collection.created",
            collection=IMAGE_COLLECTION,
            vector_size=dimension,
        )
        return True
    except Exception as exc:
        # CollectionExists (race lost) is success — qdrant-client
        # 1.x raises ``ValueError`` with "already exists" in the
        # message. We catch it broadly so the log payload stays
        # PII-safe (no exception repr).
        msg = str(exc).lower()
        if "already exists" in msg:
            log.info(
                "qdrant.image_collection.race_lost",
                collection=IMAGE_COLLECTION,
            )
            return True
        log.warning(
            "qdrant.image_collection.ensure_failed",
            collection=IMAGE_COLLECTION,
            vector_size=dimension,
            error_type=type(exc).__name__,
        )
        return False
