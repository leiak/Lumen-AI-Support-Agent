"""M4.B close-out regression tests (Task 5).

These pin the non-fallback paths through ``LLMClient.chat``
(``PinnedResolver``, ``_PrefixResolver``) and verify ``LLMGateway`` still
uses ``_PrefixResolver`` when no fallback chain is configured. They guard
against the M4.B ``ainvoke`` branch accidentally swallowing the existing
behavior — a future resolver kind must opt into chain semantics by
exposing ``ainvoke``, not by silently routing through it.

All tests use stubs (no live DB, no HTTP), so they don't belong in
``test_client.py`` (whose docstring declares "Requires live DB") or
``test_gateway.py`` (which targets unit-level gateway behavior).
This dedicated file keeps the regression contract discoverable and
side-effect-free.
"""
import asyncio
from unittest.mock import AsyncMock, create_autospec

from llm_client.client import ROUTE_AUTO, ROUTE_PINNED, LLMClient
from llm_client.exceptions import ProviderUnavailable
from llm_client.gateway import LLMGateway
from llm_client.providers.base import BaseProvider
from llm_client.resolvers import PinnedResolver, _PrefixResolver
from llm_client.types import ChatMessage, ChatRequest, ChatResponse, MessageRole


def _stub_provider(name: str) -> BaseProvider:
    p = create_autospec(BaseProvider, instance=True)
    p.name = name
    return p


def _stub_gateway_provider(name: str) -> BaseProvider:
    p = create_autospec(BaseProvider, instance=True)
    p.name = name
    p.aclose = AsyncMock()  # type: ignore[attr-defined]
    return p


def _stub_response(provider_name: str, model: str) -> ChatResponse:
    return ChatResponse(
        content="ok",
        model=model,
        prompt_tokens=2,
        completion_tokens=1,
        finish_reason="stop",
        provider_name=provider_name,
    )


def _stub_request(model: str) -> ChatRequest:
    return ChatRequest(
        model=model,
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )


def test_pinned_resolver_still_works() -> None:
    """LLMClient + PinnedResolver must keep route_mode="pinned" + rewrite model.

    Regression for M4.B: the ``ainvoke`` branch in ``LLMClient.chat`` must
    NOT fire when the resolver exposes no ``ainvoke`` (``PinnedResolver``
    does not). ``request.model`` is rewritten to the pinned model so the
    wire request body and the metric label carry the actual model name.
    """
    provider = _stub_provider("minimax")
    provider.chat.return_value = _stub_response("minimax", "MiniMax-M3")
    pinned = PinnedResolver(provider=provider, model="MiniMax-M3")
    client = LLMClient(provider_resolver=pinned, tenant_id="t-pinned-regress")

    assert client._route_mode == ROUTE_PINNED
    req = ChatRequest(
        model="some-caller-supplied-name",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    resp = asyncio.run(client.chat(req))

    # PinnedResolver rewrites request.model → MiniMax-M3 before forwarding.
    provider.chat.assert_awaited_once()
    forwarded = provider.chat.await_args.args[0]
    assert forwarded.model == "MiniMax-M3"
    assert resp.model == "MiniMax-M3"


def test_prefix_resolver_still_works() -> None:
    """LLMClient + _PrefixResolver must keep route_mode="auto" + retry loop.

    Regression for M4.B: the ``ainvoke`` branch must NOT fire on a bare
    ``_PrefixResolver`` (it exposes no ``ainvoke``). The local retry loop
    runs on transient ``ProviderUnavailable`` instead — keeping the
    M1-M4.A behavior. ``side_effect`` raises on the first call and
    succeeds on the second, so the retry path is genuinely exercised
    (default ``max_retries=1`` → exactly 2 attempts).
    """
    provider = _stub_provider("anthropic")
    # Fail once with a 5xx-style ProviderUnavailable, succeed on the next
    # call → exercises the local retry loop.
    provider.chat.side_effect = [
        ProviderUnavailable("stub 5xx"),
        _stub_response("anthropic", "claude-haiku-4-5"),
    ]
    prefix = _PrefixResolver(
        providers={"anthropic": provider},
        default_provider_name="anthropic",
    )
    client = LLMClient(provider_resolver=prefix, tenant_id="t-prefix-regress")

    assert client._route_mode == ROUTE_AUTO
    req = ChatRequest(
        model="claude-haiku-4-5",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    resp = asyncio.run(client.chat(req))

    # First call raised, second succeeded → retry loop genuinely ran.
    assert provider.chat.await_count == 2
    assert resp.provider_name == "anthropic"


def test_no_fallback_chain_env_uses_prefix_resolver() -> None:
    """LLMGateway(default_fallback_chain=None) routes by prefix.

    Regression for M4.B: when the env-driven fallback chain is explicitly
    set to ``None``, the gateway MUST still use prefix-based routing.
    It must NOT silently wrap a multi-provider deployment in a
    (single-step) ``FallbackResolver`` — that would change the
    ``route_mode`` label from "auto" to "fallback" and inflate per-step
    metrics unnecessarily.
    """
    a = _stub_gateway_provider("anthropic")
    m = _stub_gateway_provider("minimax")
    g_none = LLMGateway(
        providers={"anthropic": a, "minimax": m},
        default_fallback_chain=None,
    )
    # The regression contract: prefix-based routing still picks the right
    # provider. (The type-check half is covered by
    # ``test_gateway_no_chain_uses_prefix_resolver`` in
    # ``test_gateway_fallback.py`` — no need to duplicate here.)
    req = _stub_request("claude-haiku-4-5")
    assert g_none.default_resolver(req) is a
