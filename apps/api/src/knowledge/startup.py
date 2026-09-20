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

# Stage 19 / M3 / tech-debt #19: per-provider collection names so each
# vision embedder writes to its own Qdrant collection (different
# dimensionalities cannot coexist in one collection). Legacy
# ``IMAGE_COLLECTION = "kb_image_vectors"`` is now an alias for the
# Doubao collection (see ``ensure_image_collection`` below).
_PROVIDER_COLLECTION_NAMES: dict[str, str] = {
    "doubao": "kb_image_vectors_doubao",
    "openai_clip": "kb_image_vectors_clip",
    "voyage": "kb_image_vectors_voyage",
}


def get_image_collection_name(provider: str) -> str:
    """Return the Qdrant collection name for the given vision provider.

    Raises ``ValueError`` for unknown providers — fail loud at boot
    rather than at the first upload.
    """
    try:
        return _PROVIDER_COLLECTION_NAMES[provider]
    except KeyError as exc:
        raise ValueError(
            f"unknown vision_provider: {provider!r}"
        ) from exc


# Backward-compat alias. New code MUST call get_image_collection_name.
# This constant points at the Doubao collection name (via the alias
# below) so legacy code that imports ``IMAGE_COLLECTION`` keeps working.
IMAGE_COLLECTION = "kb_image_vectors"


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
    dimension: int | None = None,
    provider: str | None = None,
) -> bool:
    """Ensure the vision-embedder Qdrant collection exists for the
    configured provider.

    Stage 19 / M3 / tech-debt #19: each provider writes to its own
    collection. The legacy ``kb_image_vectors`` name is created as an
    alias pointing at the provider's physical collection so existing
    data + tests that reference the old name continue to work.

    Returns ``True`` on success / already-exists, ``False`` on any
    failure. Never raises.
    """
    if provider is None:
        provider = get_settings().vision_provider
    collection_name = get_image_collection_name(provider)
    if dimension is None:
        # Read the dimension from a per-provider dispatch. Avoids
        # constructing a real embedder just to read its ``dimension``
        # ClassVar.
        dim_map = {"doubao": 1024, "openai_clip": 768, "voyage": 1024}
        dimension = dim_map[provider]

    try:
        if not await qdrant_client.collection_exists(collection_name):
            await qdrant_client.create_collection(
                collection_name=collection_name,
                vectors_config={"size": dimension, "distance": "Cosine"},
            )
            log.info(
                "qdrant.image_collection.created",
                collection=collection_name,
                provider=provider,
                vector_size=dimension,
            )
        else:
            log.info(
                "qdrant.image_collection.exists",
                collection=collection_name,
                provider=provider,
            )

        # Register the legacy alias → provider collection. Skip if
        # the alias name conflicts with a physical collection
        # (would happen on upgrades from M2.B where kb_image_vectors
        # was a real collection, not an alias — log WARNING so the
        # operator knows).
        try:
            aliases = await qdrant_client.get_collection_aliases(
                collection_name=collection_name
            )
            alias_names = {a.alias_name for a in aliases.aliases}
            if IMAGE_COLLECTION not in alias_names:
                from qdrant_client.http import models as qmodels
                await qdrant_client.update_collection_aliases(
                    changes=[
                        qmodels.AliasOperations(
                            create_alias=qmodels.CreateAlias(
                                alias_name=IMAGE_COLLECTION,
                                collection_name=collection_name,
                            )
                        )
                    ]
                )
                log.info(
                    "qdrant.image_collection.alias_created",
                    alias=IMAGE_COLLECTION,
                    target=collection_name,
                )
        except Exception as exc:
            log.warning(
                "qdrant.image_collection.alias_creation_failed",
                alias=IMAGE_COLLECTION,
                target=collection_name,
                error_type=type(exc).__name__,
            )

        return True
    except Exception as exc:
        msg = str(exc).lower()
        if "already exists" in msg:
            log.info(
                "qdrant.image_collection.race_lost",
                collection=collection_name,
            )
            return True
        log.warning(
            "qdrant.image_collection.ensure_failed",
            collection=collection_name,
            vector_size=dimension,
            error_type=type(exc).__name__,
        )
        return False
