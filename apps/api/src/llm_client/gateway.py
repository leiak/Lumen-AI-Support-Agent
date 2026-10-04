"""LLMGateway: registry holder + default resolver + per-call pinning.

The gateway is the seam between ``LLMClient`` and the underlying
``BaseProvider`` instances. ``LLMClient`` holds a
``provider_resolver: Callable[[ChatRequest], BaseProvider]`` and asks
it for a provider per request. The gateway supplies:

* :meth:`resolve` / :attr:`default_resolver` — prefix-based
  auto-routing (model name → registered provider). This is the
  default resolver passed to ``LLMClient``.
* :meth:`with_config` — returns a :class:`PinnedResolver` that
  ignores ``request.model`` and pins a specific (provider, model).
  Used by QA Judge, history mining, and any other call site that
  needs to override the default routing.
* :meth:`aclose_all` — closes every registered provider's HTTP
  client on shutdown.

Future M4 sub-projects (fallback chain, per-tenant BYOK, token
budget) all layer onto the resolver seam without touching
``LLMClient`` internals.
"""
from __future__ import annotations

import asyncio
from types import MappingProxyType
from typing import TYPE_CHECKING

from core.logging import get_logger
from llm_client.resolvers import (
    FallbackResolver,
    PinnedResolver,
    Resolver,
    _PrefixResolver,
)

log = get_logger(__name__)

if TYPE_CHECKING:
    from llm_client.providers.base import BaseProvider
    from llm_client.types import ChatRequest


class LLMGateway:
    """Holder for a registry of ``BaseProvider`` instances + a default
    prefix-based resolver.

    Construction takes the registry directly; callers build the
    registry from settings via
    :func:`llm_client.provider_registry.build_provider_registry` so
    test code can construct a gateway with stub providers without
    pulling in real API keys.
    """

    def __init__(
        self,
        *,
        providers: dict[str, "BaseProvider"],
        default_provider_name: str | None = None,
        default_fallback_chain: list[tuple[str, str]] | None = None,
        attempt_timeout_s: float | None = None,
    ) -> None:
        if not providers:
            raise RuntimeError("LLMGateway needs at least one provider")
        self._providers: dict[str, "BaseProvider"] = dict(providers)
        if default_provider_name is None:
            # First insertion order (Python 3.7+ dict guarantee). This
            # matches the M1 / M3 factory behavior: MiniMax is preferred
            # over Anthropic when both keys are set.
            default_provider_name = next(iter(self._providers))
        # No strict validation that ``default_provider_name`` is in the
        # registry: the underlying ``_PrefixResolver`` raises
        # :class:`UnknownModelError` when the default provider is needed
        # but unavailable. Validating strictly would make that error
        # unreachable from ``resolve()`` — which the spec requires.
        self._default_provider_name = default_provider_name
        self._attempt_timeout_s = attempt_timeout_s

        if default_fallback_chain:
            # Build the chain; skip entries whose provider is not registered
            # (operator might enable the env in a deployment with only one
            # provider configured). Log at WARNING for visibility.
            steps: list[PinnedResolver] = []
            for provider_name, model in default_fallback_chain:
                if provider_name not in self._providers:
                    log.warning(
                        "llm_client.fallback_step_skipped",
                        provider=provider_name,
                        model=model,
                        reason="provider not registered",
                    )
                    continue
                steps.append(
                    PinnedResolver(
                        provider=self._providers[provider_name],
                        model=model,
                    )
                )
            # Always replace the resolver with FallbackResolver when the
            # chain kwarg is non-empty — even if all steps were skipped
            # downstream, we still want the constructor to fail loudly so
            # operators notice the misconfiguration rather than silently
            # falling back to single-provider mode.
            self._resolver: Resolver = FallbackResolver(
                steps=steps,
                attempt_timeout_s=attempt_timeout_s,
            )
        else:
            # Existing behaviour: prefix-based auto-routing.
            self._resolver = _PrefixResolver(
                providers=self._providers,
                default_provider_name=self._default_provider_name,
            )

    def __repr__(self) -> str:
        return (
            f"<LLMGateway providers={sorted(self._providers)} "
            f"default={self._default_provider_name!r}>"
        )

    @property
    def providers(self) -> MappingProxyType[str, "BaseProvider"]:
        """Read-only view of the registered providers."""
        return MappingProxyType(self._providers)

    @property
    def default_provider_name(self) -> str:
        """Name of the provider used for non-pinned, non-prefix-matched requests."""
        return self._default_provider_name

    @property
    def default_resolver(self) -> Resolver:
        """The auto-routing resolver. Default ``provider_resolver`` value."""
        return self._resolver

    def resolve(self, request: "ChatRequest") -> "BaseProvider":
        """Resolve ``request`` to a provider via the prefix router.

        Raises :class:`llm_client.resolvers.UnknownModelError` if the
        model prefix doesn't match any registered provider and the
        default provider is also unavailable.
        """
        return self._resolver(request)

    def with_config(self, *, provider: str, model: str) -> PinnedResolver:
        """Return a :class:`PinnedResolver` bound to ``(provider, model)``.

        ``LLMClient`` detects ``PinnedResolver`` via ``isinstance`` and
        overwrites ``request.model`` with the pinned model before
        forwarding, so metric labels reflect the actual model used.

        Raises ``ValueError`` if ``provider`` is not in the registry.
        Callers should fail fast at startup rather than at first LLM
        call when the configured provider is misconfigured.
        """
        if provider not in self._providers:
            raise ValueError(
                f"Provider {provider!r} not registered (have: "
                f"{sorted(self._providers)})"
            )
        return PinnedResolver(
            provider=self._providers[provider],
            model=model,
        )

    async def aclose_all(self) -> None:
        """Close every registered provider's HTTP client.

        Called on API shutdown. Provider stubs without an ``aclose`` method
        are skipped (defense for test fixtures). A single failing provider
        does not abort the rest — each close runs independently and any
        exception is logged at WARNING with the provider name + error class.
        PII discipline: only provider name + error class name in logs.
        """
        closes = []
        for provider_name, provider in self._providers.items():
            aclose = getattr(provider, "aclose", None)
            if aclose is not None:
                closes.append((provider_name, aclose()))
        results = await asyncio.gather(
            *(c[1] for c in closes), return_exceptions=True
        )
        for (provider_name, _), result in zip(closes, results):
            if isinstance(result, BaseException):
                log.warning(
                    "llm_client.aclose_failed",
                    provider=provider_name,
                    error_type=type(result).__name__,
                )


__all__ = ["LLMGateway"]
