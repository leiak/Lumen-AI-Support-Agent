"""Tests for the OutboxEvent ORM model. Integration test requires live DB."""
import pytest
from sqlalchemy import select

from channel.models import OutboxEvent
from core.database import get_session
from core.id_gen import new_id


@pytest.mark.integration
async def test_outbox_event_roundtrip() -> None:
    """Create an OutboxEvent, commit, re-query, verify fields persisted."""
    tenant_id = new_id()
    async with get_session() as session:
        event = OutboxEvent(
            id=new_id(),
            tenant_id=tenant_id,
            type="channel.send_message",
            payload_json={"channel_id": "ch1", "text": "hello"},
            status="pending",
        )
        session.add(event)
        await session.flush()
        await session.commit()

    try:
        async with get_session() as session:
            result = await session.execute(
                select(OutboxEvent).where(OutboxEvent.tenant_id == tenant_id)
            )
            loaded = result.scalar_one()
            assert loaded.type == "channel.send_message"
            assert loaded.payload_json == {"channel_id": "ch1", "text": "hello"}
            assert loaded.status == "pending"
            assert loaded.attempts == 0
            assert loaded.last_error is None
            assert loaded.processed_at is None
            assert loaded.created_at is not None
    finally:
        # Cleanup
        async with get_session() as session:
            r = await session.execute(
                select(OutboxEvent).where(OutboxEvent.tenant_id == tenant_id)
            )
            ev = r.scalar_one_or_none()
            if ev is not None:
                await session.delete(ev)
                await session.commit()


def test_outbox_event_table_name() -> None:
    """Pure-Python test (no DB): verify model is importable and table is named correctly."""
    from channel.models import OutboxEvent

    assert OutboxEvent.__tablename__ == "outbox_events"
    # Verify expected columns exist
    column_names = {c.name for c in OutboxEvent.__table__.columns}
    assert {
        "id",
        "tenant_id",
        "type",
        "payload_json",
        "status",
        "attempts",
        "last_error",
        "next_attempt_at",
        "created_at",
        "processed_at",
    } <= column_names