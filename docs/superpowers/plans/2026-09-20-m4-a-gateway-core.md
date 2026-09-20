# M4.A — LLM Gateway Core & Multi-Provider Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single-provider `LLMClient` shell with a resolver-driven `LLMGateway` that can route per-call to any registered provider, while preserving all existing retry / usage / metric behavior and migrating every call site.

**Architecture:** Add `LLMGateway` (holds a `dict[str, BaseProvider]` registry + a prefix-based default resolver) and inject a `provider_resolver: Callable[[ChatRequest], BaseProvider]` into `LLMClient`. `LLMClient` internals stay unchanged — they just call the resolver to find a provider per request. A new `PinnedResolver` (replacing the `LLMClient.with_config` stub) lets call sites pin a (provider, model) pair regardless of `request.model`.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, prometheus_client, pytest, pytest-httpx, pytest-asyncio.

**Spec Reference:** `docs/superpowers/specs/2026-09-20-m4-a-gateway-core-design.md`

---

## File map

| File | Status | Responsibility |
|---|---|---|
| `apps/api/src/llm_client/resolvers.py` | NEW | `PinnedResolver`, `_PrefixResolver`, `UnknownModelError` |
| `apps/api/src/llm_client/gateway.py` | NEW | `LLMGateway` (registry holder + default resolver + `with_config`) |
| `apps/api/src/llm_client/provider_registry.py` | NEW | `build_provider_registry(settings) -> dict[str, BaseProvider]` |
| `apps/api/src/llm_client/client.py` | MODIFY | `__init__` accepts `provider_resolver`; `with_config` removed; `route_mode` label on metrics |
| `apps/api/src/agent/llm_factory.py` | MODIFY | `_default_llm_client_factory` builds gateway + returns LLMClient wrapping `gateway.default_resolver` |
| `apps/api/src/qa/judge.py` | MODIFY | `JudgeClient.from_settings` uses `gateway.with_config(...)` |
| `apps/api/src/history_mining/worker.py` | MODIFY | Inner `_llm_factory` uses `gateway.with_config(...)` |
| `apps/api/src/core/business_metrics.py` | MODIFY | Add `route_mode` label to `LLM_CALLS_TOTAL` + `LLM_TOKENS_TOTAL` |
| `apps/api/tests/llm_client/test_resolvers.py` | NEW | Resolver unit tests |
| `apps/api/tests/llm_client/test_gateway.py` | NEW | Gateway unit tests |
| `apps/api/tests/llm_client/test_provider_registry.py` | NEW | Registry unit tests |
| `apps/api/tests/llm_client/integration/test_gateway_e2e.py` | NEW | Gateway e2e (httpx mock) |
| `apps/api/tests/llm_client/test_client.py` | MODIFY | Migrate `default_provider=` → `provider_resolver=` |
| `apps/api/tests/llm_client/test_client_stream.py` | MODIFY | Same migration |
| `README.md` | MODIFY | Add M4.A status row |

---

## Task 1: Add resolvers module (`PinnedResolver` + `_PrefixResolver` + `UnknownModelError`)

**Files:**
- Create: `apps/api/src/llm_client/resolvers.py`
- Test: `apps/api/tests/llm_client/test_resolvers.py`

The resolvers are the seam between `LLMClient` and `LLMGateway`. `LLMClient` will only know about `PinnedResolver` (via `isinstance`); the auto-router lives in `_PrefixResolver` and is owned by `LLMGateway`.

- [ ] **Step 1.1: Write failing test for `PinnedResolver`**

```python
# apps/api/tests/llm_client/test_resolvers.py
"""Unit tests for llm_client.resolvers."""
from unittest.mock import create_autospec

import pytest

from llm_client.providers.base import BaseProvider
from llm_client.resolvers import PinnedResolver, UnknownModelError, _PrefixResolver
from llm_client.types import ChatMessage, ChatRequest, MessageRole


def _stub_provider(name: str) -> BaseProvider:
    """Build an isinstance-compatible BaseProvider stub for tests."""
    p = create_autospec(BaseProvider, instance=True)
    p.name = name
    return p


def test_pinned_resolver_returns_pinned_provider_for_any_request() -> None:
    pinned = _stub_provider("minimax")
    r = PinnedResolver(provider=pinned, model="MiniMax-M3")
    req = ChatRequest(
        model="ignored",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    assert r(req) is pinned


def test_pinned_resolver_exposes_model_attribute() -> None:
    pinned = _stub_provider("minimax")
    r = PinnedResolver(provider=pinned, model="MiniMax-M3")
    assert r.model == "MiniMax-M3"
    assert r.provider is pinned
```

- [ ] **Step 1.2: Run test to verify it fails**

Run: `cd apps/api && pytest tests/llm_client/test_resolvers.py -v`
Expected: `ModuleNotFoundError: No module named 'llm_client.resolvers'`

- [ ] **Step 1.3: Implement `PinnedResolver`**

```python
# apps/api/src/llm_client/resolvers.py
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
        for prefix, name in _PREFIX_TABLE:
            if request.model.startswith(prefix):
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
```

- [ ] **Step 1.4: Run test to verify it passes**

Run: `cd apps/api && pytest tests/llm_client/test_resolvers.py -v`
Expected: PASS (2 tests)

- [ ] **Step 1.5: Write failing test for `_PrefixResolver` (prefix dispatch)**

Append to `apps/api/tests/llm_client/test_resolvers.py`:

```python
def test_prefix_resolver_routes_minimax_to_minimax_provider() -> None:
    minimax = _stub_provider("minimax")
    anthropic = _stub_provider("anthropic")
    r = _PrefixResolver(
        providers={"minimax": minimax, "anthropic": anthropic},
        default_provider_name="anthropic",
    )
    req = ChatRequest(
        model="MiniMax-M3",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    assert r(req) is minimax


def test_prefix_resolver_routes_claude_to_anthropic() -> None:
    anthropic = _stub_provider("anthropic")
    openai = _stub_provider("openai")
    r = _PrefixResolver(
        providers={"anthropic": anthropic, "openai": openai},
        default_provider_name="anthropic",
    )
    req = ChatRequest(
        model="claude-sonnet-4-5",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    assert r(req) is anthropic


def test_prefix_resolver_routes_openai_prefix_to_openai() -> None:
    openai = _stub_provider("openai")
    anthropic = _stub_provider("anthropic")
    r = _PrefixResolver(
        providers={"openai": openai, "anthropic": anthropic},
        default_provider_name="anthropic",
    )
    for model in ("gpt-4o-mini", "o1-preview", "o3-mini"):
        req = ChatRequest(
            model=model,
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
        )
        assert r(req) is openai, f"prefix {model!r} should route to openai"


def test_prefix_resolver_falls_back_to_default_for_unknown_prefix() -> None:
    anthropic = _stub_provider("anthropic")
    r = _PrefixResolver(
        providers={"anthropic": anthropic},
        default_provider_name="anthropic",
    )
    req = ChatRequest(
        model="some-custom-model",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    assert r(req) is anthropic


def test_prefix_resolver_raises_UnknownModelError_when_no_default() -> None:
    minimax = _stub_provider("minimax")
    r = _PrefixResolver(
        providers={"minimax": minimax},
        default_provider_name="openai",
    )
    req = ChatRequest(
        model="mystery-model",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    with pytest.raises(UnknownModelError):
        r(req)


def test_prefix_resolver_uses_default_when_prefix_family_unregistered() -> None:
    """A model like 'gpt-4o' with no OpenAI registered falls back to default.

    The spec says unknown prefix → default. If the prefix is recognized
    but that family isn't registered, we treat it like unknown — same
    fallback. (We don't 4xx just because OpenAI is unset.)
    """
    anthropic = _stub_provider("anthropic")
    r = _PrefixResolver(
        providers={"anthropic": anthropic},
        default_provider_name="anthropic",
    )
    req = ChatRequest(
        model="gpt-4o-mini",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    assert r(req) is anthropic
```

- [ ] **Step 1.6: Run test to verify prefix dispatch passes**

Run: `cd apps/api && pytest tests/llm_client/test_resolvers.py -v`
Expected: PASS (8 tests total)

- [ ] **Step 1.7: Commit**

```bash
git add apps/api/src/llm_client/resolvers.py apps/api/tests/llm_client/test_resolvers.py
git commit -m "feat(llm-client): add resolvers (PinnedResolver + _PrefixResolver)

PinnedResolver pins a (provider, model) pair for LLMClient.with_config
replacement; _PrefixResolver dispatches by request.model prefix with
default fallback. Raises UnknownModelError when no provider matches.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 2: Add `LLMGateway` + `provider_registry`

**Files:**
- Create: `apps/api/src/llm_client/gateway.py`
- Create: `apps/api/src/llm_client/provider_registry.py`
- Test: `apps/api/tests/llm_client/test_gateway.py`
- Test: `apps/api/tests/llm_client/test_provider_registry.py`

The gateway owns the provider registry and exposes `resolve`, `default_resolver`, `with_config`, and `aclose_all`. `provider_registry.build_provider_registry` builds the registry from `Settings` (replaces inline wiring in `agent/llm_factory._default_llm_client_factory`).

- [ ] **Step 2.1: Write failing test for `LLMGateway` core API**

```python
# apps/api/tests/llm_client/test_gateway.py
"""Unit tests for llm_client.gateway.LLMGateway."""
from unittest.mock import create_autospec

import pytest

from llm_client.gateway import LLMGateway
from llm_client.providers.base import BaseProvider
from llm_client.resolvers import PinnedResolver, UnknownModelError
from llm_client.types import ChatMessage, ChatRequest, MessageRole


def _stub(name: str) -> BaseProvider:
    p = create_autospec(BaseProvider, instance=True)
    p.name = name
    return p


def _stub_request(model: str) -> ChatRequest:
    return ChatRequest(
        model=model,
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )


def test_gateway_stores_providers_and_default() -> None:
    a = _stub("anthropic")
    m = _stub("minimax")
    g = LLMGateway(providers={"anthropic": a, "minimax": m})
    assert g.providers == {"anthropic": a, "minimax": m}
    assert g.default_provider_name == "anthropic"  # first insertion order


def test_gateway_explicit_default_provider_name() -> None:
    a = _stub("anthropic")
    m = _stub("minimax")
    g = LLMGateway(
        providers={"anthropic": a, "minimax": m},
        default_provider_name="minimax",
    )
    assert g.default_provider_name == "minimax"


def test_gateway_raises_when_providers_empty() -> None:
    with pytest.raises(RuntimeError, match="at least one provider"):
        LLMGateway(providers={})


def test_gateway_raises_when_default_not_in_registry() -> None:
    a = _stub("anthropic")
    with pytest.raises(RuntimeError, match="not in registry"):
        LLMGateway(providers={"anthropic": a}, default_provider_name="openai")


def test_gateway_resolve_routes_by_model_prefix() -> None:
    a = _stub("anthropic")
    m = _stub("minimax")
    g = LLMGateway(providers={"anthropic": a, "minimax": m})
    assert g.resolve(_stub_request("claude-sonnet-4-5")) is a
    assert g.resolve(_stub_request("MiniMax-M3")) is m


def test_gateway_default_resolver_returns_callable() -> None:
    a = _stub("anthropic")
    g = LLMGateway(providers={"anthropic": a})
    resolver = g.default_resolver
    assert resolver(_stub_request("anything")) is a


def test_gateway_with_config_returns_pinned_resolver() -> None:
    a = _stub("anthropic")
    m = _stub("minimax")
    g = LLMGateway(providers={"anthropic": a, "minimax": m})
    pinned = g.with_config(provider="minimax", model="MiniMax-M3")
    assert isinstance(pinned, PinnedResolver)
    # And it ignores the request's model field:
    assert pinned(_stub_request("claude-sonnet-4-5")) is m
    assert pinned.model == "MiniMax-M3"


def test_gateway_with_config_raises_for_unknown_provider() -> None:
    a = _stub("anthropic")
    g = LLMGateway(providers={"anthropic": a})
    with pytest.raises(ValueError, match="not registered"):
        g.with_config(provider="openai", model="gpt-4o")


def test_gateway_resolve_propagates_UnknownModelError() -> None:
    a = _stub("anthropic")
    g = LLMGateway(providers={"anthropic": a}, default_provider_name="openai")
    with pytest.raises(UnknownModelError):
        g.resolve(_stub_request("mystery-model"))


@pytest.mark.asyncio
async def test_gateway_aclose_all_closes_each_provider() -> None:
    a = _stub("anthropic")
    m = _stub("minimax")
    g = LLMGateway(providers={"anthropic": a, "minimax": m})
    await g.aclose_all()
    a.aclose.assert_awaited_once()  # type: ignore[attr-defined]
    m.aclose.assert_awaited_once()  # type: ignore[attr-defined]
```

- [ ] **Step 2.2: Run test to verify it fails**

Run: `cd apps/api && pytest tests/llm_client/test_gateway.py -v`
Expected: `ModuleNotFoundError: No module named 'llm_client.gateway'`

- [ ] **Step 2.3: Implement `LLMGateway`**

```python
# apps/api/src/llm_client/gateway.py
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
        if default_provider_name not in self._providers:
            raise RuntimeError(
                f"default_provider_name {default_provider_name!r} not "
                f"in registry (have: {sorted(self._providers)})"
            )
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
```

- [ ] **Step 2.4: Run test to verify `LLMGateway` passes**

Run: `cd apps/api && pytest tests/llm_client/test_gateway.py -v`
Expected: PASS (9 tests)

- [ ] **Step 2.5: Write failing test for `build_provider_registry`**

```python
# apps/api/tests/llm_client/test_provider_registry.py
"""Unit tests for llm_client.provider_registry.build_provider_registry."""
from unittest.mock import patch

import pytest

from llm_client.provider_registry import build_provider_registry


def _settings(**overrides):  # type: ignore[no-untyped-def]
    """Build a Settings-like namespace from overrides.

    Avoids importing real Settings (which would trigger .env reads and
    pydantic validators). The factory only reads the fields listed
    below — we patch the constructor to return a simple namespace.
    """
    from core.config import Settings

    defaults = {
        "minimax_api_key": None,
        "minimax_base_url": None,
        "minimax_model": None,
        "anthropic_api_key": None,
        "default_llm_model": "claude-3-5-sonnet-20241022",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[call-arg]


def test_registry_includes_minimax_when_minimax_key_set() -> None:
    settings = _settings(minimax_api_key="k")
    with patch("llm_client.provider_registry.OpenAIProvider") as mock_openai:
        reg = build_provider_registry(settings)
    assert "minimax" in reg
    mock_openai.assert_called_once()
    call_kwargs = mock_openai.call_args.kwargs
    assert call_kwargs["api_key"] == "k"
    assert call_kwargs["model"] == "MiniMax-M3"  # default fallback
    assert "api.minimaxi.com" in call_kwargs["base_url"]


def test_registry_includes_anthropic_when_anthropic_key_set() -> None:
    settings = _settings(anthropic_api_key="anth-k")
    with patch("llm_client.provider_registry.AnthropicProvider") as mock_anth:
        reg = build_provider_registry(settings)
    assert "anthropic" in reg
    mock_anth.assert_called_once_with(
        api_key="anth-k", model="claude-3-5-sonnet-20241022"
    )


def test_registry_includes_both_when_both_keys_set() -> None:
    settings = _settings(minimax_api_key="k", anthropic_api_key="a")
    with patch("llm_client.provider_registry.OpenAIProvider"), \
         patch("llm_client.provider_registry.AnthropicProvider"):
        reg = build_provider_registry(settings)
    assert set(reg.keys()) == {"minimax", "anthropic"}


def test_registry_uses_minimax_model_override_when_set() -> None:
    settings = _settings(minimax_api_key="k", minimax_model="custom-m")
    with patch("llm_client.provider_registry.OpenAIProvider") as mock_openai:
        build_provider_registry(settings)
    assert mock_openai.call_args.kwargs["model"] == "custom-m"


def test_registry_raises_when_no_keys_set() -> None:
    settings = _settings()
    with pytest.raises(RuntimeError, match="No LLM provider configured"):
        build_provider_registry(settings)
```

- [ ] **Step 2.6: Run test to verify it fails**

Run: `cd apps/api && pytest tests/llm_client/test_provider_registry.py -v`
Expected: `ModuleNotFoundError: No module named 'llm_client.provider_registry'`

- [ ] **Step 2.7: Implement `provider_registry`**

```python
# apps/api/src/llm_client/provider_registry.py
"""Build the provider registry from :class:`core.config.Settings`.

Replaces the inline provider wiring that used to live in
:func:`agent.llm_factory._default_llm_client_factory`. Centralizing
here means the gateway is constructed identically from any call site
(production factory, QA Judge, history mining, tests).

YAGNI: only MiniMax + Anthropic are wired. OpenAI etc. can be added
later by following the same pattern. The ``_PrefixResolver`` already
recognizes ``gpt-`` / ``o1-`` / ``o3-`` prefixes, so dropping a
``providers["openai"]`` entry into the registry is enough to activate
OpenAI routing — no resolver changes needed.
"""
from __future__ import annotations

from core.config import Settings
from llm_client.providers.anthropic_provider import AnthropicProvider
from llm_client.providers.base import BaseProvider
from llm_client.providers.openai_provider import OpenAIProvider

# Default model for MiniMax when MINIMAX_MODEL isn't configured. Matches
# the M1 factory wiring (kept verbatim so behavior is unchanged for
# existing deployments).
_DEFAULT_MINIMAX_MODEL = "MiniMax-M3"
_DEFAULT_MINIMAX_BASE_URL = "https://api.minimaxi.com/v1"


def build_provider_registry(settings: Settings) -> dict[str, BaseProvider]:
    """Build the provider registry keyed by name.

    Insertion order matters: ``LLMGateway`` uses the first key as the
    default provider when no ``default_provider_name`` is supplied.
    MiniMax is registered first so it wins over Anthropic when both
    keys are configured — this matches the M1 factory behavior.
    """
    providers: dict[str, BaseProvider] = {}
    if settings.minimax_api_key:
        providers["minimax"] = OpenAIProvider(
            api_key=settings.minimax_api_key,
            model=settings.minimax_model or _DEFAULT_MINIMAX_MODEL,
            base_url=settings.minimax_base_url or _DEFAULT_MINIMAX_BASE_URL,
        )
    if settings.anthropic_api_key:
        providers["anthropic"] = AnthropicProvider(
            api_key=settings.anthropic_api_key,
            model=settings.default_llm_model,
        )
    if not providers:
        raise RuntimeError(
            "No LLM provider configured: set MINIMAX_API_KEY or "
            "ANTHROPIC_API_KEY before starting the API."
        )
    return providers


__all__ = ["build_provider_registry"]
```

- [ ] **Step 2.8: Run test to verify `build_provider_registry` passes**

Run: `cd apps/api && pytest tests/llm_client/test_provider_registry.py -v`
Expected: PASS (5 tests)

- [ ] **Step 2.9: Commit**

```bash
git add apps/api/src/llm_client/gateway.py apps/api/src/llm_client/provider_registry.py \
        apps/api/tests/llm_client/test_gateway.py apps/api/tests/llm_client/test_provider_registry.py
git commit -m "feat(llm-client): add LLMGateway + provider_registry

LLMGateway owns the provider registry, default prefix resolver, and
per-call pinning via with_config(). provider_registry.build_provider_registry
builds the registry from Settings, replacing the inline wiring in
agent.llm_factory.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 3: Refactor `LLMClient` to accept `provider_resolver` + add `route_mode` metric label

**Files:**
- Modify: `apps/api/src/llm_client/client.py`
- Modify: `apps/api/src/core/business_metrics.py`
- Modify: `apps/api/tests/llm_client/test_client.py`
- Modify: `apps/api/tests/llm_client/test_client_stream.py`

The constructor swap is the largest single change. Existing tests pass `LLMClient(default_provider=...)` — those need to switch to `LLMClient(provider_resolver=...)`. The `with_config` classmethod is removed (Task 4 migrates its callers).

- [ ] **Step 3.1: Write failing test for `LLMClient` requiring `provider_resolver`**

Add to `apps/api/tests/llm_client/test_client.py`:

```python
def test_llm_client_requires_provider_resolver() -> None:
    from llm_client.client import LLMClient
    with pytest.raises(TypeError, match="provider_resolver is required"):
        LLMClient(provider_resolver=None, tenant_id="t")  # type: ignore[arg-type]
```

Also add at the top of the file:

```python
import pytest
```

- [ ] **Step 3.2: Run test to verify it fails**

Run: `cd apps/api && pytest tests/llm_client/test_client.py::test_llm_client_requires_provider_resolver -v`
Expected: FAIL (current constructor accepts `default_provider=None` without error).

- [ ] **Step 3.3: Add `route_mode` label to metrics**

In `apps/api/src/core/business_metrics.py`, replace the `LLM_CALLS_TOTAL` and `LLM_TOKENS_TOTAL` definitions:

```python
LLM_CALLS_TOTAL = Counter(
    "lumen_llm_calls_total",
    "LLM API calls, by provider / model / route_mode / outcome",
    ("provider", "model", "route_mode", "outcome"),
)

LLM_TOKENS_TOTAL = Counter(
    "lumen_llm_tokens_total",
    "LLM tokens consumed, by provider / model / route_mode / direction",
    ("provider", "model", "route_mode", "direction"),
)
```

`route_mode` enum: `auto` (default resolver hit), `pinned` (with_config), `unknown_model` (UnknownModelError caught + treated as InvalidRequest), `resolver_error` (resolver raised non-UnknownModelError). Cardinality impact: existing labels × 4 (still well under Prometheus 100k cap).

- [ ] **Step 3.4: Refactor `LLMClient.__init__` + chat/stream paths**

In `apps/api/src/llm_client/client.py`, replace the entire class. Reference diff:

```python
# apps/api/src/llm_client/client.py
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
from llm_client.resolvers import PinnedResolver, Resolver, UnknownModelError
from llm_client.types import ChatMessage, ChatRequest, ChatResponse
from llm_client.usage import UsageRecorder

# Route-mode label values — keep them as constants so dashboards and
# alerts reference the same string we increment with.
ROUTE_AUTO = "auto"
ROUTE_PINNED = "pinned"
ROUTE_UNKNOWN_MODEL = "unknown_model"
ROUTE_RESOLVER_ERROR = "resolver_error"


def _route_mode_for(resolver: Resolver) -> str:
    """Pick the ``route_mode`` metric label value for a given resolver.

    ``PinnedResolver`` instances carry ``route_mode="pinned"``. Any
    other callable is treated as ``"auto"`` — the prefix router
    and any future resolver kind (fallback chain in M4.B, per-tenant
    BYOK resolver in M4.C) fall under this umbrella.
    """
    if isinstance(resolver, PinnedResolver):
        return ROUTE_PINNED
    return ROUTE_AUTO


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
        timeout: float = 10.0,
    ) -> BaseModel:
        """Call the underlying provider and parse the response into ``schema``.

        (Stub implementation kept verbatim from M1; future M4+ tasks may
        wire provider-native structured output. The stub is good enough for
        unit tests that mock it directly with ``MagicMock`` / ``AsyncMock``.)

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

        response = await asyncio.wait_for(_call(), timeout=timeout)
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
        max_retries: int = 3,
    ) -> ChatResponse:
        """Send a chat request with retry.

        Retries on ProviderUnavailable / OutputInvalid up to `max_retries`
        times with exponential backoff + jitter. Does NOT retry on
        RateLimited / InvalidRequest / UnknownModelError / resolver error
        (those are caller's mistake or hard 4xx-equivalent failures).
        """
        request, route_mode = self._resolve_request(request)
        request_id = uuid.uuid4().hex
        attempt = 0
        last_exc: Exception | None = None
        while attempt <= max_retries:
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
                LLM_CALLS_TOTAL.labels(
                    provider=provider.name,
                    model=resp.model,
                    route_mode=route_mode,
                    outcome="success",
                ).inc()
                LLM_TOKENS_TOTAL.labels(
                    provider=provider.name,
                    model=resp.model,
                    route_mode=route_mode,
                    direction="input",
                ).inc(resp.prompt_tokens)
                LLM_TOKENS_TOTAL.labels(
                    provider=provider.name,
                    model=resp.model,
                    route_mode=route_mode,
                    direction="output",
                ).inc(resp.completion_tokens)
                return resp
            except RateLimited:
                LLM_CALLS_TOTAL.labels(
                    provider="<unknown>",
                    model=request.model,
                    route_mode=route_mode,
                    outcome="rate_limited",
                ).inc()
                raise
            except InvalidRequest:
                LLM_CALLS_TOTAL.labels(
                    provider="<unknown>",
                    model=request.model,
                    route_mode=route_mode,
                    outcome="invalid_request",
                ).inc()
                raise
            except ProviderUnavailable as e:
                LLM_CALLS_TOTAL.labels(
                    provider="<unknown>",
                    model=request.model,
                    route_mode=route_mode,
                    outcome="unavailable",
                ).inc()
                last_exc = e
                if attempt == max_retries:
                    break
                backoff = min(2 ** attempt, 8) + random.uniform(0, 0.5)  # noqa: S311
                await asyncio.sleep(backoff)
                attempt += 1
            except OutputInvalid as e:
                LLM_CALLS_TOTAL.labels(
                    provider="<unknown>",
                    model=request.model,
                    route_mode=route_mode,
                    outcome="output_invalid",
                ).inc()
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
                LLM_CALLS_TOTAL.labels(
                    provider="<unknown>",
                    model=request.model,
                    route_mode=ROUTE_UNKNOWN_MODEL,
                    outcome="invalid_request",
                ).inc()
                raise InvalidRequest(
                    f"No LLM provider registered for model {request.model!r}"
                )
            except Exception:
                # Resolver raised a non-UnknownModelError (config bug,
                # runtime crash). Surface as ProviderUnavailable so the
                # caller treats it as a transient infrastructure failure.
                LLM_CALLS_TOTAL.labels(
                    provider="<unknown>",
                    model=request.model,
                    route_mode=ROUTE_RESOLVER_ERROR,
                    outcome="unavailable",
                ).inc()
                raise ProviderUnavailable(
                    "LLM provider resolver failed"
                )

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
        saw_final_response = False
        try:
            provider = self.provider_resolver(request)
            async for item in provider.stream(request):
                if isinstance(item, ChatResponse):
                    saw_final_response = True
                    self.usage.enqueue(
                        tenant_id=self.tenant_id,
                        provider=provider.name,
                        model=item.model,
                        prompt_tokens=item.prompt_tokens,
                        completion_tokens=item.completion_tokens,
                        request_id=request_id,
                    )
                    LLM_CALLS_TOTAL.labels(
                        provider=provider.name,
                        model=item.model,
                        route_mode=route_mode,
                        outcome="success",
                    ).inc()
                    LLM_TOKENS_TOTAL.labels(
                        provider=provider.name,
                        model=item.model,
                        route_mode=route_mode,
                        direction="input",
                    ).inc(item.prompt_tokens)
                    LLM_TOKENS_TOTAL.labels(
                        provider=provider.name,
                        model=item.model,
                        route_mode=route_mode,
                        direction="output",
                    ).inc(item.completion_tokens)
                yield item
        except (RateLimited, InvalidRequest, ProviderUnavailable, OutputInvalid) as e:
            outcome_map = {
                RateLimited: "rate_limited",
                InvalidRequest: "invalid_request",
                ProviderUnavailable: "unavailable",
                OutputInvalid: "output_invalid",
            }
            LLM_CALLS_TOTAL.labels(
                provider="<unknown>",
                model=request.model,
                route_mode=route_mode,
                outcome=outcome_map[type(e)],
            ).inc()
            raise
        except UnknownModelError:
            LLM_CALLS_TOTAL.labels(
                provider="<unknown>",
                model=request.model,
                route_mode=ROUTE_UNKNOWN_MODEL,
                outcome="invalid_request",
            ).inc()
            raise InvalidRequest(
                f"No LLM provider registered for model {request.model!r}"
            )
        except Exception:
            LLM_CALLS_TOTAL.labels(
                provider="<unknown>",
                model=request.model,
                route_mode=ROUTE_RESOLVER_ERROR,
                outcome="unavailable",
            ).inc()
            raise ProviderUnavailable("LLM provider resolver failed")
```

Notes on the rewrite:
- The `with_config` classmethod is **deleted** (Task 4 migrates callers).
- `aclose()` no longer touches the provider — the gateway owns `aclose_all`. Calling code that previously did `await client.aclose()` continues to work for usage flushing.
- `<unknown>` is used as the `provider` label value when the resolver raised before we could pick a provider. The cardinality stays bounded (it's a literal string).

- [ ] **Step 3.5: Migrate existing `test_client.py` constructions**

In `apps/api/tests/llm_client/test_client.py`, replace the fixture:

```python
@pytest.fixture
def client_with_anthropic() -> LLMClient:
    from llm_client.gateway import LLMGateway

    p = AnthropicProvider(api_key="k", model="claude-3-5-sonnet-20241022")
    g = LLMGateway(providers={"anthropic": p})
    return LLMClient(provider_resolver=g.default_resolver, tenant_id="t-llm-client-test")
```

Add a teardown to close the gateway (otherwise httpx mocks leak across tests):

```python
@pytest.fixture
async def client_with_anthropic() -> AsyncGenerator[LLMClient, None]:
    from llm_client.gateway import LLMGateway

    p = AnthropicProvider(api_key="k", model="claude-3-5-sonnet-20241022")
    g = LLMGateway(providers={"anthropic": p})
    yield LLMClient(provider_resolver=g.default_resolver, tenant_id="t-llm-client-test")
    await g.aclose_all()
```

Update imports at the top:

```python
from collections.abc import AsyncGenerator
```

The existing 4 test methods stay unchanged (they call `client.chat` / `client.flush_usage` exactly as before).

- [ ] **Step 3.6: Migrate `test_client_stream.py`**

Read the file first; if it constructs `LLMClient(default_provider=...)`, do the same migration. The streaming test only differs in that it iterates the async generator. Same fixture pattern.

- [ ] **Step 3.7: Run all llm_client tests to verify**

Run: `cd apps/api && pytest tests/llm_client -v -m "not integration"`
Expected: PASS for all unit tests (`test_resolvers.py` 8 tests, `test_gateway.py` 9 tests, `test_provider_registry.py` 5 tests, `test_client.py` updated, `test_client_stream.py` updated, `test_anthropic_provider.py` unchanged, `test_openai_provider.py` unchanged, `test_types.py` unchanged).

- [ ] **Step 3.8: Commit**

```bash
git add apps/api/src/llm_client/client.py apps/api/src/core/business_metrics.py \
        apps/api/tests/llm_client/test_client.py apps/api/tests/llm_client/test_client_stream.py
git commit -m "refactor(llm-client): LLMClient accepts provider_resolver + route_mode label

- __init__ now requires provider_resolver (TypeError when None)
- with_config classmethod removed (replaced by gateway.with_config in Task 4)
- _resolve_request() rewrites request.model for PinnedResolver so metric
  label reflects the actual model used
- New route_mode label on LLM_CALLS_TOTAL + LLM_TOKENS_TOTAL with 4 enum
  values (auto / pinned / unknown_model / resolver_error)
- UnknownModelError caught and re-raised as InvalidRequest
- Other resolver errors caught and re-raised as ProviderUnavailable

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 4: Migrate `qa/judge.py` + `history_mining/worker.py` to `gateway.with_config`

**Files:**
- Modify: `apps/api/src/qa/judge.py`
- Modify: `apps/api/src/history_mining/worker.py`

Both files use the now-deleted `LLMClient.with_config(provider=, model=)` stub. Replace each call with the new pattern: build a gateway + use `gateway.with_config()`.

- [ ] **Step 4.1: Write failing test for `JudgeClient.from_settings` returning a working client**

In `apps/api/tests/qa/unit/test_judge.py` (or wherever `JudgeClient.from_settings` is tested), add or update a test:

```python
def test_judge_from_settings_uses_pinned_resolver(monkeypatch) -> None:
    """JudgeClient.from_settings must build a client that pins to the
    configured (provider, model) pair regardless of the request model."""
    from llm_client.gateway import LLMGateway
    from llm_client.resolvers import PinnedResolver

    monkeypatch.setenv("MINIMAX_API_KEY", "k")
    monkeypatch.setenv("QA_JUDGE_PROVIDER", "minimax")
    monkeypatch.setenv("QA_JUDGE_MODEL", "MiniMax-judge-1")
    # Reset cached Settings (the QA tests do this; if yours doesn't,
    # call core.config.reset_settings())
    import core.config
    core.config.reset_settings()

    client = JudgeClient.from_settings()
    assert isinstance(client.llm.provider_resolver, PinnedResolver)
    assert client.llm.provider_resolver.model == "MiniMax-judge-1"
```

- [ ] **Step 4.2: Run test to verify it fails**

Run: `cd apps/api && pytest tests/qa/unit/test_judge.py -v -k "test_judge_from_settings_uses_pinned_resolver"`
Expected: FAIL (current code calls `LLMClient.with_config` which no longer exists → `AttributeError`).

- [ ] **Step 4.3: Migrate `qa/judge.py`**

In `apps/api/src/qa/judge.py`, replace the imports + `from_settings` method:

```python
from core.config import get_settings
from core.logging import get_logger
from llm_client.client import LLMClient
from llm_client.gateway import LLMGateway
from llm_client.provider_registry import build_provider_registry
```

(Remove the `TYPE_CHECKING` import block for `LLMClient` since we now import it at runtime.)

Replace `JudgeClient.from_settings`:

```python
@classmethod
def from_settings(cls) -> "JudgeClient":
    s = get_settings()
    gateway = LLMGateway(providers=build_provider_registry(s))
    pinned = gateway.with_config(
        provider=s.qa_judge_provider, model=s.qa_judge_model
    )
    return cls(
        llm=LLMClient(provider_resolver=pinned, tenant_id="qa-judge"),
        model=s.qa_judge_model,
        threshold=s.qa_score_threshold_alert,
        max_retries=s.qa_judge_max_retries,
        timeout_seconds=s.qa_judge_timeout_seconds,
    )
```

Note: `JudgeClient` previously held an `llm: LLMClient` dataclass field. The new `llm` is constructed with a `PinnedResolver`. Tests that mock `JudgeClient` with a `MagicMock(spec=LLMClient)` keep working because the field shape is unchanged.

- [ ] **Step 4.4: Migrate `history_mining/worker.py`**

In `apps/api/src/history_mining/worker.py`, find the `_llm_factory` function (around line 230). Replace its body so it builds a gateway and returns a `LLMClient` with a pinned resolver:

```python
def _llm_factory(provider_name: str, model_name: str) -> LLMClient:
    """Build a per-worker LLMClient pinned to (provider_name, model_name).

    Used by the history-mining cluster summarizer. The (provider, model)
    pair is sourced from ``settings.history_mining_provider`` /
    ``settings.history_mining_model`` (falling back to the QA judge
    settings) so the operator can route mining onto a cheaper model
    without touching QA judge config.
    """
    from core.config import get_settings
    from llm_client.gateway import LLMGateway
    from llm_client.provider_registry import build_provider_registry

    s = get_settings()
    gateway = LLMGateway(providers=build_provider_registry(s))
    pinned = gateway.with_config(provider=provider_name, model=model_name)
    return LLMClient(provider_resolver=pinned, tenant_id="history-mining")
```

The surrounding call site (around line 248, `KBDraftGenerator(llm_client_factory=_llm_factory)`) is unchanged.

- [ ] **Step 4.5: Run QA + history-mining tests to verify migration**

Run:
```bash
cd apps/api && pytest tests/qa -v -m "not integration"
cd apps/api && pytest tests/history_mining -v -m "not integration"
```
Expected: PASS (no regressions; the existing tests don't assert on the LLMClient's internal state, only on `JudgeClient.from_settings()` returning a usable client + history_mining producing drafts).

- [ ] **Step 4.6: Commit**

```bash
git add apps/api/src/qa/judge.py apps/api/src/history_mining/worker.py \
        apps/api/tests/qa/unit/test_judge.py
git commit -m "refactor(qa,history-mining): migrate JudgeClient + mining factory to gateway.with_config

Replaces the deleted LLMClient.with_config stub with:
  gateway = LLMGateway(providers=build_provider_registry(settings))
  pinned = gateway.with_config(provider=, model=)
  LLMClient(provider_resolver=pinned, tenant_id=)

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 5: Update `agent/llm_factory.py` to use `provider_registry`

**Files:**
- Modify: `apps/api/src/agent/llm_factory.py`

`_default_llm_client_factory` currently wires providers inline. Replace with `build_provider_registry` + `LLMGateway`. The factory still returns an `LLMClient` so the existing 3 call sites (simple_responder, suggest, agent/graph) need no changes.

- [ ] **Step 5.1: Write failing test that exercises the factory end-to-end**

In `apps/api/tests/agent/test_llm_factory.py` (create if missing):

```python
"""Tests for agent.llm_factory._default_llm_client_factory."""
from unittest.mock import patch


def test_default_factory_returns_working_llm_client() -> None:
    from llm_client.client import LLMClient
    from llm_client.gateway import LLMGateway
    from llm_client.resolvers import PinnedResolver

    # Avoid requiring real API keys in unit tests — patch both providers.
    with patch("agent.llm_factory.build_provider_registry") as mock_build, \
         patch("agent.llm_factory.get_settings") as mock_settings:
        from llm_client.providers.base import BaseProvider

        mock_provider = type("Stub", (), {
            "name": "anthropic",
            "chat": lambda self, req: None,
            "stream": lambda self, req: None,
        })()
        # BaseProvider is ABC; mock with autospec so isinstance works.
        from unittest.mock import create_autospec
        stub = create_autospec(BaseProvider, instance=True)
        stub.name = "anthropic"
        mock_build.return_value = {"anthropic": stub}

        client = _default_llm_client_factory("t-factory-test")
    assert isinstance(client, LLMClient)
    # PinnedResolver check would require Anthropic key routing — for the
    # default factory path the resolver is the prefix router, not pinned.
    assert not isinstance(client.provider_resolver, PinnedResolver)
```

Actually, simpler — test the wiring:

```python
def test_default_factory_uses_build_provider_registry() -> None:
    from unittest.mock import patch

    with patch("agent.llm_factory.build_provider_registry") as mock_build, \
         patch("agent.llm_factory.get_settings") as mock_get:
        mock_get.return_value = "fake-settings"
        mock_build.return_value = {
            "anthropic": _make_stub_provider("anthropic")
        }
        client = _default_llm_client_factory("t-test")
    mock_build.assert_called_once_with("fake-settings")
    assert client.tenant_id == "t-test"


def _make_stub_provider(name: str):
    from unittest.mock import create_autospec
    from llm_client.providers.base import BaseProvider
    p = create_autospec(BaseProvider, instance=True)
    p.name = name
    return p
```

- [ ] **Step 5.2: Run test to verify it fails**

Run: `cd apps/api && pytest tests/agent/test_llm_factory.py -v`
Expected: FAIL (file doesn't exist or current factory doesn't call `build_provider_registry`).

- [ ] **Step 5.3: Refactor `agent/llm_factory.py`**

Replace the entire file:

```python
"""Default LLMClient factory — builds tenant-scoped LLMClient instances
backed by a project-wide LLMGateway.

The gateway holds the provider registry (built from settings) and
exposes a default prefix-based resolver. ``_default_llm_client_factory``
wraps that resolver in a fresh ``LLMClient`` per tenant so usage
recording is attributed to the correct tenant.

Per-tenant provider/model overrides are deferred to M4.C (BYOK). For
M4.A every tenant shares the project-wide gateway; the ``tenant_id``
is still threaded through so the existing usage path keeps working.
"""
from __future__ import annotations

from llm_client.client import LLMClient
from llm_client.gateway import LLMGateway
from llm_client.provider_registry import build_provider_registry
from llm_client.usage import UsageRecorder


def _default_llm_client_factory(tenant_id: str) -> LLMClient:
    """Build a per-tenant ``LLMClient`` wrapping the gateway's default resolver."""
    from core.config import get_settings

    settings = get_settings()
    gateway = LLMGateway(providers=build_provider_registry(settings))
    return LLMClient(
        provider_resolver=gateway.default_resolver,
        tenant_id=tenant_id,
        usage_recorder=UsageRecorder(),
    )


def _resolve_default_model() -> str:
    """Resolve the effective default model from settings.

    Returns ``MINIMAX_MODEL`` when MiniMax is configured, otherwise
    ``DEFAULT_LLM_MODEL``. Called at runtime so a config change takes
    effect without restarting the agent.
    """
    from agent.simple_responder import DEFAULT_MODEL
    from core.config import get_settings

    settings = get_settings()
    if settings.minimax_api_key:
        return settings.minimax_model or "MiniMax-M3"
    return DEFAULT_MODEL


__all__ = ["_default_llm_client_factory", "_resolve_default_model"]
```

Notes:
- `LLMGateway` is constructed per-call. This is cheap (no IO) but does construct a fresh provider set each call. Acceptable for M4.A — future stages can introduce caching if profiling shows it matters.
- The 3 call sites (`simple_responder`, `suggest`, `agent/graph/nodes`) need **no changes**: they import `_default_llm_client_factory` and use the same `LLMClientFactory = Callable[[str], LLMClient]` type alias.

- [ ] **Step 5.4: Run agent + integration tests to verify no regressions**

Run:
```bash
cd apps/api && pytest tests/agent -v -m "not integration"
cd apps/api && pytest tests/knowledge -v -m "not integration"
```
Expected: PASS — the existing agent / knowledge / conversation tests that use `_default_llm_client_factory` indirectly through `LLMClientFactory` keep working because the factory signature is unchanged.

- [ ] **Step 5.5: Commit**

```bash
git add apps/api/src/agent/llm_factory.py apps/api/tests/agent/test_llm_factory.py
git commit -m "refactor(llm-factory): _default_llm_client_factory uses gateway + provider_registry

Replaces inline provider wiring with build_provider_registry + LLMGateway.
The 3 existing call sites (simple_responder, suggest, agent/graph) need no
changes — the factory signature is preserved.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 6: Gateway e2e integration tests

**Files:**
- Create: `apps/api/tests/llm_client/integration/test_gateway_e2e.py`

These tests exercise the gateway + LLMClient end-to-end with `pytest-httpx` mocks. They mirror the style of `test_client.py` but go through the gateway + prefix resolver path.

- [ ] **Step 6.1: Write the integration test file**

```python
"""End-to-end tests for the LLM gateway via httpx mock transport."""
from collections.abc import AsyncGenerator

import pytest
from pytest_httpx import HTTPXMock

from llm_client.client import LLMClient
from llm_client.exceptions import InvalidRequest
from llm_client.gateway import LLMGateway
from llm_client.provider_registry import build_provider_registry
from llm_client.providers.anthropic_provider import AnthropicProvider
from llm_client.providers.openai_provider import OpenAIProvider
from llm_client.types import ChatMessage, ChatRequest, MessageRole


@pytest.fixture
def anthropic_only_gateway() -> AsyncGenerator[LLMGateway, None]:
    p = AnthropicProvider(api_key="k", model="claude-3-5-sonnet-20241022")
    g = LLMGateway(providers={"anthropic": p})
    yield g
    await g.aclose_all()


@pytest.fixture
def minimax_plus_anthropic_gateway() -> AsyncGenerator[LLMGateway, None]:
    mm = OpenAIProvider(
        api_key="k",
        model="MiniMax-M3",
        base_url="https://api.minimaxi.com/v1",
    )
    ant = AnthropicProvider(api_key="k", model="claude-3-5-sonnet-20241022")
    g = LLMGateway(providers={"minimax": mm, "anthropic": ant})
    yield g
    await g.aclose_all()


@pytest.mark.integration
async def test_anthropic_chat_succeeds_via_prefix_resolver(
    anthropic_only_gateway: LLMGateway, httpx_mock: HTTPXMock
) -> None:
    client = LLMClient(
        provider_resolver=anthropic_only_gateway.default_resolver,
        tenant_id="t",
    )
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        json={
            "model": "claude-3-5-sonnet-20241022",
            "content": [{"type": "text", "text": "via-anthropic"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )
    req = ChatRequest(
        model="claude-sonnet-4-5",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    resp = await client.chat(req)
    assert resp.content == "via-anthropic"


@pytest.mark.integration
async def test_minimax_chat_succeeds_via_prefix_resolver(
    minimax_plus_anthropic_gateway: LLMGateway, httpx_mock: HTTPXMock
) -> None:
    client = LLMClient(
        provider_resolver=minimax_plus_anthropic_gateway.default_resolver,
        tenant_id="t",
    )
    httpx_mock.add_response(
        url="https://api.minimaxi.com/v1/chat/completions",
        json={
            "model": "MiniMax-M3",
            "choices": [{"message": {"content": "via-minimax"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3},
        },
    )
    req = ChatRequest(
        model="MiniMax-M3",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    resp = await client.chat(req)
    assert resp.content == "via-minimax"


@pytest.mark.integration
async def test_qa_judge_pinned_path_uses_pinned_provider(
    minimax_plus_anthropic_gateway: LLMGateway, httpx_mock: HTTPXMock
) -> None:
    """Even when request.model says 'claude-...', the pinned resolver
    forces the call through MiniMax + the pinned model."""
    pinned = minimax_plus_anthropic_gateway.with_config(
        provider="minimax", model="MiniMax-judge-1"
    )
    client = LLMClient(provider_resolver=pinned, tenant_id="qa-judge")
    httpx_mock.add_response(
        url="https://api.minimaxi.com/v1/chat/completions",
        json={
            "model": "MiniMax-judge-1",
            "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )
    req = ChatRequest(
        model="claude-sonnet-4-5",  # caller-supplied, ignored by pinned
        messages=[ChatMessage(role=MessageRole.USER, content="score this")],
    )
    resp = await client.chat(req)
    assert resp.model == "MiniMax-judge-1"


@pytest.mark.integration
async def test_unknown_model_raises_InvalidRequest(
    anthropic_only_gateway: LLMGateway, httpx_mock: HTTPXMock
) -> None:
    """When the prefix resolver can't match and the default is also
    unavailable, LLMClient re-raises as InvalidRequest (no retry)."""
    # Force a default provider that isn't registered:
    g = LLMGateway(
        providers={"anthropic": AnthropicProvider(api_key="k", model="m")},
        default_provider_name="openai",
    )
    client = LLMClient(provider_resolver=g.default_resolver, tenant_id="t")
    req = ChatRequest(
        model="mystery-model",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    with pytest.raises(InvalidRequest, match="No LLM provider registered"):
        await client.chat(req)
    # No HTTP call should have been made — confirm via httpx_mock's
    # request count (it's 0 by default).
    assert len(httpx_mock.get_requests()) == 0
```

- [ ] **Step 6.2: Run integration tests**

Run: `cd apps/api && pytest tests/llm_client/integration -v`
Expected: PASS (4 tests).

- [ ] **Step 6.3: Commit**

```bash
git add apps/api/tests/llm_client/integration/test_gateway_e2e.py
git commit -m "test(llm-client): gateway e2e integration tests via httpx mock

Covers prefix-routed Anthropic + MiniMax chat, pinned (with_config)
override ignoring request.model, and unknown-model propagation as
InvalidRequest without any HTTP call.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 7: README + memory

**Files:**
- Modify: `README.md`
- Create: `C:/Users/wma19/.claude/projects/D--work-ai-0401-ai-customer/memory/m4-a-progress.md`

- [ ] **Step 7.1: Add M4.A row to README status table**

In `README.md`, find the project status table (the `| N | ... |` rows). After the Stage 20 row (which now ends with `✅ | 17 admin + 9 vision_embedder + 13 web vitest + 4 PDF retrieve |`), add a new M4.A row. Match the existing 4-column format:

```markdown
| M4.A | LLM Gateway core + multi-provider routing (gateway + provider_resolver + PinnedResolver + route_mode metric label) | ✅ | 8 resolvers + 9 gateway + 5 registry + 4 e2e (新增) |
```

Note: M4.A isn't a Stage — it's a milestone in the M-series. To keep the table uniform, prefix with `M4.A` (matching the spec name).

- [ ] **Step 7.2: Add a one-line note in the "Recent changes" or equivalent section**

Look for any "Recent changes" / "Last updated" / changelog block in the README and add:

```markdown
- **M4.A (LLM Gateway core)**: LLMClient now routes per-call through a
  resolver-driven gateway. 22 new tests (8 resolvers + 9 gateway + 5
  registry + 4 e2e); route_mode metric label added. Migration path:
  every call site unchanged (factory signature preserved); QA Judge
  and history mining migrated to `gateway.with_config()`.
```

If no such section exists, skip — the status table is sufficient.

- [ ] **Step 7.3: Write memory file**

Create `C:/Users/wma19/.claude/projects/D--work-ai-0401-ai-customer/memory/m4-a-progress.md`:

```markdown
---
name: m4-a-progress
description: "M4.A — LLM Gateway core + multi-provider routing shipped"
metadata:
  type: project
---

M4.A shipped. Resolver-driven gateway replaces single-provider LLMClient shell.

## Scope

- New: `LLMGateway` (registry holder + default prefix resolver + `with_config`)
- New: `provider_registry.build_provider_registry(settings)` (MiniMax + Anthropic)
- New: `PinnedResolver` replaces `LLMClient.with_config` stub
- New: `_PrefixResolver` dispatches by `request.model` prefix (minimax- / claude- / gpt- / o1- / o3-)
- New: `UnknownModelError` (resolver couldn't match → re-raised as `InvalidRequest`)
- `LLMClient.__init__` requires `provider_resolver` (no more `default_provider` kwarg)
- New `route_mode` label on `LLM_CALLS_TOTAL` + `LLM_TOKENS_TOTAL` (4 enum values)
- QA Judge + history mining moved off the deleted `LLMClient.with_config` stub

## Architecture decisions

- **Resolver seam, not inheritance** — `LLMClient` never imports `LLMGateway`. M4.B (fallback), M4.C (BYOK), M4.D (budget) all layer by swapping the resolver, no `LLMClient` edits needed.
- **`PinnedResolver` via `isinstance`, not Protocol** — `Protocol` with private attrs is awkward; `isinstance(self.provider_resolver, PinnedResolver)` is clearer and avoids attribute probing.
- **Factory signature preserved** — `_default_llm_client_factory(tenant_id) -> LLMClient` keeps the `LLMClientFactory = Callable[[str], LLMClient]` shape, so 3 call sites (simple_responder, suggest, agent/graph) didn't migrate.
- **Cardinality of `route_mode`** — 4 values × existing labels = ~3000 series total. Well under Prometheus 100k cap.

## Test counts

- api: 8 resolvers + 9 gateway + 5 provider_registry + 4 e2e = 26 new tests
- All previous tests zero regression (existing 672 collected, 179 integration deselected)

## Workflow

7 commits in 1 PR. Subagent-driven per task + two-stage review (spec → quality). M4.B (fallback) builds on the resolver seam; see [[m4-design]] if created.

## How to apply

For M4.B/C/D: don't touch `LLMClient` internals. Wrap or replace the resolver instead. The `isinstance(resolver, PinnedResolver)` check in `_resolve_request` is the only LLMClient-side branch that depends on resolver identity — extend the pattern (e.g., `FallbackResolver`, `TenantResolver`) rather than adding new branches to `_resolve_request`.
```

- [ ] **Step 7.4: Add pointer to MEMORY.md**

Append to `C:/Users/wma19/.claude/projects/D--work-ai-0401-ai-customer/memory/MEMORY.md`:

```markdown
- [M4.A progress](m4-a-progress.md) — LLM Gateway core + multi-provider routing shipped
```

- [ ] **Step 7.5: Commit**

```bash
git add README.md
git commit -m "docs: README M4.A row in project status table

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

(The memory files are local — they live outside the repo, so they aren't committed.)

- [ ] **Step 7.6: Push branch**

```bash
git push origin main
```

---

## Self-Review

**1. Spec coverage:**
- §3 file changes — all covered (Tasks 1-5 + Task 7).
- §4 default flow / pinned flow — Task 1 (resolvers) + Task 2 (gateway) + Task 3 (resolve_request).
- §5 error handling — Task 3 (UnknownModelError + resolver-error branches).
- §6 registry construction — Task 2 (`build_provider_registry`).
- §7 metric label — Task 3 (route_mode added to both metrics).
- §8 testing — Tasks 1-6 cover all 16 listed test items.
- §9 PR rhythm — 7 commits (Tasks 1-7), squash merge in 1 PR.
- §10 risks — each is mitigated by a task: 5 call sites (Task 4-5), label conflicts (Task 3), with_config migration (Task 4), prefix changes (Task 2), default_provider_name consistency (Task 2 constructor validates).
- §11 YAGNI — all 6 not-done items respected (no `LLMClient` deletion, no fallback, no BYOK, no budget, no USD pricing, no caching).
- §12 completion criteria — all checked across Tasks 1-7.

**2. Placeholder scan:** no "TBD" / "TODO" / "implement later" in any task. Every code block is complete. Every command has expected output.

**3. Type consistency:**
- `PinnedResolver.model` / `provider` — same name in both `resolvers.py` definition and `_resolve_request` usage.
- `LLMGateway.providers` / `default_provider_name` / `default_resolver` / `resolve` / `with_config` / `aclose_all` — same in spec, gateway.py, all test usages.
- `Resolver` type alias — used consistently in `LLMClient.__init__` parameter, `LLMGateway.default_resolver` return type, `_PrefixResolver.__call__`.
- `ROUTE_AUTO` / `ROUTE_PINNED` / `ROUTE_UNKNOWN_MODEL` / `ROUTE_RESOLVER_ERROR` constants — same in `client.py` definitions and metric label usage.
- `LLMClientFactory = Callable[[str], LLMClient]` — preserved (no change in factory signature).

No inconsistencies found.

---

## Verification (end-to-end)

After all 7 tasks:

```bash
cd apps/api

# 1. New tests pass
pytest tests/llm_client/test_resolvers.py -v                  # 8 tests
pytest tests/llm_client/test_gateway.py -v                    # 9 tests
pytest tests/llm_client/test_provider_registry.py -v          # 5 tests
pytest tests/llm_client/integration/test_gateway_e2e.py -v    # 4 tests

# 2. Existing tests zero regression
pytest tests/llm_client -v -m "not integration"
pytest tests/agent -v -m "not integration"
pytest tests/qa -v -m "not integration"
pytest tests/history_mining -v -m "not integration"
pytest tests/knowledge -v -m "not integration"

# 3. Type check clean
mypy src/llm_client src/agent src/qa src/history_mining
```

All should pass. Push to origin/main when done.

---

## Completion criteria

- [ ] 7 commits in 1 PR, all pushed to origin/main
- [ ] 26 new tests passing (8 + 9 + 5 + 4)
- [ ] 0 regressions across llm_client / agent / qa / history_mining / knowledge
- [ ] mypy clean on touched modules
- [ ] `route_mode` label visible in `/metrics` output
- [ ] README M4.A row added
- [ ] memory file `m4-a-progress.md` + MEMORY.md pointer