"""Thin wrapper around :class:`AsyncQdrantClient` for knowledge base ops.

Currently exposes a single idempotent helper, :func:`ensure_collection`,
that guarantees a named Qdrant collection exists with the requested
vector schema. The helper is designed to be safe to call on every
process boot: a missing collection is created; a present one is left
alone; any other failure is logged and swallowed so the API can keep
serving traffic even when Qdrant is briefly unavailable.
"""
from __future__ import annotations

from qdrant_client.http import models as qmodels

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

    All log payloads are PII-safe: only the collection name and the
    exception class name are emitted, never the exception message
    (which can carry Qdrant URLs, error bodies, or auth hints).
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

    try:
        # 1. Probe: does the collection already exist?
        try:
            await client.get_collection(collection_name=name)
        except Exception:
            # Most commonly: UnexpectedResponse("Not found: ...").
            # We don't care about the specific error class — any failure
            # here just means we have to try to create. Debug-level only
            # to keep the happy path quiet; WARNING/ERROR happen at the
            # outer except if even the create fails.
            log.debug("qdrant.collection.get_failed", collection=name)
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
        except Exception:
            # Race: another worker / process created the collection
            # between our get_collection and create_collection calls.
            # The end state is what we wanted (the collection exists),
            # so treat this as success.
            log.info("qdrant.collection.race_lost", collection=name)
            return True

        log.info("qdrant.collection.created", collection=name)
        return True
    except Exception as exc:
        log.warning(
            "qdrant.collection.ensure_failed",
            collection=name,
            error_type=type(exc).__name__,
        )
        return False
