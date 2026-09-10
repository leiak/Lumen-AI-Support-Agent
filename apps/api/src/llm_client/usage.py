"""Batched LLM usage recording.

Successful LLM calls are enqueued in memory and flushed in batches to
the `llm_usage` table to reduce DB pressure under load.
"""
import asyncio
from typing import Any

from sqlalchemy import insert

from core.database import get_sessionmaker
from core.id_gen import new_id
from llm_client.models import LLMUsage

# Approximate USD cost per 1K tokens (input, output). Real billing comes from
# provider invoices — this is for budget tracking only.
_COST_PER_1K: dict[str, tuple[float, float]] = {
    "claude-3-5-sonnet-20241022": (0.003, 0.015),
    "claude-3-5-haiku-20241022": (0.0008, 0.004),
    "gpt-4o": (0.005, 0.015),
    "gpt-4o-mini": (0.00015, 0.0006),
}


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Return estimated USD cost for the given model and token counts."""
    in_cost, out_cost = _COST_PER_1K.get(model, (0.0, 0.0))
    return (prompt_tokens / 1000.0) * in_cost + (completion_tokens / 1000.0) * out_cost


class UsageRecorder:
    """In-memory queue of usage rows; flush() drains the queue to the DB."""

    def __init__(self) -> None:
        self._pending: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    def enqueue(
        self,
        *,
        tenant_id: str,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        request_id: str,
        cached: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Append a usage row to the in-memory queue."""
        cost = estimate_cost_usd(model, prompt_tokens, completion_tokens)
        self._pending.append(
            {
                "id": new_id(),
                "tenant_id": tenant_id,
                "provider": provider,
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_usd": cost,
                "request_id": request_id,
                "cached": cached,
                "metadata_json": metadata or {},
            }
        )

    async def flush(self) -> None:
        """Drain the queue and bulk-insert all rows into llm_usage. No-op if empty."""
        async with self._lock:
            if not self._pending:
                return
            batch = self._pending[:]
            self._pending.clear()
        sm = get_sessionmaker()
        async with sm() as session:
            await session.execute(insert(LLMUsage), batch)
            await session.commit()
