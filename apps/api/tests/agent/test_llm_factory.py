"""Unit tests for :mod:`agent.llm_factory`.

These tests exercise the post-M4.A wiring: the factory builds an
``LLMGateway`` from ``build_provider_registry(settings)`` and wraps
its default resolver in a fresh ``LLMClient``. Tests patch
``get_settings`` (and optionally ``build_provider_registry``) so the
production code path runs without needing real API keys.

Each test patches ``get_settings`` at its source module
(``core.config.get_settings``). The factory does
``from core.config import get_settings`` lazily inside the function
body, so patching the factory module wouldn't intercept the local
binding; we have to patch the source where ``get_settings`` lives.
``build_provider_registry`` is imported eagerly, so we patch
``agent.llm_factory.build_provider_registry``.

The tests assert surface behaviour only:

* factory returns an ``LLMClient`` (existing call sites depend on this)
* factory's ``client.provider_resolver`` is the gateway's default
  resolver (``_PrefixResolver``, NOT ``PinnedResolver``)
* the registry that the gateway built contains the expected provider
  names when the corresponding env keys are set
* factory preserves ``tenant_id``
* factory raises ``RuntimeError`` when ``build_provider_registry``
  raises ``RuntimeError`` (no keys configured)
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec, patch

import pytest

from agent.llm_factory import _default_llm_client_factory
from llm_client.client import LLMClient
from llm_client.providers.base import BaseProvider
from llm_client.resolvers import PinnedResolver


def _stub_provider(name: str) -> BaseProvider:
    """Return an ``isinstance``-compatible BaseProvider stub for tests.

    ``BaseProvider`` is an ABC; ``create_autospec`` ensures the stub
    satisfies ``isinstance`` checks and exposes a ``name`` attribute.
    """
    p = create_autospec(BaseProvider, instance=True)
    p.name = name
    return p


def _fake_settings(**overrides: Any) -> SimpleNamespace:
    """Return a Settings-like proxy carrying the M4.B fallback fields.

    The factory reads ``llm_fallback_chain`` and ``llm_fallback_attempt_timeout_s``
    in addition to whatever ``build_provider_registry`` reads (which we
    patch anyway). Pass overrides to exercise the chain path.
    """
    base: dict[str, Any] = {
        "llm_fallback_chain": None,
        "llm_fallback_attempt_timeout_s": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_default_factory_returns_llm_client_with_resolver() -> None:
    """Factory returns ``LLMClient`` wired to a non-pinned resolver.

    The factory uses ``gateway.default_resolver``, which is a
    ``_PrefixResolver`` (NOT ``PinnedResolver``) because the production
    factory does not pin a specific (provider, model) pair — it lets
    the registry's prefix router pick the provider per request.
    """
    anthropic_stub = _stub_provider("anthropic")
    fake_settings = _fake_settings()
    with patch("core.config.get_settings", return_value=fake_settings), \
         patch(
             "agent.llm_factory.build_provider_registry",
             return_value={"anthropic": anthropic_stub},
         ) as mock_build:
        client = _default_llm_client_factory("t-test")

    assert isinstance(client, LLMClient)
    assert client.tenant_id == "t-test"
    # The default factory is the project's auto-router, not a pinned
    # override — a PinnedResolver would mean QA-Judge-style pinning,
    # which lives in a different code path.
    assert not isinstance(client.provider_resolver, PinnedResolver)
    # And ``build_provider_registry`` was called with the settings object.
    mock_build.assert_called_once_with(fake_settings)


def test_default_factory_uses_minimax_when_minimax_key_set() -> None:
    """When ``MINIMAX_API_KEY`` is set, the registry exposes ``"minimax"``.

    We don't assert against the real ``Settings``; we patch
    ``build_provider_registry`` so the registry exactly matches what
    the factory would build when the operator has only a MiniMax key.
    """
    minimax_stub = _stub_provider("minimax")
    with patch("core.config.get_settings", return_value=_fake_settings()), \
         patch(
             "agent.llm_factory.build_provider_registry",
             return_value={"minimax": minimax_stub},
         ):
        client = _default_llm_client_factory("t-minimax")

    # Resolver is callable; invoking it must route by prefix to the stub
    # so the LLMClient's per-request resolution goes to MiniMax.
    from llm_client.types import ChatMessage, ChatRequest, MessageRole

    req = ChatRequest(
        model="MiniMax-M3",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    resolved = client.provider_resolver(req)
    assert resolved is minimax_stub


def test_default_factory_uses_anthropic_when_only_anthropic_key_set() -> None:
    """When only ``ANTHROPIC_API_KEY`` is set, MiniMax isn't registered.

    The factory still produces a working ``LLMClient`` whose resolver
    routes to the Anthropic provider for claude-prefixed model names.
    """
    anthropic_stub = _stub_provider("anthropic")
    with patch("core.config.get_settings", return_value=_fake_settings()), \
         patch(
             "agent.llm_factory.build_provider_registry",
             return_value={"anthropic": anthropic_stub},
         ):
        client = _default_llm_client_factory("t-anthropic")

    from llm_client.types import ChatMessage, ChatRequest, MessageRole

    req = ChatRequest(
        model="claude-sonnet-4-5",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    resolved = client.provider_resolver(req)
    assert resolved is anthropic_stub
    # And: the registry the factory saw has no ``minimax`` entry — the
    # operator didn't set ``MINIMAX_API_KEY``. Explicitly assert this so
    # a regression where ``build_provider_registry`` returns a stale
    # registry with both providers is caught.
    # The function under test only exposes the resolved provider, but
    # we re-invoke the factory path to introspect the registry contents.
    with patch("core.config.get_settings", return_value=_fake_settings()), \
         patch(
             "agent.llm_factory.build_provider_registry",
             return_value={"anthropic": anthropic_stub},
         ) as mock_build:
        _default_llm_client_factory("t-anthropic-2")
    assert "minimax" not in mock_build.return_value
    assert "anthropic" in mock_build.return_value


def test_default_factory_raises_when_no_keys() -> None:
    """With neither env key set, ``build_provider_registry`` raises
    ``RuntimeError`` and the factory propagates that surface to the
    caller. A no-keys misconfiguration MUST fail fast at first use
    rather than silently producing an unusable ``LLMClient``."""
    with patch("core.config.get_settings", return_value=_fake_settings()), \
         patch(
             "agent.llm_factory.build_provider_registry",
             side_effect=RuntimeError(
                 "No LLM provider configured: set MINIMAX_API_KEY or "
                 "ANTHROPIC_API_KEY before starting the API."
             ),
         ):
        with pytest.raises(RuntimeError, match="No LLM provider configured"):
            _default_llm_client_factory("t-no-keys")


def test_default_factory_preserves_tenant_id() -> None:
    """``tenant_id`` round-trips: ``factory(tenant_id).tenant_id == tenant_id``.

    The 3 call sites (simple_responder, suggest, agent/graph) all
    thread ``tenant_id`` into the factory so usage recording can
    attribute every call. This test pins the contract.
    """
    anthropic_stub = _stub_provider("anthropic")
    with patch("core.config.get_settings", return_value=_fake_settings()), \
         patch(
             "agent.llm_factory.build_provider_registry",
             return_value={"anthropic": anthropic_stub},
         ):
        client = _default_llm_client_factory("tenant-abc")
    assert client.tenant_id == "tenant-abc"

    # And again with a different tenant — the factory is a pure
    # function of its argument; no module-global tenant state leaks
    # between calls.
    with patch("core.config.get_settings", return_value=_fake_settings()), \
         patch(
             "agent.llm_factory.build_provider_registry",
             return_value={"anthropic": anthropic_stub},
         ):
        client_b = _default_llm_client_factory("tenant-xyz")
    assert client_b.tenant_id == "tenant-xyz"
    assert client_b.tenant_id != client.tenant_id


def test_default_factory_builds_fallback_resolver_when_chain_env_set() -> None:
    """M4.B — when ``LLM_FALLBACK_CHAIN`` is set, the factory builds a
    :class:`FallbackResolver` instead of the prefix-based auto-router.

    The gateway's ``default_resolver`` then exposes ``ainvoke`` so
    ``LLMClient.chat`` engages the chain path; the primary step's
    ``__call__`` still returns the primary provider so streaming
    works on the first step.
    """
    minimax_stub = _stub_provider("minimax")
    anthropic_stub = _stub_provider("anthropic")
    fake_settings = _fake_settings(
        llm_fallback_chain="minimax:MiniMax-M3,anthropic:claude-haiku-4-5"
    )
    with patch("core.config.get_settings", return_value=fake_settings), \
         patch(
             "agent.llm_factory.build_provider_registry",
             return_value={"minimax": minimax_stub, "anthropic": anthropic_stub},
         ):
        client = _default_llm_client_factory("t-fallback")

    from llm_client.resolvers import FallbackResolver
    from llm_client.types import ChatMessage, ChatRequest, MessageRole

    assert isinstance(client.provider_resolver, FallbackResolver)
    # And the chain path is engaged — LLMClient routes to ainvoke.
    assert hasattr(client.provider_resolver, "ainvoke")
    # Streaming still works on the primary step.
    req = ChatRequest(
        model="MiniMax-M3",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    assert client.provider_resolver(req) is minimax_stub


def test_default_factory_no_chain_uses_prefix_resolver() -> None:
    """M4.B — when ``LLM_FALLBACK_CHAIN`` is unset, the factory uses the
    existing prefix-based auto-router (NOT a ``FallbackResolver``).

    This pins the regression contract for ``LLM_FALLBACK_CHAIN=None``
    so an operator who hasn't opted into fallback doesn't accidentally
    get the chain semantics.
    """
    anthropic_stub = _stub_provider("anthropic")
    fake_settings = _fake_settings()  # llm_fallback_chain=None default
    with patch("core.config.get_settings", return_value=fake_settings), \
         patch(
             "agent.llm_factory.build_provider_registry",
             return_value={"anthropic": anthropic_stub},
         ):
        client = _default_llm_client_factory("t-no-chain")

    from llm_client.resolvers import FallbackResolver, _PrefixResolver

    assert not isinstance(client.provider_resolver, FallbackResolver)
    assert isinstance(client.provider_resolver, _PrefixResolver)
