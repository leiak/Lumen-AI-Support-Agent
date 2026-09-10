"""Tests for the OutboxDispatcher. Integration tests use a fake handler."""
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from channel.models import OutboxEvent
from channel.outbox.dispatcher import HandlerRegistry, OutboxDispatcher
from core.database import get_session
from core.id_gen import new_id


class FakeHandler:
    """Handler that succeeds on call_count=0, fails twice, then succeeds."""

    def __init__(self) -> None:
        self.call_count = 0
        self.calls: list[dict] = []

    async def __call__(self, payload: dict) -> None:
        self.calls.append(payload)
        self.call_count += 1
        if self.call_count <= 2:
            raise RuntimeError(f"simulated failure #{self.call_count}")


async def _create_event(
    *,
    tenant_id: str,
    type: str,
    payload: dict,
    status: str = "pending",
) -> str:
    """Insert a new OutboxEvent and return its id."""
    event_id = new_id()
    async with get_session() as session:
        event = OutboxEvent(
            id=event_id,
            tenant_id=tenant_id,
            type=type,
            payload_json=payload,
            status=status,
        )
        session.add(event)
        await session.commit()
    return event_id


async def _get_event(event_id: str) -> OutboxEvent:
    async with get_session() as session:
        stmt = select(OutboxEvent).where(OutboxEvent.id == event_id)
        result = await session.execute(stmt)
        return result.scalar_one()


async def _delete_event(event_id: str) -> None:
    async with get_session() as session:
        ev = await session.get(OutboxEvent, event_id)
        if ev:
            await session.delete(ev)
            await session.commit()


@pytest.mark.integration
async def test_dispatch_success_marks_done() -> None:
    tenant_id = new_id()
    payload = {"text": "hello"}
    handler = FakeHandler()
    handler.call_count = 99  # bypass the failure path
    registry = HandlerRegistry()
    registry.register("test.event", handler)
    dispatcher = OutboxDispatcher(registry=registry, max_attempts=5)

    event_id = await _create_event(tenant_id=tenant_id, type="test.event", payload=payload)
    try:
        processed = await dispatcher.dispatch_one(event_id)
        assert processed is True
        ev = await _get_event(event_id)
        assert ev.status == "done"
        assert ev.processed_at is not None
        assert ev.attempts == 1
    finally:
        await _delete_event(event_id)


@pytest.mark.integration
async def test_dispatch_failure_increments_attempts_and_schedules_retry() -> None:
    tenant_id = new_id()
    handler = FakeHandler()  # first 2 calls fail
    registry = HandlerRegistry()
    registry.register("test.event", handler)
    dispatcher = OutboxDispatcher(registry=registry, max_attempts=5)

    event_id = await _create_event(tenant_id=tenant_id, type="test.event", payload={})
    try:
        processed = await dispatcher.dispatch_one(event_id)
        assert processed is False  # not done yet
        ev = await _get_event(event_id)
        assert ev.status == "pending"
        assert ev.attempts == 1
        assert ev.last_error is not None
        assert "simulated failure" in ev.last_error
        assert ev.next_attempt_at is not None
        assert ev.next_attempt_at > datetime.now(UTC)
    finally:
        await _delete_event(event_id)


@pytest.mark.integration
async def test_dispatch_marks_failed_after_max_attempts() -> None:
    tenant_id = new_id()

    async def always_fails(payload: dict) -> None:
        raise RuntimeError("permanent failure")

    registry = HandlerRegistry()
    registry.register("test.event", always_fails)
    dispatcher = OutboxDispatcher(registry=registry, max_attempts=3)

    event_id = await _create_event(tenant_id=tenant_id, type="test.event", payload={})
    try:
        # Dispatch max_attempts times. Reset next_attempt_at between calls to
        # simulate time passing (in production, the scheduler waits for backoff).
        for _ in range(3):
            async with get_session() as session:
                ev = await session.get(OutboxEvent, event_id)
                if ev is not None:
                    ev.next_attempt_at = None
                    await session.commit()
            await dispatcher.dispatch_one(event_id)

        ev = await _get_event(event_id)
        assert ev.status == "failed"
        assert ev.attempts == 3
        assert "permanent failure" in (ev.last_error or "")
    finally:
        await _delete_event(event_id)


@pytest.mark.integration
async def test_dispatch_skips_event_with_no_handler() -> None:
    """An event with no registered handler is left in pending status (no crash)."""
    tenant_id = new_id()
    registry = HandlerRegistry()
    dispatcher = OutboxDispatcher(registry=registry, max_attempts=3)

    event_id = await _create_event(tenant_id=tenant_id, type="unregistered.event", payload={})
    try:
        processed = await dispatcher.dispatch_one(event_id)
        assert processed is False
        ev = await _get_event(event_id)
        assert ev.status == "pending"
        # No last_error set when no handler is found (intentional)
        assert ev.last_error is None
    finally:
        await _delete_event(event_id)


@pytest.mark.integration
async def test_dispatch_skips_event_not_due_yet() -> None:
    """Event with future next_attempt_at is skipped."""
    tenant_id = new_id()
    handler = FakeHandler()
    handler.call_count = 99
    registry = HandlerRegistry()
    registry.register("test.event", handler)
    dispatcher = OutboxDispatcher(registry=registry, max_attempts=3)

    # Create an event scheduled for 1 hour in the future
    event_id = new_id()
    from datetime import timedelta
    future = datetime.now(UTC) + timedelta(hours=1)
    async with get_session() as session:
        event = OutboxEvent(
            id=event_id,
            tenant_id=tenant_id,
            type="test.event",
            payload_json={},
            status="pending",
            next_attempt_at=future,
        )
        session.add(event)
        await session.commit()
    try:
        processed = await dispatcher.dispatch_one(event_id)
        assert processed is False
        # Handler was not called
        assert handler.call_count == 99  # still 99, never invoked
    finally:
        await _delete_event(event_id)


def test_handler_registry_register_and_get() -> None:
    """Pure-Python test: registry stores handlers and looks them up."""
    async def h1(payload: dict) -> None:
        pass

    registry = HandlerRegistry()
    registry.register("type.a", h1)
    assert registry.get("type.a") is h1
    assert registry.get("type.b") is None


def test_handler_registry_list_types() -> None:
    async def h1(payload: dict) -> None:
        pass
    async def h2(payload: dict) -> None:
        pass

    registry = HandlerRegistry()
    registry.register("a", h1)
    registry.register("b", h2)
    assert sorted(registry.types()) == ["a", "b"]
