"""Outbox dispatcher — drains the outbox table.

The dispatcher is called by an arq task (or a periodic HTTP trigger in dev).
It picks up pending events whose `next_attempt_at` is null or in the past,
calls the registered handler, and updates the row.

Handlers are looked up by `event.type` from a HandlerRegistry. This makes
it easy to add new event types without changing the dispatcher.
"""
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from channel.models import OutboxEvent
from core.database import get_sessionmaker

logger = logging.getLogger(__name__)

# Type alias for a handler: async callable taking the payload dict, returning None
EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class HandlerRegistry:
    """Maps event.type strings to async handler callables."""

    def __init__(self) -> None:
        self._handlers: dict[str, EventHandler] = {}

    def register(self, event_type: str, handler: EventHandler) -> None:
        """Register a handler for an event type. Overwrites if already registered."""
        self._handlers[event_type] = handler

    def get(self, event_type: str) -> EventHandler | None:
        return self._handlers.get(event_type)

    def types(self) -> list[str]:
        return list(self._handlers.keys())


class OutboxDispatcher:
    """Drains the outbox by dispatching each pending event to its handler.

    Retry policy: exponential backoff with jitter, capped at 1 hour.
    After `max_attempts` failures, the event is marked 'failed' and skipped.
    """

    def __init__(
        self,
        *,
        registry: HandlerRegistry,
        max_attempts: int = 5,
    ) -> None:
        self.registry = registry
        self.max_attempts = max_attempts

    async def dispatch_one(self, event_id: str) -> bool:
        """Dispatch a single event by id. Returns True if the event is now 'done'.

        Idempotency: a 'done' event returns True without calling the handler again.
        A 'failed' event returns True (no retry possible).
        An event not yet due (next_attempt_at in the future) returns False.
        """
        sm = get_sessionmaker()
        async with sm() as session:
            event = await session.get(OutboxEvent, event_id)
            if event is None:
                logger.warning("outbox.event_not_found", extra={"event_id": event_id})
                return True  # treat as "nothing to do"
            if event.status == "done":
                return True
            if event.status == "failed":
                return True
            now = datetime.now(UTC)
            if event.next_attempt_at is not None and event.next_attempt_at > now:
                return False  # not due yet

            handler = self.registry.get(event.type)
            if handler is None:
                logger.warning(
                    "outbox.no_handler",
                    extra={"event_id": event_id, "event_type": event.type},
                )
                return False  # leave pending; don't fail it (someone may register later)

            # Attempt the handler
            try:
                await handler(dict(event.payload_json))
            except Exception as exc:
                event.attempts += 1
                event.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                if event.attempts >= self.max_attempts:
                    event.status = "failed"
                    logger.error(
                        "outbox.handler_exhausted",
                        extra={
                            "event_id": event_id,
                            "event_type": event.type,
                            "attempts": event.attempts,
                        },
                    )
                else:
                    # Exponential backoff: 2^attempts seconds, capped at 1h
                    backoff_seconds = min(2 ** event.attempts, 3600)
                    event.next_attempt_at = now + timedelta(seconds=backoff_seconds)
                    logger.warning(
                        "outbox.handler_failed_will_retry",
                        extra={
                            "event_id": event_id,
                            "event_type": event.type,
                            "attempts": event.attempts,
                            "next_attempt_in_seconds": backoff_seconds,
                        },
                    )
                await session.commit()
                return False
            else:
                event.status = "done"
                event.processed_at = now
                event.attempts += 1
                await session.commit()
                logger.info(
                    "outbox.handler_succeeded",
                    extra={"event_id": event_id, "event_type": event.type},
                )
                return True

    async def dispatch_batch(self, limit: int = 100) -> int:
        """Dispatch up to `limit` due events. Returns the number successfully dispatched.

        Picks events with status='pending' AND
        (next_attempt_at IS NULL OR next_attempt_at <= now()).
        """
        sm = get_sessionmaker()
        async with sm() as session:
            now = datetime.now(UTC)
            due_or_immediate = (OutboxEvent.next_attempt_at.is_(None)) | (
                OutboxEvent.next_attempt_at <= now
            )
            stmt = (
                select(OutboxEvent.id)
                .where(OutboxEvent.status == "pending")
                .where(due_or_immediate)
                .order_by(OutboxEvent.created_at)
                .limit(limit)
            )
            result = await session.execute(stmt)
            event_ids = [row[0] for row in result.all()]

        dispatched = 0
        for event_id in event_ids:
            if await self.dispatch_one(event_id):
                dispatched += 1
        return dispatched


# Convenience: arq task wrapper. The arq worker can register this as a periodic task.
async def outbox_periodic_task(ctx: dict[str, Any]) -> int:
    """arq task entry point — drains up to 100 events per call.

    `ctx` is provided by arq and may contain a registered dispatcher; if not,
    we use a default one with no handlers (events with no handler will be left
    pending — fine for tests; production should pre-register handlers).
    """
    dispatcher: OutboxDispatcher | None = ctx.get("dispatcher")
    if dispatcher is None:
        return 0
    return await dispatcher.dispatch_batch(limit=100)
