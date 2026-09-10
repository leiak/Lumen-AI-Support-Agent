"""High-level LLM client: wraps a provider, adds retry, records usage."""
import asyncio
import random
import uuid

from llm_client.exceptions import (
    InvalidRequest,
    OutputInvalid,
    ProviderUnavailable,
    RateLimited,
)
from llm_client.providers.base import BaseProvider
from llm_client.types import ChatRequest, ChatResponse
from llm_client.usage import UsageRecorder


class LLMClient:
    """Wraps a single provider with retry + usage recording.

    - Retry: 5xx (ProviderUnavailable), unparseable responses (OutputInvalid)
    - No retry: 429 (RateLimited), 4xx (InvalidRequest)
    - On success: enqueue a usage row (flushed via flush_usage())
    """

    def __init__(
        self,
        *,
        default_provider: BaseProvider,
        tenant_id: str,
        usage_recorder: UsageRecorder | None = None,
    ) -> None:
        self.default_provider = default_provider
        self.tenant_id = tenant_id
        self.usage = usage_recorder or UsageRecorder()

    async def aclose(self) -> None:
        """Flush pending usage and close the underlying provider's HTTP client."""
        await self.flush_usage()
        if hasattr(self.default_provider, "aclose"):
            await self.default_provider.aclose()

    async def flush_usage(self) -> None:
        await self.usage.flush()

    async def chat(
        self,
        request: ChatRequest,
        *,
        max_retries: int = 3,
    ) -> ChatResponse:
        """Send a chat request with retry.

        Retries on ProviderUnavailable / OutputInvalid up to `max_retries` times
        with exponential backoff + jitter. Does NOT retry on RateLimited /
        InvalidRequest (those are caller's mistake or upstream throttling).
        """
        request_id = uuid.uuid4().hex
        attempt = 0
        last_exc: Exception | None = None
        while attempt <= max_retries:
            try:
                resp = await self.default_provider.chat(request)
                self.usage.enqueue(
                    tenant_id=self.tenant_id,
                    provider=self.default_provider.name,
                    model=resp.model,
                    prompt_tokens=resp.prompt_tokens,
                    completion_tokens=resp.completion_tokens,
                    request_id=request_id,
                )
                return resp
            except (RateLimited, InvalidRequest):
                # 429 / 4xx: don't retry, propagate immediately
                raise
            except (ProviderUnavailable, OutputInvalid) as e:
                last_exc = e
                if attempt == max_retries:
                    break
                # Exponential backoff with jitter: 0..0.5s + 2^attempt, capped at 8s
                backoff = min(2 ** attempt, 8) + random.uniform(0, 0.5)  # noqa: S311
                await asyncio.sleep(backoff)
                attempt += 1

        # Exhausted retries — raise the last exception
        assert last_exc is not None
        raise last_exc
