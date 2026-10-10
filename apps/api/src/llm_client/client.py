"""High-level LLM client: wraps a resolver, adds retry, records usage.

The resolver is the seam between the client and the underlying
``BaseProvider`` instances. By default the client expects an
``LLMGateway.default_resolver`` (prefix-based auto-routing), but
callers that need a pinned (provider, model) pair — QA Judge,
history mining — inject a :class:`PinnedResolver` from
``LLMGateway.with_config`` instead.

The client is unaware of how many providers the gateway holds. It
asks the resolver once per request and forwards the resulting
``provider.chat`` / ``provider.stream`` call.
"""
import asyncio
import json
import random
import uuid
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel

from core.business_metrics import LLM_CALLS_TOTAL, LLM_TOKENS_TOTAL
from llm_client.exceptions import (
    InvalidRequest,
    OutputInvalid,
    ProviderUnavailable,
    RateLimited,
)
from llm_client.providers.base import BaseProvider
from llm_client.resolvers import (
    FallbackResolver,
    PinnedResolver,
    Resolver,
    UnknownModelError,
)
from llm_client.types import ChatMessage, ChatRequest, ChatResponse
from llm_client.usage import UsageRecorder

# Route-mode label values — keep them as constants so dashboards and
# alerts reference the same string we increment with.
ROUTE_AUTO = "auto"
ROUTE_FALLBACK = "fallback"  # NEW — M4.B FallbackResolver chain
ROUTE_PINNED = "pinned"
ROUTE_UNKNOWN_MODEL = "unknown_model"
ROUTE_RESOLVER_ERROR = "resolver_error"

# Provider label value used when the resolver raised before we could
# pick a provider. Cardinality stays bounded (single literal string).
_UNKNOWN_PROVIDER_LABEL = "<unknown>"


def _route_mode_for(resolver: Resolver) -> str:
    """Pick the ``route_mode`` metric label value for a given resolver.

    Order matters: ``FallbackResolver`` check must precede the
    ``PinnedResolver`` check because a FallbackResolver's outer
    ``__call__`` returns the primary step's provider (a PinnedResolver
    in spirit) but its ``route_mode`` semantically means "a chain ran".
    """
    if isinstance(resolver, FallbackResolver):
        return ROUTE_FALLBACK
    if isinstance(resolver, PinnedResolver):
        return ROUTE_PINNED
    return ROUTE_AUTO


def _record_success(
    provider_label: str,
    model: str,
    route_mode: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> None:
    """Increment LLM_CALLS_TOTAL (success) + LLM_TOKENS_TOTAL (in + out).

    Centralised so the success-path label set stays consistent between
    :meth:`LLMClient.chat` and :meth:`LLMClient.stream_chat` — adding a
    new label later is one edit, not 5.
    """
    LLM_CALLS_TOTAL.labels(
        provider=provider_label,
        model=model,
        route_mode=route_mode,
        outcome="success",
    ).inc()
    LLM_TOKENS_TOTAL.labels(
        provider=provider_label,
        model=model,
        route_mode=route_mode,
        direction="input",
    ).inc(prompt_tokens)
    LLM_TOKENS_TOTAL.labels(
        provider=provider_label,
        model=model,
        route_mode=route_mode,
        direction="output",
    ).inc(completion_tokens)


def _record_failure(
    provider_label: str,
    model: str,
    route_mode: str,
    outcome: str,
) -> None:
    """Increment LLM_CALLS_TOTAL with a failure outcome.

    Token counters are NOT touched — failures don't consume model
    tokens (4xx/429/quota) or the token count is unknown (resolver /
    transport errors). ``provider_label`` is usually
    ``_UNKNOWN_PROVIDER_LABEL`` because the resolver raised before we
    could attribute the call to a provider.
    """
    LLM_CALLS_TOTAL.labels(
        provider=provider_label,
        model=model,
        route_mode=route_mode,
        outcome=outcome,
    ).inc()


class LLMClient:
    """Wraps a resolver with retry + usage recording.

    - Retry: 5xx (ProviderUnavailable), unparseable responses (OutputInvalid)
    - No retry: 429 (RateLimited), 4xx (InvalidRequest), unknown model,
      resolver error
    - On success: enqueue a usage row (flushed via flush_usage())
    """

    def __init__(
        self,
        *,
        provider_resolver: Resolver,
        tenant_id: str,
        usage_recorder: UsageRecorder | None = None,
    ) -> None:
        if provider_resolver is None:
            raise TypeError("provider_resolver is required")
        self.provider_resolver = provider_resolver
        self.tenant_id = tenant_id
        self.usage = usage_recorder or UsageRecorder()
        self._route_mode = _route_mode_for(provider_resolver)

    def _resolve_request(self, request: ChatRequest) -> tuple[ChatRequest, str]:
        """Resolve ``request`` to a (possibly rewritten request, route_mode).

        For PinnedResolver, ``request.model`` is overwritten with the
        pinned model so the metric label reflects the actual model used.
        For all other resolvers, the request is returned unchanged.
        """
        if isinstance(self.provider_resolver, PinnedResolver):
            return (
                request.model_copy(
                    update={"model": self.provider_resolver.model}
                ),
                ROUTE_PINNED,
            )
        return request, self._route_mode

    async def chat_with_structured_output(
        self,
        *,
        messages: list[dict[str, Any]],
        schema: type[BaseModel],
        model: str,
        timeout_s: float = 10.0,
    ) -> BaseModel:
        """Call the underlying provider and parse the response into ``schema``.

        (Stub implementation kept verbatim from M1; future M4+ tasks may
        wire provider-native structured output. The stub is good enough
        for unit tests that mock it directly with ``MagicMock`` / ``AsyncMock``.)

        Raises:
            OutputInvalid: if the response is not parseable JSON or the
                parsed dict cannot construct ``schema``.
            asyncio.TimeoutError: if the underlying ``chat`` doesn't
                return within ``timeout`` seconds.
        """
        chat_request = ChatRequest(
            model=model,
            messages=[
                ChatMessage(role=m["role"], content=m["content"])  # type: ignore[arg-type]
                for m in messages
            ],
        )

        async def _call() -> ChatResponse:
            return await self.chat(chat_request)

        response = await asyncio.wait_for(_call(), timeout=timeout_s)
        try:
            parsed = json.loads(response.content)
        except (json.JSONDecodeError, TypeError) as e:
            raise OutputInvalid(f"judge response not JSON: {response.content[:200]}") from e
        try:
            return schema(**parsed)
        except Exception as e:
            raise OutputInvalid(
                f"judge response did not match schema {schema.__name__}: {e}"
            ) from e

    async def aclose(self) -> None:
        """Flush pending usage. Provider-level close is handled by the
        owning ``LLMGateway.aclose_all`` — the client no longer owns a
        single provider reference, so it can't close one.
        """
        await self.flush_usage()

    async def flush_usage(self) -> None:
        await self.usage.flush()

    async def chat(
        self,
        request: ChatRequest,
        *,
        max_retries: int = 1,
    ) -> ChatResponse:
        """Send a chat request with retry.

        Retries on ProviderUnavailable / OutputInvalid up to `max_retries`
        times with exponential backoff + jitter. Does NOT retry on
        RateLimited / InvalidRequest / UnknownModelError / resolver error
        (those are caller's mistake or hard 4xx-equivalent failures).

        When ``provider_resolver`` exposes an ``ainvoke`` method
        (FallbackResolver from M4.B), the chain IS the retry mechanism —
        we delegate and skip the local retry loop to avoid
        ``max_retries * chain_length`` attempt explosion.  # noqa: RUF002
        """
        # M4.B: FallbackResolver.ainvoke runs the whole chain (primary +
        # backup, ...) and either returns success or raises
        # FallbackChainExhausted. We deliberately do NOT wrap it in this
        # method's retry loop — the chain already handles per-step retry
        # semantics, and re-running it on exhaustion would compound.
        if hasattr(self.provider_resolver, "ainvoke"):
            return await self.provider_resolver.ainvoke(request)

        request, route_mode = self._resolve_request(request)
        request_id = uuid.uuid4().hex
        attempt = 0
        last_exc: Exception | None = None
        provider: BaseProvider | None = None  # hoist; reset per iteration
        while attempt <= max_retries:
            provider = None  # avoid stale binding from a prior iteration
            try:
                provider = self.provider_resolver(request)
                resp = await provider.chat(request)
                self.usage.enqueue(
                    tenant_id=self.tenant_id,
                    provider=provider.name,
                    model=resp.model,
                    prompt_tokens=resp.prompt_tokens,
                    completion_tokens=resp.completion_tokens,
                    request_id=request_id,
                )
                _record_success(
                    provider.name,
                    resp.model,
                    route_mode,
                    resp.prompt_tokens,
                    resp.completion_tokens,
                )
                return resp
            except RateLimited:
                _record_failure(
                    provider.name if provider is not None else _UNKNOWN_PROVIDER_LABEL,
                    request.model,
                    route_mode,
                    "rate_limited",
                )
                raise
            except InvalidRequest:
                _record_failure(
                    provider.name if provider is not None else _UNKNOWN_PROVIDER_LABEL,
                    request.model,
                    route_mode,
                    "invalid_request",
                )
                raise
            except ProviderUnavailable as e:
                _record_failure(
                    provider.name if provider is not None else _UNKNOWN_PROVIDER_LABEL,
                    request.model,
                    route_mode,
                    "unavailable",
                )
                last_exc = e
                if attempt == max_retries:
                    break
                backoff = min(2 ** attempt, 8) + random.uniform(0, 0.5)  # noqa: S311
                await asyncio.sleep(backoff)
                attempt += 1
            except OutputInvalid as e:
                _record_failure(
                    provider.name if provider is not None else _UNKNOWN_PROVIDER_LABEL,
                    request.model,
                    route_mode,
                    "output_invalid",
                )
                last_exc = e
                if attempt == max_retries:
                    break
                backoff = min(2 ** attempt, 8) + random.uniform(0, 0.5)  # noqa: S311
                await asyncio.sleep(backoff)
                attempt += 1
            except UnknownModelError:
                # Resolver couldn't match the model to a registered
                # provider — 4xx-equivalent. Propagate as InvalidRequest
                # so callers see a uniform "bad request" surface and
                # metric label is ``invalid_request`` not a new outcome.
                # ``provider`` is ``None`` here because the resolver raised
                # before we picked, so we keep ``<unknown>`` as the label.
                _record_failure(
                    _UNKNOWN_PROVIDER_LABEL,
                    request.model,
                    ROUTE_UNKNOWN_MODEL,
                    "invalid_request",
                )
                raise InvalidRequest(  # noqa: B904
                    f"No LLM provider registered for model {request.model!r}"
                )
            except Exception as exc:
                # Resolver raised a non-UnknownModelError (config bug,
                # runtime crash). Surface as ProviderUnavailable so the
                # caller treats it as a transient infrastructure failure.
                # ``from exc`` preserves the original exception class
                # via ``__cause__`` for ops debugging (Sentry grouping,
                # distinguishing config bugs from transient failures).
                # ``provider`` is ``None`` here because the resolver raised
                # before we picked, so we keep ``<unknown>`` as the label.
                _record_failure(
                    _UNKNOWN_PROVIDER_LABEL,
                    request.model,
                    ROUTE_RESOLVER_ERROR,
                    "unavailable",
                )
                raise ProviderUnavailable("LLM provider resolver failed") from exc

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
        request, route_mode = self._resolve_request(request)
        request_id = uuid.uuid4().hex
        provider: BaseProvider | None = None
        try:
            provider = self.provider_resolver(request)
            async for item in provider.stream(request):
                if isinstance(item, ChatResponse):
                    self.usage.enqueue(
                        tenant_id=self.tenant_id,
                        provider=provider.name,
                        model=item.model,
                        prompt_tokens=item.prompt_tokens,
                        completion_tokens=item.completion_tokens,
                        request_id=request_id,
                    )
                    _record_success(
                        provider.name,
                        item.model,
                        route_mode,
                        item.prompt_tokens,
                        item.completion_tokens,
                    )
                yield item
        except (RateLimited, InvalidRequest, ProviderUnavailable, OutputInvalid) as e:
            outcome_map = {
                RateLimited: "rate_limited",
                InvalidRequest: "invalid_request",
                ProviderUnavailable: "unavailable",
                OutputInvalid: "output_invalid",
            }
            _record_failure(
                provider.name if provider is not None else _UNKNOWN_PROVIDER_LABEL,
                request.model,
                route_mode,
                outcome_map[type(e)],
            )
            raise
        except UnknownModelError:
            _record_failure(
                _UNKNOWN_PROVIDER_LABEL,
                request.model,
                ROUTE_UNKNOWN_MODEL,
                "invalid_request",
            )
            raise InvalidRequest(  # noqa: B904
                f"No LLM provider registered for model {request.model!r}"
            )
        except Exception as exc:
            _record_failure(
                _UNKNOWN_PROVIDER_LABEL,
                request.model,
                ROUTE_RESOLVER_ERROR,
                "unavailable",
            )
            raise ProviderUnavailable("LLM provider resolver failed") from exc
