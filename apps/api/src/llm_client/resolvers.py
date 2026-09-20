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

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llm_client.providers.base import BaseProvider
    from llm_client.types import ChatRequest


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

    def __init__(self, *, provider: "BaseProvider", model: str) -> None:
        self.provider = provider
        self.model = model

    def __call__(self, request: "ChatRequest") -> "BaseProvider":
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
        providers: dict[str, "BaseProvider"],
        default_provider_name: str,
    ) -> None:
        self._providers = providers
        self._default_name = default_provider_name

    def __call__(self, request: "ChatRequest") -> "BaseProvider":
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


__all__ = ["PinnedResolver", "Resolver", "UnknownModelError"]