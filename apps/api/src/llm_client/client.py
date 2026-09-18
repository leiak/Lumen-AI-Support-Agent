"""High-level LLM client: wraps a provider, adds retry, records usage."""
import asyncio
import random
import uuid
from collections.abc import AsyncIterator

from core.business_metrics import LLM_CALLS_TOTAL, LLM_TOKENS_TOTAL
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
                # Stage 11.3: success metric. We only inc on the final
                # successful attempt — earlier retries that failed and
                # bounced are accounted by the failure-path incs below.
                _provider = self.default_provider.name
                LLM_CALLS_TOTAL.labels(
                    provider=_provider, model=resp.model, outcome="success"
                ).inc()
                LLM_TOKENS_TOTAL.labels(
                    provider=_provider, model=resp.model, direction="input"
                ).inc(resp.prompt_tokens)
                LLM_TOKENS_TOTAL.labels(
                    provider=_provider, model=resp.model, direction="output"
                ).inc(resp.completion_tokens)
                return resp
            except RateLimited as e:
                # 429: don't retry, propagate immediately. Outcome label
                # mirrors the exception class name so dashboards can
                # split "rate_limited" vs "invalid_request" cleanly.
                LLM_CALLS_TOTAL.labels(
                    provider=self.default_provider.name,
                    model=request.model,
                    outcome="rate_limited",
                ).inc()
                raise
            except InvalidRequest as e:
                # 4xx (other than 429): same as RateLimited — propagate.
                LLM_CALLS_TOTAL.labels(
                    provider=self.default_provider.name,
                    model=request.model,
                    outcome="invalid_request",
                ).inc()
                raise
            except ProviderUnavailable as e:
                # 5xx / network: inc the retryable outcome and back off.
                # We inc once per attempt that hit this branch; the
                # final-exhaustion raise below is not a separate inc.
                LLM_CALLS_TOTAL.labels(
                    provider=self.default_provider.name,
                    model=request.model,
                    outcome="unavailable",
                ).inc()
                last_exc = e
                if attempt == max_retries:
                    break
                # Exponential backoff with jitter: 0..0.5s + 2^attempt, capped at 8s
                backoff = min(2 ** attempt, 8) + random.uniform(0, 0.5)  # noqa: S311
                await asyncio.sleep(backoff)
                attempt += 1
            except OutputInvalid as e:
                # Provider returned unparseable response. Treat as a
                # retryable failure (same code path as unavailable).
                LLM_CALLS_TOTAL.labels(
                    provider=self.default_provider.name,
                    model=request.model,
                    outcome="output_invalid",
                ).inc()
                last_exc = e
                if attempt == max_retries:
                    break
                backoff = min(2 ** attempt, 8) + random.uniform(0, 0.5)  # noqa: S311
                await asyncio.sleep(backoff)
                attempt += 1

        # Exhausted retries — raise the last exception
        assert last_exc is not None
        raise last_exc


    async def stream_chat(
        self, request: ChatRequest
    ) -> AsyncIterator["ChatResponse | str"]:
        """Stream a chat turn. Yields text deltas, then a final ChatResponse.

        Mirrors :meth:`chat`'s usage accounting: on the final ``ChatResponse``
        a usage row is enqueued (flushed via :meth:`flush_usage`). Streaming
        cannot be resumed after a partial response, so errors surface directly
        without retry — callers that need reliability for non-streaming turns
        should keep using :meth:`chat`.
        """
        request_id = uuid.uuid4().hex
        provider = self.default_provider.name
        # Track whether we ever saw a final ChatResponse — if not, the
        # stream failed (or never produced a terminal chunk) and we
        # need to inc the failure outcome. Stage 11.3 metric.
        saw_final_response = False
        try:
            async for item in self.default_provider.stream(request):
                if isinstance(item, ChatResponse):
                    saw_final_response = True
                    self.usage.enqueue(
                        tenant_id=self.tenant_id,
                        provider=provider,
                        model=item.model,
                        prompt_tokens=item.prompt_tokens,
                        completion_tokens=item.completion_tokens,
                        request_id=request_id,
                    )
                    LLM_CALLS_TOTAL.labels(
                        provider=provider, model=item.model, outcome="success"
                    ).inc()
                    LLM_TOKENS_TOTAL.labels(
                        provider=provider, model=item.model, direction="input"
                    ).inc(item.prompt_tokens)
                    LLM_TOKENS_TOTAL.labels(
                        provider=provider, model=item.model, direction="output"
                    ).inc(item.completion_tokens)
                yield item
        except (RateLimited, InvalidRequest, ProviderUnavailable, OutputInvalid) as e:
            # Streaming has no retry: surface the error after recording
            # the outcome so the metric reflects the failure.
            outcome_map = {
                RateLimited: "rate_limited",
                InvalidRequest: "invalid_request",
                ProviderUnavailable: "unavailable",
                OutputInvalid: "output_invalid",
            }
            LLM_CALLS_TOTAL.labels(
                provider=provider,
                model=request.model,
                outcome=outcome_map[type(e)],
            ).inc()
            raise
