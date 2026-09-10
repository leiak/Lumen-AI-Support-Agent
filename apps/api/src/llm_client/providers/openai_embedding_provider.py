"""OpenAI /v1/embeddings API adapter.

Wraps `openai.AsyncOpenAI` for batch embedding generation. Retries on
`openai.RateLimitError` (up to `max_retries` attempts with exponential
backoff + jitter). Other errors propagate as `EmbeddingError` immediately.

`EmbeddingResult` / `EmbeddingError` live in `llm_client.types` to avoid a
circular import with `llm_client.embeddings` (the public module).
"""
from __future__ import annotations

import asyncio
import random

import openai

from core.logging import get_logger
from llm_client.types import EmbeddingError, EmbeddingResult


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
        up to `max_retries` times with exponential backoff + jitter
        (1s, 2s, 4s, ... + 0..0.5s).
        """
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
                    model=model,  # trust the caller's requested alias
                    prompt_tokens=prompt_tokens,
                    total_tokens=total_tokens,
                )
            except openai.RateLimitError as e:
                last_exc = e
                if attempt == self._max_retries:
                    break
                # Exponential backoff with jitter (matches LLMClient.chat):
                # 2^attempt + uniform(0, 0.5).
                backoff = self._base_backoff * (2**attempt) + random.uniform(0, 0.5)  # noqa: S311
                log.warning(
                    "embedding_rate_limit",
                    model=model,
                    text_count=len(texts),
                    attempt=attempt + 1,
                    backoff_seconds=backoff,
                )
                await asyncio.sleep(backoff)
                attempt += 1
            except Exception as e:
                # Includes APIError, BadRequestError, AuthenticationError, etc.
                # Do NOT retry — propagate immediately as EmbeddingError.
                # Use repr(e) (not str(e)) because OpenAI AuthenticationError
                # __str__ may include the failing API-key prefix.
                log.warning(
                    "embedding_api_error",
                    error_type=type(e).__name__,
                    error_repr=repr(e),
                    model=model,
                    text_count=len(texts),
                )
                raise EmbeddingError(
                    f"OpenAI embedding error ({type(e).__name__})"
                ) from e

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