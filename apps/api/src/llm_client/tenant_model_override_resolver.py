"""Tenant-scoped model override wrapper (M5.2).

Wraps the gateway's default resolver and rewrites ``request.model``
to the tenant's per-provider preference when one is configured.
Works transparently for all inner resolver types (PinnedResolver,
FallbackResolver, _PrefixResolver) — the wrapper only needs to
rewrite the model AFTER the inner resolver picks the provider.

Why this exists: per the M4.A resolver seam, providers always use
``request.model`` (see ``anthropic_provider.py:113``,
``openai_provider.py:122``). The provider's ``self.model`` attribute
is dead code on the request path. So the only place to inject a
per-tenant override is between the inner resolver and the provider,
which is exactly what this wrapper does.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.business_metrics import LLM_TENANT_MODEL_OVERRIDE_TOTAL
from observability import tracer

if TYPE_CHECKING:
    from llm_client.providers.base import BaseProvider
    from llm_client.resolvers import Resolver
    from llm_client.types import ChatRequest


class TenantModelOverrideResolver:
    """Wrap an inner resolver and apply per-tenant model overrides.

    Construction takes:
        inner: the existing resolver from ``LLMGateway.default_resolver``
               (typically a ``FallbackResolver`` or ``_PrefixResolver``).
        overrides: dict mapping provider_name → model_override
                   (only entries with non-None values are honored;
                   absent provider = no override).

    The wrapper satisfies the :data:`Resolver` protocol via
    ``__call__`` AND the async ``ainvoke`` seam used by
    ``FallbackResolver`` / ``TenantResolver``. Both paths rewrite
    ``request.model`` after the inner resolver picks the provider so
    the provider prefix match still works (the caller's original
    ``request.model`` is the prefix signal; the wrapper replaces it
    with the tenant override).
    """

    def __init__(
        self,
        *,
        inner: Resolver,
        overrides: dict[str, str | None],
    ) -> None:
        self._inner = inner
        self._overrides = dict(overrides)

    def __call__(self, request: ChatRequest) -> BaseProvider:
        provider = self._inner(request)
        self._maybe_rewrite(request, provider)
        return provider

    async def ainvoke(self, request: ChatRequest) -> Any:
        """Async path used by ``LLMClient`` for the ainvoke seam.

        Two cases:
          1. Inner has its own ``ainvoke`` (e.g. ``FallbackResolver``) —
             delegate to it so the chain's retry/fallback semantics
             work. The model is rewritten BEFORE delegation so the
             inner resolver sees the override on its first step
             (per-step ``PinnedResolver`` may then rewrite again per
             its own ``step.model``).
          2. Inner is a sync resolver (e.g. ``_PrefixResolver``) —
             perform the chat ourselves so the caller (``LLMClient``)
             always receives a ``ChatResponse``, never a ``BaseProvider``.
             Without this fallback, returning the provider here would
             cause ``LLMClient`` to AttributeError on
             ``resp.prompt_tokens`` — real bug surfaced 2026-10-13
             with the demo tenant pointing at the Doubao Ark endpoint,
             after the embedding model was patched to
             ``doubao-embedding-vision`` and the chat call finally
             reached the wire.
        """
        inner = self._inner
        # Rewrite model first regardless of which path we take.
        provider = inner(request)
        self._maybe_rewrite(request, provider)
        if hasattr(inner, "ainvoke"):
            return await inner.ainvoke(request)  # type: ignore[attr-defined]
        # No async inner path — call the provider's chat directly.
        return await provider.chat(request)

    def _maybe_rewrite(
        self,
        request: ChatRequest,
        provider: BaseProvider,
    ) -> None:
        """Rewrite ``request.model`` if the tenant has an override.

        The provider's name is read from a ``name`` attribute (all
        ``BaseProvider`` subclasses set this). If no override is
        configured for that provider, the request is untouched and
        ``outcome="unset"`` fires. Missing provider name → no-op
        (defensive for mock providers in tests).

        M6.3 OpenTelemetry tracing: wraps the override logic in an
        ``llm.override`` span with ``provider_name`` + ``outcome``
        attributes (``hit`` | ``miss`` | ``unset``). The ``miss``
        outcome fires when the provider has no ``.name`` attribute
        (defensive for mock providers); ``unset`` fires when the
        provider has a name but no override entry; ``hit`` fires
        when the rewrite actually happened.
        """
        provider_name = getattr(provider, "name", None)
        if provider_name is None:
            # Wrap in the span even when we can't extract the name
            # so the trace still shows the resolve-wrapper fired
            # (with outcome="miss"). Use ``<unknown>`` for the
            # provider_name attribute when we can't resolve it.
            with tracer.start_as_current_span(
                "llm.override",
                attributes={
                    "provider_name": "<unknown>",
                    "outcome": "miss",
                },
            ):
                return
        override = self._overrides.get(provider_name)
        if override is None:
            with tracer.start_as_current_span(
                "llm.override",
                attributes={
                    "provider_name": provider_name,
                    "outcome": "unset",
                },
            ):
                LLM_TENANT_MODEL_OVERRIDE_TOTAL.labels(
                    provider=provider_name, outcome="unset",
                ).inc()
            return
        # Hit — rewrite in-place. ``request.model`` is a regular attribute
        # on the Pydantic model; ``LLMClient`` and providers do not
        # mutate the model field after they receive it.
        with tracer.start_as_current_span(
            "llm.override",
            attributes={
                "provider_name": provider_name,
                "outcome": "hit",
            },
        ):
            request.model = override  # type: ignore[misc]
            LLM_TENANT_MODEL_OVERRIDE_TOTAL.labels(
                provider=provider_name, outcome="hit",
            ).inc()


__all__ = ["TenantModelOverrideResolver"]