"""Startup-time Qdrant collection initialization.

Thin orchestration layer called from the FastAPI lifespan: reads the
desired collection name / vector size / distance (currently fixed for
M1, will move to per-KB settings later) and delegates to
:func:`knowledge.qdrant_client.ensure_collection`. Failures are logged
but never re-raised — the API must boot even if Qdrant is briefly
unavailable, and the health endpoint will report the degraded state.
"""
from __future__ import annotations

from core.config import get_settings
from core.logging import get_logger
from knowledge.qdrant_client import (
    DEFAULT_COLLECTION,
    DEFAULT_DISTANCE,
    vector_size_for_model,
    ensure_collection,
)

log = get_logger(__name__)


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
