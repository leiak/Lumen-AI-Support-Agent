"""Idempotent Pack B budget/credit/usage seed.

Run AFTER ``tests/e2e/scripts/seed.py`` — assumes the DEMO_TENANT_ID
tenant + DEMO_ADMIN_USER_ID + DEMO_AGENT_USER_ID users already exist
from the base seed. Adds three more concerns:

1. ``tenant_budgets`` row — soft_warn_tokens=10_000, hard_cap_tokens=20_000,
   period_anchor_tz="UTC". Makes the tenant budget-enforced (Pack A).
3. ``tenant_budget_credits`` rows — 2 grants (5_000 + 3_000 tokens) for
   the current UTC month, granting 8_000 extra tokens of effective cap
   (Pack B #2).
5. ``llm_usage`` rows — 5 rows across ``(openai, gpt-4o-mini)`` and
   ``(anthropic, claude-haiku-4-5)`` for the current UTC period, so
   ``?breakdown=true`` returns >1 group (Pack B #5).

Idempotency
-----------
Each seeded budget row is keyed on (tenant_id) — the unique constraint
in ``tenant_budgets`` triggers an upsert path: the script looks up the
existing row first, only inserts when missing, and the credit seed
de-duplicates by (tenant_id, period, granted_by, tokens, note) tuple
so a re-run is a no-op.

Usage (from apps/api, with the existing .env)::

    cd apps/api && uv run python ../../tests/e2e/scripts/seed_budget.py

Lives under ``tests/e2e/`` only — does NOT touch apps/api source.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

# Anchor ``apps/api/src`` onto sys.path. Same helper as ``seed.py`` —
# keeps the script runnable from any cwd as long as the file lives at
# ``<repo>/tests/e2e/scripts/seed_budget.py``.
def _find_repo_root(start: Path) -> Path:
    cur = start.resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / "apps" / "api" / "src").is_dir() and (
            candidate / "tests" / "e2e"
        ).is_dir():
            return candidate
    raise RuntimeError(
        f"could not locate repo root from {start} — "
        "expected to find apps/api/src and tests/e2e as siblings",
    )


_API_SRC = _find_repo_root(Path(__file__)) / "apps" / "api" / "src"
if str(_API_SRC) not in sys.path:
    sys.path.insert(0, str(_API_SRC))

from budget.models import TenantBudget, TenantBudgetCredit  # noqa: E402
from core.database import get_session, reset_engine, reset_sessionmaker  # noqa: E402
from core.id_gen import new_id  # noqa: E402
from llm_client.models import LLMUsage  # noqa: E402
from tenant.repository import TenantRepository  # noqa: E402

# Must match seed.py.
DEMO_TENANT_ID = "01HZDEMO00000000000000000"
DEMO_SUPER_ADMIN_ID = "01HZDEMO0000000000000000F"  # synthetic super-admin user

# Budget config — sized for easy triggering in the demo: a single
# large chat can blow past soft_warn, and ~30k of credit grants
# leaves effective_cap=28_000 — past the credit window so a couple of
# 1500-token requests drive both Pack B #2 (credit-aware cap) and the
# post-mortem 429 gate logic.
DEMO_HARD_CAP_TOKENS = 20_000
DEMO_SOFT_WARN_TOKENS = 10_000
DEMO_CREDITS = [
    # (tokens, note, granted_by)
    (5_000, "Demo seed — initial top-up", DEMO_SUPER_ADMIN_ID),
    (3_000, "Demo seed — follow-up top-up", DEMO_SUPER_ADMIN_ID),
]

# Per-model llm_usage rows: openai/gpt-4o-mini × 3 + anthropic/claude-haiku-4-5 × 2
# — enough for the breakdown endpoint to return 2 groups.
DEMO_USAGE_ROWS: list[dict[str, object]] = [
    {"provider": "openai", "model": "gpt-4o-mini", "prompt": 800, "completion": 400},
    {"provider": "openai", "model": "gpt-4o-mini", "prompt": 1200, "completion": 600},
    {"provider": "openai", "model": "gpt-4o-mini", "prompt": 500, "completion": 250},
    {"provider": "anthropic", "model": "claude-haiku-4-5", "prompt": 600, "completion": 300},
    {"provider": "anthropic", "model": "claude-haiku-4-5", "prompt": 400, "completion": 200},
]


async def _seed_budget(session) -> None:
    """Insert the demo tenant_budgets row, or no-op if present.

    Idempotency key: ``tenant_id`` (the unique constraint on the table).
    We look up by tenant_id instead of caching a fixed id because the
    ``id`` column is VARCHAR(26) and tenant_id is also VARCHAR(26) —
    reusing tenant_id as the budget id keeps the seed trivial to reason
    about without burning a fresh ULID.
    """
    tenant = await TenantRepository().get_by_id(DEMO_TENANT_ID)
    if tenant is None:
        raise RuntimeError(
            f"tenant {DEMO_TENANT_ID} missing — run tests/e2e/scripts/seed.py first"
        )
    from sqlalchemy import select

    existing = (
        await session.execute(
            select(TenantBudget).where(TenantBudget.tenant_id == DEMO_TENANT_ID)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    session.add(
        TenantBudget(
            id=DEMO_TENANT_ID,  # VARCHAR(26) — fits since tenant_id is also 26 chars
            tenant_id=DEMO_TENANT_ID,
            soft_warn_tokens=DEMO_SOFT_WARN_TOKENS,
            hard_cap_tokens=DEMO_HARD_CAP_TOKENS,
            period_anchor_tz="UTC",
        )
    )


async def _seed_credits(session, *, period: str) -> int:
    """Insert demo credit rows. Idempotent: dedupes on
    (tenant_id, period, tokens, granted_by, note).

    Returns the number of rows actually inserted.
    """
    from sqlalchemy import select

    inserted = 0
    for tokens, note, granted_by in DEMO_CREDITS:
        result = await session.execute(
            select(TenantBudgetCredit).where(
                TenantBudgetCredit.tenant_id == DEMO_TENANT_ID,
                TenantBudgetCredit.period == period,
                TenantBudgetCredit.tokens == tokens,
                TenantBudgetCredit.note == note,
                TenantBudgetCredit.granted_by == granted_by,
            )
        )
        if result.scalar_one_or_none() is not None:
            continue
        session.add(
            TenantBudgetCredit(
                id=new_id(),
                tenant_id=DEMO_TENANT_ID,
                period=period,
                tokens=tokens,
                note=note,
                granted_by=granted_by,
            )
        )
        inserted += 1
    return inserted


async def _seed_usage(session, *, now: datetime) -> int:
    """Insert demo llm_usage rows. Idempotent: dedupes on
    (tenant_id, provider, model, prompt_tokens, request_id_prefix).

    Returns the number of rows actually inserted.

    ``now`` is offset-aware UTC; ``LLMUsage.created_at`` is
    ``TIMESTAMP WITHOUT TIME ZONE`` (no ``DateTime(timezone=True)``
    on the column — see llm_client.models.LLMUsage), so we strip the
    tzinfo before INSERT to keep asyncpg happy.
    """
    from sqlalchemy import select

    inserted = 0
    naive_now = now.replace(tzinfo=None)  # strip tz for TIMESTAMP WITHOUT TIME ZONE
    for i, row in enumerate(DEMO_USAGE_ROWS):
        rid = f"seed-budget-{row['provider']}-{i}"
        result = await session.execute(
            select(LLMUsage).where(
                LLMUsage.tenant_id == DEMO_TENANT_ID,
                LLMUsage.request_id == rid,
            )
        )
        if result.scalar_one_or_none() is not None:
            continue
        session.add(
            LLMUsage(
                id=new_id(),
                tenant_id=DEMO_TENANT_ID,
                provider=str(row["provider"]),
                model=str(row["model"]),
                prompt_tokens=int(row["prompt"]),  # type: ignore[arg-type]
                completion_tokens=int(row["completion"]),  # type: ignore[arg-type]
                cost_usd=0.0,
                request_id=rid,
                cached=False,
                metadata_json={"source": "seed-budget"},
                created_at=naive_now,
            )
        )
        inserted += 1
    return inserted


def _current_period(tz_name: str = "UTC") -> tuple[str, datetime]:
    """Return (period, period_start) — matches ``budget.resolver._current_period``."""
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name)
    except Exception:
        from datetime import timezone

        tz = timezone.utc
    now = datetime.now(tz)
    period = now.strftime("%Y-%m")
    period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return period, period_start


async def main() -> None:
    """Run all three seeds inside a single async session, commit once."""
    period, _ = _current_period("UTC")
    now = datetime.now(UTC)
    try:
        async with get_session() as session:
            await _seed_budget(session)
            n_credits = await _seed_credits(session, period=period)
            n_usage = await _seed_usage(session, now=now)
            await session.commit()
    finally:
        reset_engine()
        reset_sessionmaker()

    print("seed_budget complete:")
    print(f"  tenant_id            = {DEMO_TENANT_ID}")
    print(f"  hard_cap_tokens      = {DEMO_HARD_CAP_TOKENS}")
    print(f"  soft_warn_tokens     = {DEMO_SOFT_WARN_TOKENS}")
    print(f"  credits_period       = {period}")
    print(f"  credits_inserted     = {n_credits}")
    print(f"  usage_rows_inserted  = {n_usage}")


if __name__ == "__main__":
    asyncio.run(main())