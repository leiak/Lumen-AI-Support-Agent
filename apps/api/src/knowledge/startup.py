"""Startup-time Qdrant collection initialization.

Thin orchestration layer called from the FastAPI lifespan: reads the
desired collection name / vector size / distance (currently fixed for
M1, will move to per-KB settings later) and delegates to
:func:`knowledge.qdrant_client.ensure_collection`. Failures are logged
but never re-raised — the API must boot even if Qdrant is briefly
unavailable, and the health endpoint will report the degraded state.
"""
from __future__ import annotations

from core.logging import get_logger
from knowledge.qdrant_client import (
    DEFAULT_COLLECTION,
    DEFAULT_DISTANCE,
    DEFAULT_VECTOR_SIZE,
    ensure_collection,
)

log = get_logger(__name__)


async def ensure_qdrant_collection() -> bool:
    """Ensure the M1 ``article_chunks`` Qdrant collection is ready.

    Returns True if the collection exists (or was created), False on
    any failure. The return value is informational — callers should
    not abort startup on False; the /health endpoint will surface the
    real status.
    """
    log.info(
        "qdrant.collection.starting",
        collection=DEFAULT_COLLECTION,
        vector_size=DEFAULT_VECTOR_SIZE,
        distance=DEFAULT_DISTANCE,
    )
    success = await ensure_collection(
        name=DEFAULT_COLLECTION,
        vector_size=DEFAULT_VECTOR_SIZE,
        distance=DEFAULT_DISTANCE,
    )
    if success:
        log.info("qdrant.collection.ready", collection=DEFAULT_COLLECTION)
    else:
        # WARNING is also emitted by ensure_collection itself; this
        # is the startup-orchestration level signal.
        log.warning("qdrant.collection.startup_failed", collection=DEFAULT_COLLECTION)
    return success
