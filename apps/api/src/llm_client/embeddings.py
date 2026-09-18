"""Public embedding API for the LLM client.

`embed_texts` is the entrypoint used by the knowledge pipeline (Stage 6).
It batches inputs at OpenAI's 2048-text-per-call limit, calls the embedding
provider per batch concurrently (bounded by a semaphore), and aggregates
the results in input order.

PII contract: log lines MUST NOT include raw text content. Allowed fields:
`tenant_id` (opaque), `model`, `text_count`, `prompt_tokens`, `total_tokens`,
`error_type`.
"""
import asyncio

import openai

from core.config import get_settings
from core.logging import get_logger
from llm_client.providers.openai_embedding_provider import OpenAIEmbeddingProvider
from llm_client.types import EmbeddingError, EmbeddingResult

_OPENAI_BATCH_LIMIT = 2048
_MAX_RETRIES = 3

# Cap concurrent in-flight embedding batches. OpenAI's per-org rate limit
# applies across all parallel calls; 4 is a safe M1 default for a single
# process. Raise if you have headroom.
_BATCH_CONCURRENCY = 4
_batch_semaphore = asyncio.Semaphore(_BATCH_CONCURRENCY)

# Module-level singleton so callers without DI don't leak an `AsyncOpenAI`
# socket per call. Reset by `aclose_default_client()` (called from the API
# lifespan on shutdown and from tests between cases).
_client_singleton: openai.AsyncOpenAI | None = None

# Re-exported so callers can keep importing from `llm_client.embeddings`.
__all__ = ["EmbeddingError", "EmbeddingResult", "aclose_default_client", "embed_texts"]


def _get_default_client() -> openai.AsyncOpenAI:
    """Return the module-level `AsyncOpenAI` singleton, creating it lazily.

    Routing priority:
      1. If ``DOUBAO_API_KEY`` is set → use the Doubao/Ark base URL + Doubao key
         (serves Doubao embedding models like ``doubao-embedding``).
      2. Otherwise → use ``OPENAI_API_KEY`` against OpenAI's default endpoint.

    The OpenAI Python SDK is OpenAI-compatible, so it talks to either server
    without code changes in the provider layer. The singleton is cached so
    subsequent calls reuse the same HTTPX connection pool.

    Re-init after ``reset_settings()`` requires a manual
    ``_reset_default_client_for_tests()`` (called by test teardown).
    """
    global _client_singleton
    if _client_singleton is None:
        settings = get_settings()
        if settings.doubao_api_key:
            _client_singleton = openai.AsyncOpenAI(
                api_key=settings.doubao_api_key,
                base_url=settings.doubao_base_url,
            )
        else:
            _client_singleton = openai.AsyncOpenAI(api_key=settings.openai_api_key)
    return _client_singleton


async def aclose_default_client() -> None:
    """Close the singleton client (if any) and clear the reference.

    Idempotent. Called from the API lifespan on shutdown so the underlying
    HTTPX connection pool is released.
    """
    global _client_singleton
    if _client_singleton is not None:
        # `close()` on `AsyncOpenAI` is async in openai >= 1.30 (and remains
        # the supported teardown in this project's pinned 1.x line).
        await _client_singleton.close()
        _client_singleton = None


def _reset_default_client_for_tests() -> None:
    """Test helper: drop the singleton reference without closing it.

    Tests inject their own client via ``client=`` so the singleton isn't
    actually used; this just prevents cross-test pollution if a previous
    test inadvertently triggered lazy init.
    """
    global _client_singleton
    _client_singleton = None


async def _embed_batch_with_semaphore(
    provider: OpenAIEmbeddingProvider,
    batch: list[str],
    model: str,
) -> EmbeddingResult:
    async with _batch_semaphore:
        return await provider.embed(batch, model)


async def embed_texts(
    *,
    texts: list[str],
    model: str | None = None,
    tenant_id: str | None = None,
    client: openai.AsyncOpenAI | None = None,
) -> EmbeddingResult:
    """Embed a batch of texts via the OpenAI-compatible embeddings API.

    Splits `texts` into chunks of ≤ 2048 (OpenAI's per-call limit) and calls
    the provider concurrently per chunk (bounded by ``_BATCH_CONCURRENCY``).
    Returns vectors in input order with token usage summed across batches.

    Args:
        texts: Strings to embed.
        model: Embedding model ID. Default: ``text-embedding-3-small``.
        tenant_id: Tenant opaque ID, logged for traceability. Never the API
            key — that lives in settings.
        client: Optional ``AsyncOpenAI`` instance for DI / testing. If
            omitted, the module-level singleton is used (and lazily created
            from ``settings.openai_api_key``).

    Returns:
        ``EmbeddingResult`` with vectors in input order and aggregated usage.

    Raises:
        EmbeddingError: On permanent failure (4xx, auth, rate-limit
            exhausted, etc.).
    """
    # Resolve the embedding model lazily so settings changes (env override
    # in tests, hot-reload) propagate without a process restart. The
    # caller may still pass `model` explicitly to override per-KB.
    if model is None:
        model = get_settings().default_embedding_model

    log = get_logger("llm.embedding")
    log.info(
        "embedding_request",
        tenant_id=tenant_id,
        model=model,
        text_count=len(texts),
    )

    if not texts:
        log.info(
            "embedding_empty",
            tenant_id=tenant_id,
            model=model,
        )
        return EmbeddingResult(vectors=[], model=model, prompt_tokens=0, total_tokens=0)

    if client is None:
        client = _get_default_client()

    provider = OpenAIEmbeddingProvider(client=client, max_retries=_MAX_RETRIES)

    batches = [
        texts[start : start + _OPENAI_BATCH_LIMIT]
        for start in range(0, len(texts), _OPENAI_BATCH_LIMIT)
    ]

    # Run all batches concurrently (bounded by the semaphore). Even though
    # batches may complete out of order, `asyncio.gather` preserves input
    # order in the returned list.
    batch_results = await asyncio.gather(
        *(_embed_batch_with_semaphore(provider, batch, model) for batch in batches)
    )

    all_vectors: list[list[float]] = []
    prompt_tokens_total = 0
    total_tokens_total = 0
    for chunk_result in batch_results:
        all_vectors.extend(chunk_result.vectors)
        prompt_tokens_total += chunk_result.prompt_tokens
        total_tokens_total += chunk_result.total_tokens

    log.info(
        "embedding_success",
        tenant_id=tenant_id,
        model=model,
        text_count=len(texts),
        prompt_tokens=prompt_tokens_total,
        total_tokens=total_tokens_total,
    )

    return EmbeddingResult(
        vectors=all_vectors,
        model=model,
        prompt_tokens=prompt_tokens_total,
        total_tokens=total_tokens_total,
    )