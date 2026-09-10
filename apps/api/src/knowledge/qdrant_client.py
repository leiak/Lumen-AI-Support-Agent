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
