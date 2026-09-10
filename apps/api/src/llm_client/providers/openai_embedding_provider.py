"""OpenAI /v1/embeddings API adapter.

Wraps `openai.AsyncOpenAI` for batch embedding generation. Retries on
`openai.RateLimitError` (up to `max_retries` attempts with exponential
backoff). Other errors propagate as `EmbeddingError` immediately.

`EmbeddingResult` / `EmbeddingError` are imported lazily to avoid a
circular import with `llm_client.embeddings` (the public module).
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import openai

from core.logging import get_logger

if TYPE_CHECKING:
    from llm_client.embeddings import EmbeddingResult


class OpenAIEmbeddingProvider:
    """Adapter for OpenAI's /v1/embeddings endpoint and compatible servers.

    Args:
        client: An initialized `openai.AsyncOpenAI` instance. Injected so
            tests can substitute a mock.
        max_retries: Number of *additional* attempts after the first one when
            the provider returns a `RateLimitError`. Total attempts =
            1 + max_retries.
        base_backoff_seconds: Base for exponential backoff (1s, 2s, 4s, ...
            by default). Set to 0 in tests to keep them fast.
    """

    name = "openai-embedding"

    def __init__(
        self,
        *,
        client: openai.AsyncOpenAI,
        max_retries: int = 3,
        base_backoff_seconds: float = 1.0,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if base_backoff_seconds < 0:
            raise ValueError("base_backoff_seconds must be >= 0")
        self._client = client
        self._max_retries = max_retries
        self._base_backoff = base_backoff_seconds

    async def embed(self, texts: list[str], model: str) -> EmbeddingResult:
        """Embed a single batch (≤ 2048 texts) and return vectors in input order.

        Raises `EmbeddingError` on permanent failure. Retries `RateLimitError`
        up to `max_retries` times with exponential backoff (1s, 2s, 4s, ...).
        """
        # Lazy import to break circular dependency with llm_client.embeddings.
        from llm_client.embeddings import EmbeddingError, EmbeddingResult

        log = get_logger("llm.embedding")
        attempt = 0
        last_exc: Exception | None = None
        while attempt <= self._max_retries:
            try:
                resp = await self._client.embeddings.create(
                    model=model,
                    input=texts,
                    encoding_format="float",
                )
                # OpenAI returns `data` in input order.
                vectors = [item.embedding for item in resp.data]
                usage = resp.usage
                prompt_tokens = usage.prompt_tokens if usage else 0
                total_tokens = usage.total_tokens if usage else 0
                return EmbeddingResult(
                    vectors=vectors,
                    model=resp.model,
                    prompt_tokens=prompt_tokens,
                    total_tokens=total_tokens,
                )
            except openai.RateLimitError as e:
                last_exc = e
                if attempt == self._max_retries:
                    break
                backoff = self._base_backoff * (2**attempt)
                log.warning(
                    "embedding_rate_limit",
                    model=model,
                    text_count=len(texts),
                    attempt=attempt + 1,
                    backoff_seconds=backoff,
                )
                await asyncio.sleep(backoff)
                attempt += 1
            except openai.APIError as e:
                # Includes BadRequestError, AuthenticationError, etc.
                # Do NOT retry — propagate immediately as EmbeddingError.
                log.warning(
                    "embedding_api_error",
                    error_type=type(e).__name__,
                    model=model,
                    text_count=len(texts),
                )
                raise EmbeddingError(f"OpenAI embedding error: {e}") from e

        # Exhausted retries on rate limit.
        assert last_exc is not None
        log.warning(
            "embedding_rate_limit_exhausted",
            model=model,
            text_count=len(texts),
            max_retries=self._max_retries,
        )
        raise EmbeddingError(
            f"OpenAI rate limit after {self._max_retries} retries"
        ) from last_exc