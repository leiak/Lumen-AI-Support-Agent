"""Tests for channel.inbound.process_inbound_envelope."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from agent.simple_responder import AgentResponse
from channel.enums import ChannelType
from channel.inbound import process_inbound_envelope
from channel.messages import MessageEnvelope
from conversation.enums import ConversationStatus, MessageRole


def _envelope(**overrides) -> MessageEnvelope:
    base = dict(
        envelope_id="env_1",
        tenant_id="t1",
        channel_type=ChannelType.FEISHU,
        channel_id="ch1",
        external_user_id="user1",
        external_conversation_id="chat1",
        external_message_id="om_1",
        text="hello",
        received_at=datetime.now(UTC),
    )
    base.update(overrides)
    return MessageEnvelope(**base)


@pytest.mark.asyncio
async def test_process_inbound_creates_conv_and_records_message() -> None:
    """Happy path: envelope -> find_or_create + record_message both called."""
    with patch("channel.inbound.ConversationService") as mock_svc_cls:
        mock_service = mock_svc_cls.return_value
        mock_conv = MagicMock(id="c1", tenant_id="t1")
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=mock_conv)
        mock_service.record_message = AsyncMock()

        await process_inbound_envelope(_envelope())

        mock_service.find_or_create_for_inbound.assert_awaited_once_with(
            tenant_id="t1",
            channel_id="ch1",
            customer_external_id="user1",
        )
        mock_service.record_message.assert_awaited_once_with(
            tenant_id="t1",
            conversation_id="c1",
            role=MessageRole.CUSTOMER,
            content_text="hello",
        )


@pytest.mark.asyncio
async def test_process_inbound_drops_on_cross_tenant() -> None:
    """find_or_create returns None -> log + drop, no record_message."""
    with patch("channel.inbound.ConversationService") as mock_svc_cls:
        mock_service = mock_svc_cls.return_value
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=None)
        mock_service.record_message = AsyncMock()

        await process_inbound_envelope(_envelope())

        mock_service.record_message.assert_not_called()


@pytest.mark.asyncio
async def test_process_inbound_swallows_persistence_error() -> None:
    """DB error is logged but does NOT propagate — webhook still ACKs."""
    with patch("channel.inbound.ConversationService") as mock_svc_cls:
        mock_service = mock_svc_cls.return_value
        mock_service.find_or_create_for_inbound = AsyncMock(
            side_effect=RuntimeError("db down")
        )

        # Must not raise
        await process_inbound_envelope(_envelope())
        mock_service.record_message.assert_not_called()


@pytest.mark.asyncio
async def test_process_inbound_swallows_record_message_error() -> None:
    """An error in record_message is also logged + swallowed."""
    with patch("channel.inbound.ConversationService") as mock_svc_cls:
        mock_service = mock_svc_cls.return_value
        mock_conv = MagicMock(id="c1", tenant_id="t1")
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=mock_conv)
        mock_service.record_message = AsyncMock(side_effect=RuntimeError("write fail"))

        # Must not raise
        await process_inbound_envelope(_envelope())


@pytest.mark.asyncio
async def test_process_inbound_does_not_log_message_content() -> None:
    """The processor must not include message text in any log extra (PII safety)."""
    with patch("channel.inbound.ConversationService") as mock_svc_cls, \
         patch("channel.inbound.logger") as mock_logger:
        mock_service = mock_svc_cls.return_value
        mock_conv = MagicMock(id="c1", tenant_id="t1")
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=mock_conv)
        mock_service.record_message = AsyncMock()

        secret_text = "SECRET_PII_TOKEN_DO_NOT_LOG"  # noqa: S105
        await process_inbound_envelope(_envelope(text=secret_text))

        # Inspect every call's extra dict — none should contain the text.
        for call in mock_logger.info.call_args_list + mock_logger.warning.call_args_list + \
                mock_logger.exception.call_args_list:
            kwargs = call.kwargs or {}
            extra = kwargs.get("extra", {})
            assert secret_text not in str(extra), (
                f"text leaked into log extra: {extra}"
            )


@pytest.mark.asyncio
async def test_process_inbound_triggers_ai_response_when_open_and_ai_handling() -> None:
    """When conv is OPEN + ai_handling, SimpleResponder is called and AI msg recorded."""
    with patch("channel.inbound.ConversationService") as mock_svc_cls, \
         patch("channel.inbound.SimpleResponder") as mock_resp_cls:
        mock_service = mock_svc_cls.return_value
        mock_conv = MagicMock(
            id="c1",
            tenant_id="t1",
            ai_handling=True,
            status=ConversationStatus.OPEN,
        )
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=mock_conv)
        mock_service.record_message = AsyncMock()

        mock_responder = mock_resp_cls.return_value
        mock_responder.respond = AsyncMock(
            return_value=AgentResponse(content_text="AI says hi", role=MessageRole.AI)
        )

        await process_inbound_envelope(_envelope())

        assert mock_responder.respond.await_count == 1
        call_kwargs = mock_responder.respond.await_args.kwargs
        assert call_kwargs["tenant_id"] == "t1"
        assert call_kwargs["conversation_id"] == "c1"
        # A streaming relay callback is wired so message.delta frames flow.
        assert callable(call_kwargs["on_delta"])
        # record_message called twice: customer, then AI.
        assert mock_service.record_message.await_count == 2
        second_call = mock_service.record_message.await_args_list[1]
        assert second_call.kwargs["role"] == MessageRole.AI
        assert second_call.kwargs["content_text"] == "AI says hi"


@pytest.mark.asyncio
async def test_process_inbound_skips_ai_response_when_human_handling() -> None:
    """When ai_handling=False, SimpleResponder must not be called."""
    with patch("channel.inbound.ConversationService") as mock_svc_cls, \
         patch("channel.inbound.SimpleResponder") as mock_resp_cls:
        mock_service = mock_svc_cls.return_value
        mock_conv = MagicMock(
            id="c1",
            tenant_id="t1",
            ai_handling=False,
            status=ConversationStatus.PENDING,
        )
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=mock_conv)
        mock_service.record_message = AsyncMock()

        mock_responder = mock_resp_cls.return_value

        await process_inbound_envelope(_envelope())

        mock_responder.respond.assert_not_called()
        # Only customer message recorded, no AI.
        assert mock_service.record_message.await_count == 1


@pytest.mark.asyncio
async def test_process_inbound_skips_ai_response_when_responder_returns_none() -> None:
    """When SimpleResponder returns None, inbound still completes cleanly."""
    with patch("channel.inbound.ConversationService") as mock_svc_cls, \
         patch("channel.inbound.SimpleResponder") as mock_resp_cls:
        mock_service = mock_svc_cls.return_value
        mock_conv = MagicMock(
            id="c1",
            tenant_id="t1",
            ai_handling=True,
            status=ConversationStatus.OPEN,
        )
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=mock_conv)
        mock_service.record_message = AsyncMock()

        mock_responder = mock_resp_cls.return_value
        mock_responder.respond = AsyncMock(return_value=None)

        await process_inbound_envelope(_envelope())

        # Only customer message recorded.
        assert mock_service.record_message.await_count == 1


@pytest.mark.asyncio
async def test_process_inbound_swallows_ai_responder_exception() -> None:
    """If the AI responder raises, the inbound pipeline must not propagate."""
    with patch("channel.inbound.ConversationService") as mock_svc_cls, \
         patch("channel.inbound.SimpleResponder") as mock_resp_cls:
        mock_service = mock_svc_cls.return_value
        mock_conv = MagicMock(
            id="c1",
            tenant_id="t1",
            ai_handling=True,
            status=ConversationStatus.OPEN,
        )
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=mock_conv)
        mock_service.record_message = AsyncMock()

        mock_responder = mock_resp_cls.return_value
        mock_responder.respond = AsyncMock(side_effect=RuntimeError("boom"))

        # Must not raise.
        await process_inbound_envelope(_envelope())

        # Only the customer message was recorded; AI attempt failed.
        assert mock_service.record_message.await_count == 1


# ---- Task 5.4: WS message.complete push ----


@pytest.mark.asyncio
async def test_process_inbound_broadcasts_message_complete_to_ws() -> None:
    """AI message persistence triggers a message.complete WS broadcast."""
    with patch("channel.inbound.ConversationService") as mock_svc_cls, \
         patch("channel.inbound.SimpleResponder") as mock_resp_cls, \
         patch("channel.inbound._broadcast_ai_complete", new_callable=AsyncMock) as mock_bcast:
        mock_service = mock_svc_cls.return_value
        mock_conv = MagicMock(
            id="c1",
            tenant_id="t1",
            ai_handling=True,
            status=ConversationStatus.OPEN,
        )
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=mock_conv)

        ai_msg = MagicMock(id="m_ai_1", role=MessageRole.AI, content_text="AI reply")
        mock_service.record_message = AsyncMock(side_effect=[MagicMock(), ai_msg])

        mock_responder = mock_resp_cls.return_value
        mock_responder.respond = AsyncMock(
            return_value=AgentResponse(content_text="AI reply", role=MessageRole.AI)
        )

        await process_inbound_envelope(_envelope(channel_id="ch1"))

        mock_bcast.assert_awaited_once_with(
            channel_id="ch1",
            conversation_id="c1",
            message_id="m_ai_1",
            role=MessageRole.AI,
            content="AI reply",
        )


@pytest.mark.asyncio
async def test_process_inbound_broadcast_uses_persisted_message_id() -> None:
    """The pushed message_id must be the persisted row id, not a fresh ULID.

    The frontend dedupes the WS push against a follow-up REST fetch, so the
    two must agree on the id.
    """
    with patch("channel.inbound.ConversationService") as mock_svc_cls, \
         patch("channel.inbound.SimpleResponder") as mock_resp_cls, \
         patch("channel.inbound._broadcast_ai_complete", new_callable=AsyncMock) as mock_bcast:
        mock_service = mock_svc_cls.return_value
        mock_conv = MagicMock(
            id="c1", tenant_id="t1", ai_handling=True, status=ConversationStatus.OPEN
        )
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=mock_conv)

        persisted = MagicMock(id="PERSISTED_ROW_ID", role=MessageRole.AI)
        mock_service.record_message = AsyncMock(side_effect=[MagicMock(), persisted])

        mock_responder = mock_resp_cls.return_value
        mock_responder.respond = AsyncMock(
            return_value=AgentResponse(content_text="x", role=MessageRole.AI)
        )

        await process_inbound_envelope(_envelope())

        assert mock_bcast.await_args.kwargs["message_id"] == "PERSISTED_ROW_ID"


@pytest.mark.asyncio
async def test_process_inbound_streams_message_delta_via_relay_callback() -> None:
    """The on_delta callback handed to SimpleResponder must fan each chunk
    out as a message.delta broadcast (keyed by conversation_id, no row id yet).
    """
    with patch("channel.inbound.ConversationService") as mock_svc_cls, \
         patch("channel.inbound.SimpleResponder") as mock_resp_cls, \
         patch("channel.inbound._broadcast_ai_delta", new_callable=AsyncMock) as mock_delta:
        mock_service = mock_svc_cls.return_value
        mock_conv = MagicMock(
            id="c1",
            tenant_id="t1",
            channel_id="ch1",
            ai_handling=True,
            status=ConversationStatus.OPEN,
        )
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=mock_conv)
        ai_msg = MagicMock(id="m_ai_1", role=MessageRole.AI, content_text="Hel lo")
        mock_service.record_message = AsyncMock(side_effect=[MagicMock(), ai_msg])

        mock_responder = mock_resp_cls.return_value
        mock_responder.respond = AsyncMock(
            return_value=AgentResponse(content_text="Hel lo", role=MessageRole.AI)
        )

        await process_inbound_envelope(_envelope(channel_id="ch1"))

        # Response was invoked with a relay callback that fans deltas out.
        on_delta = mock_responder.respond.await_args.kwargs["on_delta"]
        await on_delta("Hel")
        await on_delta(" lo")

        # Each chunk is fanned out as its own message.delta broadcast.
        mock_delta.assert_has_awaits(
            [
                call(channel_id="ch1", conversation_id="c1", text="Hel"),
                call(channel_id="ch1", conversation_id="c1", text=" lo"),
            ]
        )
        assert mock_delta.await_count == 2


@pytest.mark.asyncio
async def test_process_inbound_skips_broadcast_when_no_ai_response() -> None:
    """If AI returns None (e.g. conversation not ai_handling), no broadcast."""
    with patch("channel.inbound.ConversationService") as mock_svc_cls, \
         patch("channel.inbound._broadcast_ai_complete", new_callable=AsyncMock) as mock_bcast:
        mock_service = mock_svc_cls.return_value
        mock_conv = MagicMock(
            id="c1",
            tenant_id="t1",
            ai_handling=False,
            status=ConversationStatus.PENDING,
        )
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=mock_conv)
        mock_service.record_message = AsyncMock()

        await process_inbound_envelope(_envelope())

        mock_bcast.assert_not_called()


@pytest.mark.asyncio
async def test_process_inbound_skips_broadcast_on_cross_tenant_probe() -> None:
    """A blocked cross-tenant probe must not emit anything onto the wire."""
    with patch("channel.inbound.ConversationService") as mock_svc_cls, \
         patch("channel.inbound._broadcast_ai_complete", new_callable=AsyncMock) as mock_bcast:
        mock_service = mock_svc_cls.return_value
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=None)
        mock_service.record_message = AsyncMock()

        await process_inbound_envelope(_envelope())

        mock_bcast.assert_not_called()


@pytest.mark.asyncio
async def test_process_inbound_swallows_broadcast_failure() -> None:
    """WS broadcast failure does not break the inbound path."""
    with patch("channel.inbound.ConversationService") as mock_svc_cls, \
         patch("channel.inbound.SimpleResponder") as mock_resp_cls, \
         patch("channel.inbound._broadcast_ai_complete", new_callable=AsyncMock) as mock_bcast:
        mock_service = mock_svc_cls.return_value
        mock_conv = MagicMock(
            id="c1",
            tenant_id="t1",
            ai_handling=True,
            status=ConversationStatus.OPEN,
        )
        mock_service.find_or_create_for_inbound = AsyncMock(return_value=mock_conv)

        ai_msg = MagicMock(id="m_ai_1", role=MessageRole.AI, content_text="reply")
        mock_service.record_message = AsyncMock(side_effect=[MagicMock(), ai_msg])

        mock_responder = mock_resp_cls.return_value
        mock_responder.respond = AsyncMock(
            return_value=AgentResponse(content_text="reply", role=MessageRole.AI)
        )
        mock_bcast.side_effect = RuntimeError("WS down")

        # Must not raise — the AI message is already durably persisted.
        await process_inbound_envelope(_envelope())

        assert mock_service.record_message.await_count == 2


# ---- Task 11 / nitpick P2: async factory with one-session-per-call ----


class _SessionCtx:
    """Mock async-context-manager returned by the patched sessionmaker.

    Matches the ``async with sm() as session`` usage shape so the
    factory under test can drive its session lifecycle through the
    normal ``__aenter__`` / ``__aexit__`` protocol.
    """

    def __init__(self, session: object) -> None:
        self._s = session

    def __call__(self) -> _SessionCtx:
        # ``sm()`` must return a context manager; tests invoke it
        # directly so this returns self.
        return self

    async def __aenter__(self) -> object:
        return self._s

    async def __aexit__(self, *args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_inbound_factory_builds_service_from_session() -> None:
    """The TicketService factory is sync and accepts a session parameter.

    Post-bug-fix the factory no longer manages session lifecycle — it
    simply constructs a ``TicketService`` from a caller-provided
    ``AsyncSession``. The caller (``_try_auto_create_ticket``) owns
    the session via ``core.database.get_session()``, so each inbound
    customer message gets a dedicated short-lived session with proper
    commit/rollback semantics (Task 11 / nitpick P2 invariant preserved
    — session lifetime stays bounded to one customer message).

    Three assertions:

    1. ``not asyncio.iscoroutinefunction(factory)`` — factory is sync;
       callers must NOT ``await`` it (an unawaited sync call is fine,
       but awaiting sync was the latent bug surface that hid the
       missing commit). The session is opened by the caller, not here.
    2. ``factory(session)`` returns a real ``TicketService`` whose
       repository points at the caller-provided session (no hidden
       session creation).
    3. The factory does not touch ``core.database.get_sessionmaker``
       — session lifecycle is the caller's responsibility now.
    """
    from channel.inbound import _build_ticket_service_factory
    from ticket.service import TicketService

    factory = _build_ticket_service_factory()

    # 1. Factory is SYNC — the session is owned by the caller.
    assert not asyncio.iscoroutinefunction(factory), (
        "TicketService factory must be sync post-fix; callers now own "
        "session lifecycle via core.database.get_session() and "
        "factory() is not awaited."
    )

    # 2. Invocation must yield a TicketService backed by the passed session.
    session = MagicMock(name="session")
    svc = factory(session)

    assert isinstance(svc, TicketService)
    # Service is backed by the caller-provided session — no hidden
    # session creation means the caller's ``get_session()`` commit
    # actually persists the ticket.
    assert svc.repo.session is session
    # 3. Factory must not touch the sessionmaker — session comes from
    # the caller. (If it did, the caller's commit would not commit the
    # factory's session, and the bug would still be there.)
    with patch("core.database.get_sessionmaker") as mock_sm:
        factory(MagicMock(name="other_session"))
        mock_sm.assert_not_called()


@pytest.mark.asyncio
async def test_inbound_factory_opens_fresh_session_across_calls() -> None:
    """Two factory invocations with two distinct sessions must yield
    two distinct ``TicketService`` instances backed by those sessions.

    Post-fix the factory is sync and accepts the session as a
    parameter (Task 11's "one session per call" invariant is now
    enforced at the *caller* side via ``core.database.get_session()``).
    This test pins that the factory doesn't accidentally cache a
    session across invocations — passing ``session_a`` yields a
    service backed by ``session_a``, and ``session_b`` yields a
    service backed by ``session_b``.
    """
    from channel.inbound import _build_ticket_service_factory
    from ticket.service import TicketService

    factory = _build_ticket_service_factory()

    session_a = MagicMock(name="session_a")
    session_b = MagicMock(name="session_b")

    svc_a = factory(session_a)
    svc_b = factory(session_b)

    assert isinstance(svc_a, TicketService)
    assert isinstance(svc_b, TicketService)
    # Different sessions -> different services (identity check is
    # robust to changes in TicketService.__eq__ semantics).
    assert svc_a is not svc_b
    # Each service carries the session it was handed.
    assert svc_a.repo.session is session_a
    assert svc_b.repo.session is session_b
    # The factory is sync (not a coroutine function); sessions are
    # owned by the caller, not the factory.
    assert not asyncio.iscoroutinefunction(factory)


# ---- Pre-existing-bug regression: auto-create-ticket must commit ----
#
# The factory historically opened an ``AsyncSession`` without ``async
# with``, so ``TicketService.create()`` flushed but the transaction
# was rolled back when the session was GC'd. M2.A's auto-create-ticket
# feature was silently broken — customer tickets never persisted in
# production. The fix moved session ownership to the caller
# (``_try_auto_create_ticket``) which wraps the work in
# ``core.database.get_session()`` — the existing helper that commits
# on clean exit and rolls back on exception.


@pytest.mark.asyncio
async def test_ticket_auto_create_session_committed_via_get_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: M2.A's auto-create-ticket must persist.

    When the inbound customer-message hook (``_try_auto_create_ticket``)
    runs the ``ticket_service_factory``, the session must go through
    proper lifecycle — ``__aenter__`` + ``__aexit__`` via
    ``core.database.get_session()``, with ``session.commit()`` invoked
    on the clean path. Without this, the ``TicketRepository.create``
    flush silently rolls back at GC and the customer never gets a
    ticket in production.

    Pins BOTH:

    * ``get_session()`` is invoked (the caller is wrapping the work).
    * The yielded session's ``commit()`` is awaited.

    If a future refactor drops the ``async with get_session()``
    wrapping at the caller side, this test fails.
    """
    from contextlib import asynccontextmanager

    from sqlalchemy.ext.asyncio import AsyncSession

    from conversation import service as conv_service_module

    mock_session = MagicMock(spec=AsyncSession)
    mock_session.commit = AsyncMock()
    mock_session.rollback = AsyncMock()

    # Track session lifecycle so we can assert the proper protocol was
    # followed even if commit() is async-mocked.
    lifecycle_calls: list[str] = []

    @asynccontextmanager
    async def fake_get_session() -> object:
        lifecycle_calls.append("aenter")
        try:
            yield mock_session
            await mock_session.commit()
            lifecycle_calls.append("commit")
        except Exception:
            await mock_session.rollback()
            lifecycle_calls.append("rollback")
            raise
        finally:
            lifecycle_calls.append("aexit")

    # Patch get_session where conversation.service reads it.
    monkeypatch.setattr(conv_service_module, "get_session", fake_get_session)
    # ``get_session()`` historically used ``get_sessionmaker()``; keep
    # both attrs patched so the legacy import path (if any is left in
    # the module) stays benign.
    monkeypatch.setattr(
        conv_service_module, "get_sessionmaker", lambda: MagicMock()
    )

    # Custom sync factory — accepts the session and returns the fake
    # service so we can assert the auto-create work ran. (We don't use
    # ``_build_ticket_service_factory`` here because that builds a
    # real ``TicketService`` against the (mocked) session.)
    fake_svc = MagicMock()
    fake_svc.repo.get_for_conversation = AsyncMock(return_value=None)
    fake_svc.create = AsyncMock()

    def factory(session: object) -> MagicMock:
        return fake_svc

    await conv_service_module._try_auto_create_ticket(
        factory=factory,  # type: ignore[arg-type]
        tenant_id="t1",
        conversation_id="c1",
        content_text="I need help with my account",
    )

    # Auto-create work actually ran.
    fake_svc.repo.get_for_conversation.assert_awaited_once_with(
        "c1", tenant_id="t1"
    )
    fake_svc.create.assert_awaited_once()

    # Session went through the proper lifecycle via get_session().
    assert "aenter" in lifecycle_calls, (
        "expected the session to enter get_session(); "
        "caller likely dropped the async with get_session() wrap"
    )
    assert "commit" in lifecycle_calls, (
        "expected session.commit() to be invoked on the clean path; "
        "ticket create silently rolled back"
    )
    mock_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_ticket_auto_create_rolls_back_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the auto-create work raises, ``get_session()`` must roll back.

    The bug fix uses ``core.database.get_session()`` which calls
    ``session.rollback()`` on the exception path. Without this, a
    partial flush would be left dangling on the connection.

    Companion to ``test_ticket_auto_create_session_committed_via_get_session``
    — pins BOTH the success AND the failure path of the same lifecycle.
    """
    from contextlib import asynccontextmanager

    from sqlalchemy.ext.asyncio import AsyncSession

    from conversation import service as conv_service_module

    mock_session = MagicMock(spec=AsyncSession)
    mock_session.commit = AsyncMock(side_effect=RuntimeError("commit boom"))
    mock_session.rollback = AsyncMock()

    rollback_invoked = False

    @asynccontextmanager
    async def fake_get_session() -> object:
        try:
            yield mock_session
            await mock_session.commit()
        except Exception:
            nonlocal rollback_invoked
            rollback_invoked = True
            await mock_session.rollback()
            raise

    monkeypatch.setattr(conv_service_module, "get_session", fake_get_session)
    monkeypatch.setattr(
        conv_service_module, "get_sessionmaker", lambda: MagicMock()
    )

    # Custom sync factory — auto-create work raises to exercise the
    # rollback path.
    fake_svc = MagicMock()
    fake_svc.repo.get_for_conversation = AsyncMock(return_value=None)
    fake_svc.create = AsyncMock(side_effect=RuntimeError("FK violation"))

    def factory(session: object) -> MagicMock:
        return fake_svc

    # Should NOT propagate (the caller swallows and logs WARNING).
    await conv_service_module._try_auto_create_ticket(
        factory=factory,  # type: ignore[arg-type]
        tenant_id="t1",
        conversation_id="c1",
        content_text="msg",
    )

    assert rollback_invoked, (
        "expected get_session() to invoke session.rollback() on the "
        "exception path; the half-flushed ticket was left dangling"
    )
