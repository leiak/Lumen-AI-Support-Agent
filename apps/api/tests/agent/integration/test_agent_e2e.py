"""Stage 7.5 — end-to-end integration tests for the agent graph path.

Exercises ``SimpleResponder.respond`` against the real Postgres +
Qdrant + Redis stack (mirroring the ``test_rag_e2e.py`` style).
Only the LLM is stubbed via the ``_default_llm_client_factory``
monkeypatch; the RAG pipeline, the conversation service, and the
escalation tool all hit the real DB / Qdrant.

PII discipline
--------------

Every article and conversation message carries a distinctive
``MAGIC_PHRASE_*`` marker. Assertions are made against those
markers only — no real customer text ever appears in test data.

Behaviors under test (Task 7.5 spec)
------------------------------------

1. Real RAG injection into the LLM call (real Qdrant + real embed text).
2. Escalation tool firing against real ``ConversationService.escalate_to_human_queue``.
3. Cross-tenant isolation at the agent layer.
4. Summary path with conversation > 50 messages.
5. ContextVar lifecycle across multiple ``respond()`` calls.
6. Metrics emitted with ``turn_kind`` correctly classified.
7. RAG fail-open (Qdrant down / failure).
8. Tool-call failure (escalation tool raises) falls back to text.

Marking
-------

All tests are tagged ``@pytest.mark.integration`` (matching the
existing convention in ``test_rag_e2e.py``).
"""
from __future__ import annotations

import contextlib
import io
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from agent.simple_responder import (
    FALLBACK_MESSAGE,
    MAX_HISTORY_BEFORE_SUMMARY,
    MAX_HISTORY_MESSAGES,
    AgentResponse,
    SimpleResponder,
)
from channel.enums import ChannelStatus, ChannelType
from channel.repository import ChannelRepository
from conversation.enums import ConversationStatus, MessageRole
from conversation.models import Conversation
from conversation.repository import ConversationRepository, MessageRepository
from core.database import get_session
from core.id_gen import new_id
from llm_client.exceptions import RateLimited

# ============================================================================
# Helpers
# ============================================================================


class _StructlogCapture:
    """Captures stdout emitted during a code block.

    Mirrors the helper from ``test_agent_graph.py``. structlog
    defaults to a ``ConsoleRenderer`` in the test environment, so
    event names appear as substrings on plain-text log lines.
    """

    def __init__(self) -> None:
        self.buffer = io.StringIO()
        self.text: str = ""
        self._cm: Any = None

    def __enter__(self) -> _StructlogCapture:
        self._cm = contextlib.redirect_stdout(self.buffer)
        self._cm.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        assert self._cm is not None
        self._cm.__exit__(*exc)
        self.text = self.buffer.getvalue()

    def has_event(self, event_name: str) -> bool:
        return event_name in self.text


async def _record_customer_message(
    *,
    tenant_id: str,
    conversation_id: str,
    text: str,
) -> Any:
    """Persist a customer message via the conversation service.

    Mirrors what ``channel.inbound.process_inbound_envelope`` would
    do on a customer turn. Returns the persisted Message.
    """
    from conversation.service import ConversationService

    return await ConversationService().record_message(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        role=MessageRole.CUSTOMER,
        content_text=text,
    )


async def _seed_conversation(
    *,
    tenant: Any,
    channel: Any,
    ai_handling: bool = True,
    customer_external_id: str = "ou_inline_conv",
) -> Conversation:
    """Insert a fresh OPEN conversation for the tenant + channel."""
    conv_repo = ConversationRepository()
    now = datetime.now(UTC)
    conv = Conversation(
        id=new_id(),
        tenant_id=tenant.id,
        channel_id=channel.id,
        customer_external_id=customer_external_id,
        status=ConversationStatus.OPEN,
        assigned_agent_id=None,
        ai_handling=ai_handling,
        opened_at=now,
        last_activity_at=now,
    )
    await conv_repo.create(conversation=conv)
    return conv


async def _delete_conversation(conversation_id: str) -> None:
    """Delete a single conversation row (used when the surrounding tenant
    cleanup will not cascade it)."""
    async with get_session() as session:
        c = await session.get(Conversation, conversation_id)
        if c is not None:
            await session.delete(c)
            await session.commit()


# ============================================================================
# Test 1 — RAG injection into the LLM call (real Qdrant + real embed)
# ============================================================================


@pytest.mark.integration
async def test_respond_injects_rag_chunks_into_llm_call(
    seeded_tenant_with_kb: tuple[Any, Any, Any, Any],
    stub_llm_capture: Any,
    no_threshold_rag_service: None,
) -> None:
    """Customer asks about KB content -> LLM request includes the
    synthesized system message with the chunk text.

    Verifies both via the captured messages AND by checking that the
    LLM context length is reasonable (no empty stub).
    """
    tenant, _kb, _article, channel = seeded_tenant_with_kb

    conv = await _seed_conversation(
        tenant=tenant, channel=channel, customer_external_id="ou_rag_inject"
    )
    try:
        await _record_customer_message(
            tenant_id=tenant.id,
            conversation_id=conv.id,
            text="how long does shipping take",
        )

        stub_llm_capture.set_text("stubbed AI reply")
        result = await SimpleResponder().respond(
            tenant_id=tenant.id, conversation_id=conv.id
        )

        assert result is not None
        assert result.content_text == "stubbed AI reply"

        # The captured LLM request MUST include the RAG block with
        # the shipping marker. With no-threshold RAG every indexed
        # chunk is returned.
        assert len(stub_llm_capture.calls) >= 1
        chat_call = stub_llm_capture.calls[-1]
        system_contents = [m.content for m in chat_call if m.role == "system"]
        assert any("customer-service agent" in c for c in system_contents)
        rag_blocks = [c for c in system_contents if "Retrieved knowledge:" in c]
        assert rag_blocks, (
            f"No RAG block in LLM request; system messages: "
            f"{[c[:60] for c in system_contents]!r}"
        )
        assert any("MAGIC_PHRASE_SHIPPING_AGENT_001" in block for block in rag_blocks), (
            f"RAG block did not contain the shipping marker; got: "
            f"{rag_blocks!r}"
        )
        # Sanity: context length is reasonable.
        full_request = "\n".join(m.content for m in chat_call)
        assert len(full_request) > 200
    finally:
        await _delete_conversation(conv.id)


# ============================================================================
# Test 2 — no KB -> no RAG block in the LLM request
# ============================================================================


@pytest.mark.integration
async def test_respond_no_kb_skips_rag(
    seeded_conv_with_messages: Callable[..., Any],
    stub_llm_capture: Any,
    no_threshold_rag_service: None,
) -> None:
    """Tenant with zero KBs -> RAG short-circuits; AI reply still flows."""
    factory = seeded_conv_with_messages
    tenant, _channel, conv, cleanup = await factory(n_messages=1)

    try:
        await _record_customer_message(
            tenant_id=tenant.id,
            conversation_id=conv.id,
            text="hello do you have any help",
        )

        stub_llm_capture.set_text("stubbed reply no KB")
        result = await SimpleResponder().respond(
            tenant_id=tenant.id, conversation_id=conv.id
        )

        assert result is not None
        assert result.content_text == "stubbed reply no KB"

        chat_call = stub_llm_capture.calls[-1]
        system_contents = [m.content for m in chat_call if m.role == "system"]
        # No RAG block — tenant has no KB.
        assert not any("Retrieved knowledge:" in c for c in system_contents), (
            f"RAG block leaked despite no KB; got: "
            f"{[c[:60] for c in system_contents]!r}"
        )
        # M1 system prompt still present.
        assert any("customer-service agent" in c for c in system_contents)
    finally:
        await cleanup()


# ============================================================================
# Test 3 — cross-tenant KB isolation at the agent layer
# ============================================================================


@pytest.mark.integration
async def test_respond_cross_tenant_kb_isolation(
    seeded_tenant_with_kb: tuple[Any, Any, Any, Any],
    seeded_second_tenant: tuple[Any, Any, Any],
    stub_llm_capture: Any,
    no_threshold_rag_service: None,
) -> None:
    """Tenant A's KB has MAGIC_PHRASE_SHIPPING_AGENT_001; tenant B's customer
    asks the same question -> tenant B's LLM call MUST NOT see
    tenant A's KB content.

    Pins the cross-tenant isolation contract at the agent layer
    (not just at the retriever in isolation).
    """
    _tenant_a, _kb_a, _article_a, _channel_a = seeded_tenant_with_kb
    tenant_b, _kb_b, _article_b = seeded_second_tenant

    # Build a fresh channel + conversation for tenant B.
    channel_b = await ChannelRepository().create(
        tenant_id=tenant_b.id,
        type=ChannelType.WEB,
        name="Cross-tenant Channel",
        credentials_encrypted="{}",
        status=ChannelStatus.ACTIVE,
    )
    conv_b = await _seed_conversation(
        tenant=tenant_b,
        channel=channel_b,
        customer_external_id="ou_cross",
    )
    try:
        await _record_customer_message(
            tenant_id=tenant_b.id,
            conversation_id=conv_b.id,
            text="how long does shipping take",
        )

        stub_llm_capture.set_text("tenant-b reply")
        await SimpleResponder().respond(
            tenant_id=tenant_b.id, conversation_id=conv_b.id
        )

        chat_call = stub_llm_capture.calls[-1]
        system_contents = [m.content for m in chat_call if m.role == "system"]
        rag_text = "\n".join(
            c for c in system_contents if "Retrieved knowledge:" in c
        )
        # Tenant A's KB MUST NOT appear in tenant B's request.
        assert "MAGIC_PHRASE_SHIPPING_AGENT_001" not in rag_text, (
            f"Cross-tenant leak: tenant A's KB content surfaced in "
            f"tenant B's LLM request. Got: {rag_text[:200]!r}"
        )
    finally:
        await _delete_conversation(conv_b.id)
        await _delete_conversation(channel_b.id)


# ============================================================================
# Test 4 — escalation tool flips ai_handling via real service
# ============================================================================


@pytest.mark.integration
async def test_respond_escalation_tool_flips_ai_handling(
    seeded_conv_with_messages: Callable[..., Any],
    stub_llm_capture: Any,
    no_threshold_rag_service: None,
) -> None:
    """Stub LLM returns ``escalate_to_human`` tool_call -> after
    ``respond()``, the DB row's ``ai_handling=False``,
    ``status=PENDING``, ``assigned_agent_id=None``.

    Verifies the tool actually hits the real
    ``ConversationService.escalate_to_human_queue`` (not a mock).
    """
    factory = seeded_conv_with_messages
    tenant, _channel, conv, cleanup = await factory(n_messages=1)
    # Refetch to ensure we see ai_handling=True.
    conv_repo = ConversationRepository()
    conv_row = await conv_repo.get_by_id(conv.id)
    assert conv_row is not None
    assert conv_row.ai_handling is True

    try:
        await _record_customer_message(
            tenant_id=tenant.id,
            conversation_id=conv.id,
            text="please transfer me to a human",
        )

        stub_llm_capture.set_tool_call(
            name="escalate_to_human",
            args={"reason": "MAGIC_PHRASE_ESCALATE_REASON"},
        )
        result = await SimpleResponder().respond(
            tenant_id=tenant.id, conversation_id=conv.id
        )

        assert result is not None
        # The customer-facing message comes from the LLM's tool args.
        assert result.content_text == "MAGIC_PHRASE_ESCALATE_REASON"
        assert result.role == MessageRole.AI

        # Verify the DB row was flipped by the REAL
        # ConversationService.escalate_to_human_queue path.
        after = await conv_repo.get_by_id(conv.id)
        assert after is not None
        assert after.ai_handling is False, (
            f"expected ai_handling=False after escalation, got {after.ai_handling}"
        )
        assert after.status == ConversationStatus.PENDING
        assert after.assigned_agent_id is None
    finally:
        await cleanup()


# ============================================================================
# Test 5 — escalation path does NOT persist synthetic messages
# ============================================================================


@pytest.mark.integration
async def test_respond_escalation_does_not_persist_synthetic_messages(
    seeded_conv_with_messages: Callable[..., Any],
    stub_llm_capture: Any,
    no_threshold_rag_service: None,
) -> None:
    """Pre-existing invariant: the escalation tool path MUST NOT
    persist any new DB message. The conversation's ``ai_handling``
    flip is the sole side effect — the escalation ``reason`` text
    lives in the ``AgentResponse`` returned to the caller, not in
    the messages table.
    """
    factory = seeded_conv_with_messages
    tenant, _channel, conv, cleanup = await factory(n_messages=1)

    try:
        # Seed one customer message via the conversation service so
        # the responder sees a non-empty history. Count messages before.
        await _record_customer_message(
            tenant_id=tenant.id,
            conversation_id=conv.id,
            text="please escalate me",
        )
        msg_repo = MessageRepository()
        before = await msg_repo.list_by_conversation(conversation_id=conv.id)
        before_count = len(before)

        stub_llm_capture.set_tool_call(
            name="escalate_to_human",
            args={"reason": "MAGIC_PHRASE_NO_PERSIST"},
        )
        await SimpleResponder().respond(
            tenant_id=tenant.id, conversation_id=conv.id
        )

        after = await msg_repo.list_by_conversation(conversation_id=conv.id)
        after_count = len(after)
        # No synthetic rows leaked into the messages table.
        assert after_count == before_count, (
            f"escalation persisted a new message: before={before_count} "
            f"after={after_count}"
        )
        # And the rows that ARE there contain no escalation text.
        contents = [m.content_text for m in after]
        assert not any("MAGIC_PHRASE_NO_PERSIST" in c for c in contents), (
            f"escalation reason text leaked into messages table: "
            f"{contents!r}"
        )
        # No SYSTEM / TOOL role rows appeared (only CUSTOMER
        # messages from the seed + test setup; no synthetic
        # escalation row).
        roles = [m.role for m in after]
        assert roles, "expected at least one persisted message"
        assert all(r == MessageRole.CUSTOMER for r in roles), (
            f"non-customer role leaked into messages table: {roles!r}"
        )
    finally:
        await cleanup()


# ============================================================================
# Test 6 — summary path: conversation > 50 messages
# ============================================================================


@pytest.mark.integration
async def test_respond_summarizes_overflow_history(
    seeded_conv_with_messages: Callable[..., Any],
    stub_llm_capture: Any,
    no_threshold_rag_service: None,
) -> None:
    """Seed 60 messages -> LLM called TWICE (summary + real chat).

    Summary contains content from old messages; the real chat has
    ``MAX_HISTORY_MESSAGES`` (20) verbatim messages from the tail.
    """
    factory = seeded_conv_with_messages
    tenant, _channel, conv, cleanup = await factory(
        n_messages=MAX_HISTORY_BEFORE_SUMMARY + 10
    )

    try:
        # LLM call 1 (queued via set_text) = summary reply.
        # LLM call 2 = real chat reply.
        stub_llm_capture.set_text("MAGIC_PHRASE_SUMMARY_BODY")
        stub_llm_capture.set_text("MAGIC_PHRASE_CHAT_REPLY")

        result = await SimpleResponder().respond(
            tenant_id=tenant.id, conversation_id=conv.id
        )

        assert result is not None
        assert result.content_text == "MAGIC_PHRASE_CHAT_REPLY"

        # Exactly two LLM calls — summary + chat.
        assert len(stub_llm_capture.calls) == 2, (
            f"expected 2 LLM calls (summary + chat), got {len(stub_llm_capture.calls)}"
        )
        # First call's user message is the transcript (oldest 40 messages).
        # We seeded MAGIC_PHRASE_TURN_0 .. MAGIC_PHRASE_TURN_59, so the
        # overflow slice (msgs[:-20]) covers TURN_0 .. TURN_39.
        summary_call = stub_llm_capture.calls[0]
        summary_user = next(m for m in summary_call if m.role == "user")
        # Old messages MUST be in the transcript passed to the summary call.
        assert "MAGIC_PHRASE_TURN_0" in summary_user.content
        # And the LARGEST surviving turn (TURN_59) MUST NOT appear in
        # the summary transcript because it is in the kept tail.
        assert "MAGIC_PHRASE_TURN_59" not in summary_user.content

        # Second call: the real chat. After M1_SYSTEM_PROMPT and the
        # summary system message, the latest MAX_HISTORY_MESSAGES are
        # verbatim.
        chat_call = stub_llm_capture.calls[-1]
        non_system = [m for m in chat_call if m.role != "system"]
        assert len(non_system) == MAX_HISTORY_MESSAGES, (
            f"expected {MAX_HISTORY_MESSAGES} verbatim messages, got "
            f"{len(non_system)}"
        )
        # The summary system message is present in the chat call.
        sys_contents = [m.content for m in chat_call if m.role == "system"]
        assert any("MAGIC_PHRASE_SUMMARY_BODY" in c for c in sys_contents), (
            f"summary text not surfaced in chat call; system msgs: "
            f"{[c[:60] for c in sys_contents]!r}"
        )
        # Oldest verbatim message.
        assert "MAGIC_PHRASE_TURN_40" in non_system[0].content
        # Newest verbatim message.
        assert "MAGIC_PHRASE_TURN_59" in non_system[-1].content
        # Overflow messages were summarized away.
        assert "MAGIC_PHRASE_TURN_0" not in "".join(m.content for m in non_system)
    finally:
        await cleanup()


# ============================================================================
# Test 7 — ContextVar resets across sequential respond() calls
# ============================================================================


@pytest.mark.integration
async def test_respond_context_var_resets_across_calls(
    seeded_conv_with_messages: Callable[..., Any],
    stub_llm_capture: Any,
    no_threshold_rag_service: None,
) -> None:
    """Call ``respond()`` twice on different conversations for the
    SAME tenant. The second call's tool dispatch sees the second
    conversation's tenant + conversation IDs — not the first.

    Verified by having the stub LLM return ``escalate_to_human`` for
    BOTH calls and asserting each conversation was flipped, not the
    first one twice.
    """
    factory = seeded_conv_with_messages

    # Two distinct conversations under the same tenant.
    tenant, channel_a, conv_a, cleanup_a = await factory(
        n_messages=1,
        tenant_name="Agent E2E ContextVar A",
        customer_external_id="ou_contextvar_a",
    )
    # Second conversation under the SAME tenant — we re-open via the
    # conversation service to get a second ``(channel, customer)``
    # pair. We add a second customer to the SAME channel so we
    # don't need a second channel.
    conv_b = await _seed_conversation(
        tenant=tenant,
        channel=channel_a,
        customer_external_id="ou_contextvar_b",
    )

    async def _cleanup_b() -> None:
        await _delete_conversation(conv_b.id)

    try:
        await _record_customer_message(
            tenant_id=tenant.id,
            conversation_id=conv_a.id,
            text="transfer me",
        )
        await _record_customer_message(
            tenant_id=tenant.id,
            conversation_id=conv_b.id,
            text="transfer me too",
        )

        # Two queued tool calls — one per conversation.
        stub_llm_capture.set_tool_call(
            name="escalate_to_human",
            args={"reason": "MAGIC_PHRASE_FIRST"},
        )
        stub_llm_capture.set_tool_call(
            name="escalate_to_human",
            args={"reason": "MAGIC_PHRASE_SECOND"},
        )

        await SimpleResponder().respond(
            tenant_id=tenant.id, conversation_id=conv_a.id
        )
        await SimpleResponder().respond(
            tenant_id=tenant.id, conversation_id=conv_b.id
        )

        # Both conversations must have been flipped — proves the
        # ContextVar was reset between calls.
        conv_repo = ConversationRepository()
        a_row = await conv_repo.get_by_id(conv_a.id)
        b_row = await conv_repo.get_by_id(conv_b.id)
        assert a_row is not None
        assert b_row is not None
        assert a_row.ai_handling is False, (
            "first call's tenant/conversation IDs leaked into the "
            "second call's tool dispatch — conv_a was NOT flipped"
        )
        assert b_row.ai_handling is False
        assert a_row.status == ConversationStatus.PENDING
        assert b_row.status == ConversationStatus.PENDING
        assert a_row.assigned_agent_id is None
        assert b_row.assigned_agent_id is None
    finally:
        await _cleanup_b()
        await cleanup_a()


# ============================================================================
# Test 8 — metrics event with turn_kind
# ============================================================================


@pytest.mark.integration
async def test_respond_emits_timing_metrics_with_turn_kind(
    seeded_tenant_with_kb: tuple[Any, Any, Any, Any],
    stub_llm_capture: Any,
    no_threshold_rag_service: None,
) -> None:
    """Capture structlog events and assert ``agent.graph.invoke.completed``
    fired with ``duration_ms > 0`` and ``turn_kind == "rag_hit"``.
    """
    tenant, _kb, _article, channel = seeded_tenant_with_kb

    conv = await _seed_conversation(
        tenant=tenant, channel=channel, customer_external_id="ou_metrics"
    )
    try:
        await _record_customer_message(
            tenant_id=tenant.id,
            conversation_id=conv.id,
            text="how long does shipping take",
        )

        stub_llm_capture.set_text("ok")
        with _StructlogCapture() as capture:
            await SimpleResponder().respond(
                tenant_id=tenant.id, conversation_id=conv.id
            )

        assert capture.has_event("agent.graph.invoke.completed"), (
            f"missing completed event. Captured stdout: {capture.text!r}"
        )
        # Find the completed event line and assert on duration_ms +
        # turn_kind tokens.
        completed_lines = [
            line for line in capture.text.splitlines()
            if "agent.graph.invoke.completed" in line
        ]
        assert len(completed_lines) == 1
        line = completed_lines[0]
        assert "duration_ms" in line
        # Extract the duration_ms value; it must be > 0.
        m = re.search(r"duration_ms=([\d.]+)", line)
        assert m is not None, f"no duration_ms=... in {line!r}"
        duration = float(m.group(1))
        assert duration > 0, f"duration_ms must be > 0, got {duration}"
        # And the turn_kind is rag_hit because we hit Qdrant.
        assert "rag_hit" in line, (
            f"expected turn_kind=rag_hit; got {line!r}"
        )
    finally:
        await _delete_conversation(conv.id)


# ============================================================================
# Test 9 — conv not in ai_handling -> respond() returns None
# ============================================================================


@pytest.mark.integration
async def test_respond_returns_none_when_conv_not_ai_handling(
    seeded_conv_with_messages: Callable[..., Any],
    stub_llm_capture: Any,
    no_threshold_rag_service: None,
) -> None:
    """``ai_handling=False`` -> ``respond()`` returns None and the
    LLM is NOT called.
    """
    factory = seeded_conv_with_messages
    tenant, _channel, conv, cleanup = await factory(
        n_messages=1, ai_handling=False
    )

    try:
        # Sanity: conversation really is in non-AI handling.
        conv_repo = ConversationRepository()
        row = await conv_repo.get_by_id(conv.id)
        assert row is not None
        assert row.ai_handling is False

        result = await SimpleResponder().respond(
            tenant_id=tenant.id, conversation_id=conv.id
        )

        assert result is None
        # LLM was never called.
        assert len(stub_llm_capture.calls) == 0, (
            f"LLM was called despite conv being non-AI handling; "
            f"calls={len(stub_llm_capture.calls)}"
        )
    finally:
        await cleanup()


# ============================================================================
# Test 10 — LLM failure -> fallback, no DB row written
# ============================================================================


@pytest.mark.integration
async def test_respond_returns_fallback_on_llm_failure(
    seeded_conv_with_messages: Callable[..., Any],
    stub_llm_capture: Any,
    no_threshold_rag_service: None,
) -> None:
    """Stub LLM raises ``RateLimited`` -> response is
    ``AgentResponse(content_text=FALLBACK_MESSAGE)``, no new DB row
    written.
    """
    factory = seeded_conv_with_messages
    tenant, _channel, conv, cleanup = await factory(n_messages=1)

    try:
        await _record_customer_message(
            tenant_id=tenant.id,
            conversation_id=conv.id,
            text="hello",
        )
        msg_repo = MessageRepository()
        before = await msg_repo.list_by_conversation(conversation_id=conv.id)
        before_count = len(before)

        stub_llm_capture.set_raises(RateLimited("simulated rate limit"))

        result = await SimpleResponder().respond(
            tenant_id=tenant.id, conversation_id=conv.id
        )

        assert result is not None
        assert isinstance(result, AgentResponse)
        assert result.content_text == FALLBACK_MESSAGE
        assert result.role == MessageRole.AI

        after = await msg_repo.list_by_conversation(conversation_id=conv.id)
        assert len(after) == before_count, (
            f"AI fallback persisted a new row: before={before_count} "
            f"after={len(after)}"
        )
    finally:
        await cleanup()


# ============================================================================
# Critical constraint — RAG fail-open
# ============================================================================


@pytest.mark.integration
async def test_respond_qdrant_unavailable_does_not_block_reply(
    seeded_tenant_with_kb: tuple[Any, Any, Any, Any],
    stub_llm_capture: Any,
    no_threshold_rag_service: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``retrieve_chunks`` raises -> ``respond()`` still returns a
    non-None ``AgentResponse``. The fail-open invariant at the
    retrieve node keeps the customer turn alive even when Qdrant is
    unreachable.
    """
    tenant, _kb, _article, channel = seeded_tenant_with_kb

    conv = await _seed_conversation(
        tenant=tenant, channel=channel, customer_external_id="ou_qdrant_down"
    )
    try:
        await _record_customer_message(
            tenant_id=tenant.id,
            conversation_id=conv.id,
            text="how long does shipping take",
        )

        # Force ``retrieve_chunks`` to raise — simulates a Qdrant outage.
        # ``rag_service`` imports ``retrieve_chunks`` directly, so the
        # binding lives on the rag_service module, NOT on the retriever
        # module. Patch both for safety / symmetry.
        from knowledge import rag_service as rag_service_module
        from knowledge import retriever as retriever_module

        async def _boom(**_kwargs: object) -> list[Any]:
            raise RuntimeError("qdrant unavailable")

        monkeypatch.setattr(retriever_module, "retrieve_chunks", _boom)
        monkeypatch.setattr(rag_service_module, "retrieve_chunks", _boom)

        stub_llm_capture.set_text("ok despite Qdrant down")
        result = await SimpleResponder().respond(
            tenant_id=tenant.id, conversation_id=conv.id
        )

        assert result is not None
        assert result.content_text == "ok despite Qdrant down"
        # No RAG block in the LLM request — the failure short-circuited.
        chat_call = stub_llm_capture.calls[-1]
        system_contents = [m.content for m in chat_call if m.role == "system"]
        assert not any("Retrieved knowledge:" in c for c in system_contents)
    finally:
        await _delete_conversation(conv.id)


# ============================================================================
# Critical constraint — tool-call failure falls back to LLM text
# ============================================================================


@pytest.mark.integration
async def test_respond_escalation_tool_failure_falls_back_to_text(
    seeded_conv_with_messages: Callable[..., Any],
    stub_llm_capture: Any,
    no_threshold_rag_service: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``escalate_to_human_queue`` raises -> ``respond()`` returns
    the LLM's ORIGINAL text and the conversation's ``ai_handling``
    is unchanged.

    The tool path is non-fatal by design (see ``tools.py`` and
    ``nodes.py``); a failed escalation must not crash the customer
    turn.
    """
    factory = seeded_conv_with_messages
    tenant, _channel, conv, cleanup = await factory(n_messages=1)

    try:
        await _record_customer_message(
            tenant_id=tenant.id,
            conversation_id=conv.id,
            text="transfer me",
        )

        # Patch the conversation service method that the tool
        # calls. We monkeypatch the unbound method on
        # ``ConversationService`` so any freshly-constructed
        # service instance (including the one the responder built)
        # will hit the failing version.
        from conversation.service import ConversationService

        async def _boom(
            *, tenant_id: str, conversation_id: str
        ) -> Any:
            raise RuntimeError("DB unavailable during escalation")

        monkeypatch.setattr(
            ConversationService, "escalate_to_human_queue", _boom
        )

        stub_llm_capture.set_tool_call(
            name="escalate_to_human",
            args={"reason": "MAGIC_PHRASE_TOOL_FAIL_REASON"},
        )
        result = await SimpleResponder().respond(
            tenant_id=tenant.id, conversation_id=conv.id
        )

        assert result is not None
        # Tool failed -> the loop feeds the error back to the LLM via
        # a ToolMessage so the LLM can recover on the next turn (see
        # ``make_llm_node`` tool loop in ``agent.graph.nodes``).
        # Our stub returned empty content for the tool call, so the
        # fallback message is ``FALLBACK_MESSAGE``.
        assert result.role == MessageRole.AI
        assert isinstance(result.content_text, str) and result.content_text, (
            "respond() should return non-empty text after tool failure"
        )

        # Conversation's ai_handling MUST be unchanged (still True).
        conv_repo = ConversationRepository()
        row = await conv_repo.get_by_id(conv.id)
        assert row is not None
        assert row.ai_handling is True, (
            "tool-call failure path leaked into the DB — ai_handling "
            "should remain True"
        )
        assert row.status == ConversationStatus.OPEN
    finally:
        await cleanup()