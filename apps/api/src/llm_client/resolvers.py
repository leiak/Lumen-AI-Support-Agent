"""Resolver implementations used by LLMGateway.

A resolver is any ``Callable[[ChatRequest], BaseProvider]``. ``LLMClient``
calls its injected resolver per request to find the provider to talk to.
This module ships two resolver kinds:

* :class:`PinnedResolver` — ignores ``request.model`` and always returns
  the bound provider. Used by :meth:`LLMGateway.with_config` so call
  sites (QA Judge, history mining) can pin a (provider, model) pair
  independent of the model's prefix. ``LLMClient`` detects this via
  ``isinstance`` and rewrites ``request.model`` with ``pinned.model``
  before forwarding, so the metric label reflects the actual model used.

* :class:`_PrefixResolver` — looks at ``request.model`` prefix and
  returns the matching registered provider. Falls back to the gateway's
  default provider when no prefix matches. Raises
  :class:`UnknownModelError` when the default provider is also missing.

``_PrefixResolver`` is private (leading underscore) — it's an internal
detail of :class:`LLMGateway`. Call sites only see the gateway's
``resolve`` / ``default_resolver`` / ``with_config`` API.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from core.business_metrics import LLM_FALLBACK_ATTEMPTS_TOTAL
from llm_client.exceptions import (
    AttemptRecord,
    FallbackChainExhausted,
    OutputInvalid,
    ProviderUnavailable,
    RateLimited,
)

if TYPE_CHECKING:
    from llm_client.providers.base import BaseProvider
    from llm_client.types import ChatRequest, ChatResponse


class UnknownModelError(Exception):
    """Raised by :class:`_PrefixResolver` when no provider prefix matches
    ``request.model`` AND no default provider is registered.

    ``LLMClient`` catches this and re-raises as ``InvalidRequest`` so
    callers see a uniform "bad request" surface (no retry, no metric
    label explosion).
    """


class PinnedResolver:
    """Resolver that always returns the bound provider, ignoring the
    request's ``model`` field.

    ``LLMClient`` detects this via ``isinstance(resolver, PinnedResolver)``
    and overwrites ``request.model`` with :attr:`model` so the
    ``LLM_CALLS_TOTAL{model=...}`` metric reflects the actual model
    used. Without the override the metric would label pinned calls
    with the request's caller-supplied model name (often wrong).
    """

    def __init__(self, *, provider: BaseProvider, model: str) -> None:
        self.provider = provider
        self.model = model

    def __call__(self, request: ChatRequest) -> BaseProvider:
        return self.provider


# Type alias used by LLMGateway / LLMClient.
Resolver = Callable[["ChatRequest"], "BaseProvider"]


# Module-private prefix table. Order matters only for readability —
# the resolver iterates the whole table to find the first match.
_PREFIX_TABLE: tuple[tuple[str, str], ...] = (
    ("minimax-", "minimax"),
    ("claude-", "anthropic"),
    ("gpt-", "openai"),
    ("o1-", "openai"),
    ("o3-", "openai"),
)


class _PrefixResolver:
    """Auto-router: dispatch by ``request.model`` prefix.

    Public surface is the ``__call__`` method (so this class satisfies
    the :data:`Resolver` protocol). Construction is the gateway's
    concern — see :class:`llm_client.gateway.LLMGateway`.
    """

    def __init__(
        self,
        *,
        providers: dict[str, BaseProvider],
        default_provider_name: str,
    ) -> None:
        self._providers = providers
        self._default_name = default_provider_name

    def __call__(self, request: ChatRequest) -> BaseProvider:
        model_lower = request.model.lower()
        for prefix, name in _PREFIX_TABLE:
            if model_lower.startswith(prefix):
                if name in self._providers:
                    return self._providers[name]
                # Prefix matches a known family but that family isn't
                # registered in this gateway (e.g. an OpenAI key is
                # unset). Fall through to default provider rather than
                # failing — this matches the spec's "no registered
                # provider for model prefix → default" semantics.
                break
        if self._default_name in self._providers:
            return self._providers[self._default_name]
        raise UnknownModelError(
            f"No provider registered for model {request.model!r} "
            f"(default provider {self._default_name!r} also unavailable)"
        )


# Exception types that trigger fallback to the next step in the chain.
# ``InvalidRequest`` is intentionally absent: 4xx-equivalent errors are
# caller mistakes, not transient. Fallback would mask the bug and waste
# tokens on the secondary provider.
_FALLBACK_TRIGGERS: tuple[type[BaseException], ...] = (
    ProviderUnavailable,
    OutputInvalid,
    RateLimited,
    asyncio.TimeoutError,
)


class FallbackResolver:
    """Chain-based fallback resolver: try each step in order, return first success.

    Each step is a :class:`PinnedResolver` carrying a ``(provider, model)``
    pair. The chain is iterated on transient failures (see
    :data:`_FALLBACK_TRIGGERS`); on the first successful step a
    :class:`ChatResponse` is returned and on exhaustion
    :class:`FallbackChainExhausted` is raised.

    The :meth:`__call__` shim returns the **primary step's provider** so
    the class satisfies ``Resolver = Callable[[ChatRequest], BaseProvider]``
    (M4.A contract unchanged). Real chain execution goes through
    :meth:`ainvoke`; ``LLMClient.chat`` detects the resolver via
    ``hasattr(resolver, "ainvoke")`` and routes there.
    """

    def __init__(
        self,
        *,
        steps: list[PinnedResolver],
        attempt_timeout_s: float | None = None,
    ) -> None:
        if not steps:
            raise ValueError("FallbackResolver needs at least one step")
        # Reject duplicate (provider.name, model) pairs to keep metric
        # labels unambiguous and prevent operator typos from silently
        # shadowing each other.
        seen: set[tuple[str, str]] = set()
        for step in steps:
            key = (step.provider.name, step.model)
            if key in seen:
                raise ValueError(
                    f"duplicate step {key!r} in fallback chain"
                )
            seen.add(key)
        self.steps = list(steps)
        self._timeout = attempt_timeout_s

    def __call__(self, request: ChatRequest) -> BaseProvider:
        """Return primary step's provider (M4.A protocol compatibility).

        This is a placeholder for callers that only need to know which
        provider to use (e.g. ``stream_chat``). Real chain execution
        goes through :meth:`ainvoke`.
        """
        return self.steps[0].provider

    async def ainvoke(self, request: ChatRequest) -> ChatResponse:
        """Execute the chain; return first successful response.

        On transient failure of any step, record the attempt and move to
        the next. ``InvalidRequest`` (and any non-trigger exception)
        propagates immediately — these are caller errors, not transient
        infrastructure problems.

        Returns:
            The :class:`ChatResponse` from the first successful step.

        Raises:
            FallbackChainExhausted: every step raised a trigger exception.
            InvalidRequest: a step raised ``InvalidRequest`` (not retried,
                surfaced immediately).
            Exception: any non-trigger, non-``InvalidRequest`` exception
                propagates (config bug / runtime crash — not a fallback
                candidate).
        """
        attempts: list[AttemptRecord] = []
        last_exc: BaseException | None = None
        for step_idx, step in enumerate(self.steps):
            rewritten = request.model_copy(update={"model": step.model})
            try:
                if self._timeout is not None:
                    resp = await asyncio.wait_for(
                        step.provider.chat(rewritten),
                        timeout=self._timeout,
                    )
                else:
                    resp = await step.provider.chat(rewritten)
                LLM_FALLBACK_ATTEMPTS_TOTAL.labels(
                    provider=step.provider.name,
                    model=step.model,
                    step=str(step_idx),
                    outcome="success",
                ).inc()
                return resp
            except _FALLBACK_TRIGGERS as exc:
                exc_name = type(exc).__name__
                # Map to metric outcome vocabulary. The Counter labels
                # are bounded strings, not arbitrary class names —
                # anything outside the known set collapses to
                # ``other`` to keep cardinality bounded.
                outcome_label = {
                    "ProviderUnavailable": "provider_unavailable",
                    "OutputInvalid": "output_invalid",
                    "RateLimited": "rate_limited",
                    "TimeoutError": "timeout",
                }.get(exc_name, "other")
                LLM_FALLBACK_ATTEMPTS_TOTAL.labels(
                    provider=step.provider.name,
                    model=step.model,
                    step=str(step_idx),
                    outcome=outcome_label,
                ).inc()
                attempts.append(
                    AttemptRecord(
                        provider_name=step.provider.name,
                        model=step.model,
                        exc_type=exc_name,
                    )
                )
                last_exc = exc
                continue
        # All steps exhausted.
        assert last_exc is not None  # invariant: N>=1 steps always set this
        raise FallbackChainExhausted(attempts=attempts) from last_exc


__all__ = ["FallbackResolver", "PinnedResolver", "Resolver", "UnknownModelError"]