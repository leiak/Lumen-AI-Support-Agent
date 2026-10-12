"""Production wrapper that emits a ``llm.resolve`` span on every tenant resolve (M6.3).

This is the M6.3 production realisation of the spec's ``llm.resolve``
span. The previous design relied on a :class:`TenantResolver` class
(``tenant_resolver.py``) that was never instantiated in production — the
cache's ``_raise_or_return`` returned ``gateway.default_resolver`` directly
to ``LLMClient``, so the span never fired.

The fix wires this small wrapper into ``_raise_or_return`` UNCONDITIONALLY
(whether or not the tenant has any model-override rows). The wrapper
forwards calls to the inner resolver and opens one ``llm.resolve`` span
per ``__call__`` (sync) and per ``ainvoke`` (async) invocation.

PII discipline: ``tenant_id`` and ``provider_name`` are both on
:data:`observability.PII_WHITELIST` — no PII risk.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from observability import tracer

if TYPE_CHECKING:
    from llm_client.types import ChatRequest


class TenantResolverSpan:
    """Minimal production wrapper that emits a ``llm.resolve`` span.

    Production instantiates this on every tenant resolver build so the
    span is always present — independent of whether the tenant has any
    :class:`TenantModelOverrideResolver` wrap. The class is intentionally
    tiny: it forwards calls to the inner resolver and emits one span per
    resolve.

    The span carries:
      * ``tenant_id`` — set at span start (always available, on the
        :data:`observability.PII_WHITELIST`).
      * ``provider_name`` — set AFTER delegating to the inner resolver,
        because we don't know which provider the resolver will pick until
        it runs. On the async path the inner may return a
        :class:`ChatResponse` (not a provider) — in that case the
        attribute is left unset rather than guessing.

    Both attributes are on the whitelist — no PII risk.

    Mirrors the wrap pattern of :class:`TenantModelOverrideResolver`
    (``tenant_model_override_resolver.py``) so the wiring stays
    uniform across the per-tenant resolver chain.
    """

    def __init__(
        self,
        *,
        inner: Any,
        tenant_id: str,
    ) -> None:
        self._inner = inner
        self._tenant_id = tenant_id

    def __call__(self, request: ChatRequest) -> Any:
        """Sync ``__call__`` — emits ``llm.resolve`` span, then delegates.

        Returns the provider the inner resolver picked. Callers
        (``LLMClient._chat_with_retry``) use this for the single-provider
        retry path; the result feeds straight into ``provider.chat(request)``.
        """
        with tracer.start_as_current_span(
            "llm.resolve",
            attributes={"tenant_id": self._tenant_id},
        ) as resolve_span:
            provider = self._inner(request)
            # ``provider_name`` is the inner provider's ``.name`` attribute
            # (e.g. ``anthropic`` / ``minimax`` / ``openai``). Every
            # ``BaseProvider`` subclass sets this; fall back to ``<unknown>``
            # for mock providers that don't.
            resolve_span.set_attribute(
                "provider_name",
                getattr(provider, "name", "<unknown>"),
            )
            return provider

    async def ainvoke(self, request: ChatRequest) -> Any:
        """Async ``ainvoke`` — emits ``llm.resolve`` span, then delegates.

        If the inner resolver exposes ``ainvoke`` (typical for
        :class:`FallbackResolver` / :class:`TenantModelOverrideResolver`),
        delegate to it and the inner chain runs to completion. Otherwise
        fall back to the sync ``__call__`` so callers always get a result
        (some single-provider tenants only have a ``_PrefixResolver``,
        which lacks ``ainvoke``).

        ``provider_name`` is only set when the result looks like a
        provider (has a ``.name`` attribute). For
        :class:`FallbackResolver` the async path returns a
        :class:`ChatResponse`, not a provider — in that case the
        ``llm.resolve`` span fires without ``provider_name`` and the
        upstream ``llm.chat`` span carries the actual provider name once
        the response arrives.
        """
        with tracer.start_as_current_span(
            "llm.resolve",
            attributes={"tenant_id": self._tenant_id},
        ) as resolve_span:
            inner_ainvoke = getattr(self._inner, "ainvoke", None)
            if inner_ainvoke is not None:
                result = await inner_ainvoke(request)
            else:
                # No async seam — fall back to sync ``__call__``. The
                # caller is expected to use sync semantics (this happens
                # with single-provider tenants whose gateway uses
                # ``_PrefixResolver`` directly).
                result = self._inner(request)
            if hasattr(result, "name"):
                resolve_span.set_attribute("provider_name", result.name)
            return result


__all__ = ["TenantResolverSpan"]
