"""Public embedding API for the LLM client.

`embed_texts` is the entrypoint used by the knowledge pipeline (Stage 6).
It batches inputs at OpenAI's 2048-text-per-call limit, calls the embedding
provider per batch, and aggregates the results in input order.

PII contract: log lines MUST NOT include raw text content. Allowed fields:
`tenant_id` (opaque), `model`, `text_count`, `prompt_tokens`, `total_tokens`,
`error_type`.
"""
from dataclasses import dataclass

import openai

from core.config import get_settings
from core.logging import get_logger
from llm_client.providers.openai_embedding_provider import OpenAIEmbeddingProvider

_DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
_OPENAI_BATCH_LIMIT = 2048
_MAX_RETRIES = 3


@dataclass(frozen=True)
class EmbeddingResult:
    """Result of an embedding batch call.

    `vectors[i]` corresponds to `texts[i]` (OpenAI preserves input order).
    Token counts are aggregated across all batches in the original call.
    """

    vectors: list[list[float]]
    model: str
    prompt_tokens: int
    total_tokens: int


class EmbeddingError(Exception):
    """Raised when embedding generation fails after retries.

    Wraps rate-limit exhaustion and non-retryable API errors (4xx, auth, etc.).
    """


async def embed_texts(
    *,
    texts: list[str],
    model: str = _DEFAULT_EMBEDDING_MODEL,
    tenant_id: str | None = None,
    client: openai.AsyncOpenAI | None = None,
) -> EmbeddingResult:
    """Embed a batch of texts via the OpenAI-compatible embeddings API.

    Splits `texts` into chunks of ≤ 2048 (OpenAI's per-call limit) and calls
    the provider once per chunk. Returns vectors in input order with token
    usage summed across batches.

    Args:
        texts: Strings to embed.
        model: Embedding model ID. Default: ``text-embedding-3-small``.
        tenant_id: Tenant opaque ID, logged for traceability. Never the API
            key — that lives in settings.
        client: Optional ``AsyncOpenAI`` instance for DI / testing. If
            omitted, one is created from ``settings.openai_api_key``.

    Returns:
        ``EmbeddingResult`` with vectors in input order and aggregated usage.

    Raises:
        EmbeddingError: On permanent failure (4xx, auth, rate-limit
            exhausted, etc.).
    """
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
        settings = get_settings()
        client = openai.AsyncOpenAI(api_key=settings.openai_api_key)

    provider = OpenAIEmbeddingProvider(client=client, max_retries=_MAX_RETRIES)

    all_vectors: list[list[float]] = []
    prompt_tokens_total = 0
    total_tokens_total = 0
    model_used = model

    for start in range(0, len(texts), _OPENAI_BATCH_LIMIT):
        chunk = texts[start : start + _OPENAI_BATCH_LIMIT]
        chunk_result = await provider.embed(texts=chunk, model=model)
        all_vectors.extend(chunk_result.vectors)
        prompt_tokens_total += chunk_result.prompt_tokens
        total_tokens_total += chunk_result.total_tokens
        model_used = chunk_result.model

    log.info(
        "embedding_success",
        tenant_id=tenant_id,
        model=model_used,
        text_count=len(texts),
        prompt_tokens=prompt_tokens_total,
        total_tokens=total_tokens_total,
    )

    return EmbeddingResult(
        vectors=all_vectors,
        model=model_used,
        prompt_tokens=prompt_tokens_total,
        total_tokens=total_tokens_total,
    )