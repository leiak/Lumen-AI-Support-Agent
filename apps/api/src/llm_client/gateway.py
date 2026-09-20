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

from typing import TYPE_CHECKING

from llm_client.resolvers import PinnedResolver, Resolver, _PrefixResolver

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
        self._resolver: Resolver = _PrefixResolver(
            providers=self._providers,
            default_provider_name=self._default_provider_name,
        )

    @property
    def providers(self) -> dict[str, "BaseProvider"]:
        """Read-only view of the registered providers."""
        return dict(self._providers)

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

        Called on API shutdown. Provider stubs without an ``aclose``
        method are skipped (defense for test fixtures).
        """
        for provider in self._providers.values():
            aclose = getattr(provider, "aclose", None)
            if aclose is not None:
                await aclose()


__all__ = ["LLMGateway"]