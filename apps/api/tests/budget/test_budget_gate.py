"""Pack B #8 — TenantBudgetRateLimited exception + post-mortem gate.

Covers:
- Exception class carries full context (tenant_id, period, remaining, threshold).
- BudgetResolver gate fires when remaining < threshold after a 429.
- Gate does NOT fire when remaining >= threshold (429 propagates).
- Gate is disabled when threshold=0.
- Gate uses effective_cap = base + credits (not base alone).
- Gate metric increments on fire.
- FastAPI handler converts TenantBudgetRateLimited → HTTP 429 with
  structured body.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from budget.exceptions import TenantBudgetRateLimited
from budget.resolver import BudgetResolver
from llm_client.exceptions import RateLimited


# ---- helpers ---------------------------------------------------------------


def _make_response(prompt: int = 100, completion: int = 50):
    """Build a ChatResponse-like stub (no DB)."""
    from llm_client.types import ChatResponse

    return ChatResponse(
        content="ok",
        provider="openai",
        model="gpt-4o-mini",
        prompt_tokens=prompt,
        completion_tokens=completion,
        cost_usd=0.0,
        request_id="r",
        cached=False,
    )


def _make_budget(*, hard_cap: int = 8000, soft_warn: int = 5000):
    """Build a TenantBudget-like stub (no DB)."""
    from budget.models import TenantBudget

    b = MagicMock(spec=TenantBudget)
    b.id = "b1"
    b.tenant_id = "t1"
    b.hard_cap_tokens = hard_cap
    b.soft_warn_tokens = soft_warn
    b.period_anchor_tz = "UTC"
    return b


def _make_snapshot(tenant_id: str, period: str, *, tokens_used: int = 0):
    """Build a TenantBudgetSnapshot-like stub (no DB)."""
    from datetime import datetime, timezone

    from budget.models import TenantBudgetSnapshot

    s = MagicMock(spec=TenantBudgetSnapshot)
    s.id = "s1"
    s.tenant_id = tenant_id
    s.period = period
    s.tokens_used = tokens_used
    s.last_refreshed_at = datetime.now(timezone.utc)
    s.soft_warn_fired_at = None
    return s


def _make_resolver(
    *,
    hard_cap: int = 8000,
    tokens_used: int = 0,
    credits: int = 0,
    inner_side_effect: BaseException | None = None,
):
    """Construct a BudgetResolver with all collaborators mocked."""
    budget = _make_budget(hard_cap=hard_cap)
    snap = _make_snapshot("t1", "2026-10", tokens_used=tokens_used)
    cache = MagicMock()
    cache.get_or_load_async = AsyncMock(return_value=snap)
    cache.invalidate = MagicMock()
    snap_repo = MagicMock()
    snap_repo.get_for_tenant_period = AsyncMock(return_value=snap)
    snap_repo.refresh = AsyncMock(return_value=snap)
    snap_repo.set_tokens_used = AsyncMock()
    credit_svc = MagicMock()
    credit_svc.sum_for_period = AsyncMock(return_value=credits)
    per_model_cache = MagicMock()
    per_model_cache.invalidate = MagicMock()

    inner = MagicMock()
    if inner_side_effect is not None:
        inner.ainvoke = AsyncMock(side_effect=inner_side_effect)
    else:
        inner.ainvoke = AsyncMock(return_value=_make_response())

    resolver = BudgetResolver(
        inner=inner,
        tenant_id="t1",
        budget=budget,
        snapshot_cache=cache,
        snapshot_repo=snap_repo,
        credit_service=credit_svc,
        per_model_cache=per_model_cache,
    )
    return resolver, inner, snap_repo, per_model_cache


# ---- 1. Exception class ----------------------------------------------------


def test_exception_carries_full_context() -> None:
    """Spec §5.1: tenant_id, period, remaining, threshold all on the exception."""
    exc = TenantBudgetRateLimited(
        tenant_id="t1", period="2026-10", remaining=50, threshold=1000
    )
    assert exc.tenant_id == "t1"
    assert exc.period == "2026-10"
    assert exc.remaining == 50
    assert exc.threshold == 1000
    assert "t1" in str(exc)
    assert "2026-10" in str(exc)
    assert "50" in str(exc)
    assert "1000" in str(exc)


# ---- 2. Gate fires when remaining < threshold ------------------------------


async def test_gate_raises_when_remaining_below_threshold() -> None:
    """remaining=500 < 1000 → TenantBudgetRateLimited raised.

    Verifies:
    - The gate raises with the right remaining/threshold.
    - Per-model cache was invalidated (the 429 still hit a provider).
    - snapshot_repo.set_tokens_used was NOT called (no tokens billed).
    """
    resolver, inner, snap_repo, per_model_cache = _make_resolver(
        hard_cap=8000,
        tokens_used=7500,  # remaining = effective_cap - tokens_used = 8000 - 7500 = 500
        inner_side_effect=RateLimited("429 from openai"),
    )

    with pytest.raises(TenantBudgetRateLimited) as exc_info:
        await resolver.ainvoke(MagicMock())
    assert exc_info.value.remaining == 500
    assert exc_info.value.threshold == 1000
    assert exc_info.value.tenant_id == "t1"
    assert exc_info.value.period == "2026-10"
    # Per-model cache invalidated because a request did hit a provider.
    per_model_cache.invalidate.assert_called_with("t1", "2026-10")
    # No post_record — no tokens were billed.
    snap_repo.set_tokens_used.assert_not_called()


# ---- 3. Gate does NOT fire when remaining >= threshold ----------------------


async def test_gate_does_not_raise_when_remaining_above_threshold() -> None:
    """remaining=5000 >= 1000 → 429 propagates as-is (no gate)."""
    resolver, _inner, snap_repo, per_model_cache = _make_resolver(
        hard_cap=8000,
        tokens_used=3000,  # remaining = 5000
        inner_side_effect=RateLimited("429 from openai"),
    )

    with pytest.raises(RateLimited):
        await resolver.ainvoke(MagicMock())
    # No gate fired → no metric bump, no exception raised.
    snap_repo.set_tokens_used.assert_not_called()
    # Per-model cache WAS still invalidated (the 429 hit a provider).
    per_model_cache.invalidate.assert_called_with("t1", "2026-10")


# ---- 4. Gate disabled when threshold=0 --------------------------------------


async def test_gate_disabled_when_threshold_zero() -> None:
    """TENANT_BUDGET_429_SKIP_THRESHOLD_TOKENS=0 disables the gate.

    Even with remaining=1 (< the default 1000), the gate is off and
    RateLimited propagates.
    """
    import os

    from core.config import get_settings, reset_settings

    original_env = os.environ.get("TENANT_BUDGET_429_SKIP_THRESHOLD_TOKENS")
    os.environ["TENANT_BUDGET_429_SKIP_THRESHOLD_TOKENS"] = "0"
    reset_settings()
    try:
        resolver, _inner, snap_repo, _per_model_cache = _make_resolver(
            hard_cap=8000,
            tokens_used=7999,  # remaining = 1 (would normally trigger gate)
            inner_side_effect=RateLimited("429 from openai"),
        )
        # Sanity: setting was applied.
        assert get_settings().tenant_budget_429_skip_threshold_tokens == 0
        with pytest.raises(RateLimited):
            await resolver.ainvoke(MagicMock())
        snap_repo.set_tokens_used.assert_not_called()
    finally:
        if original_env is None:
            os.environ.pop("TENANT_BUDGET_429_SKIP_THRESHOLD_TOKENS", None)
        else:
            os.environ["TENANT_BUDGET_429_SKIP_THRESHOLD_TOKENS"] = original_env
        reset_settings()


# ---- 5. Gate uses effective_cap (base + credits), not base alone -----------


async def test_gate_uses_effective_cap_not_base() -> None:
    """When credits are present, gate uses effective_cap = base + credits.

    base=8000, credits=2000 → effective_cap=10000.
    tokens_used=8500 → remaining=1500 > 1000 → gate does NOT fire.
    If gate had used base alone, remaining would be 8000-8500=-500
    (still < threshold) and the gate would have fired — but credits
    bought the tenant more runway.
    """
    resolver, _inner, snap_repo, _per_model_cache = _make_resolver(
        hard_cap=8000,
        tokens_used=8500,
        credits=2000,  # effective_cap = 8000 + 2000 = 10000, remaining = 1500
        inner_side_effect=RateLimited("429 from openai"),
    )
    with pytest.raises(RateLimited):
        await resolver.ainvoke(MagicMock())
    snap_repo.set_tokens_used.assert_not_called()


# ---- 6. Metric increments on gate fire --------------------------------------


async def test_gate_metric_increments() -> None:
    """LLM_BUDGET_GATE_TOTAL += 1 when the gate fires."""
    from core.business_metrics import LLM_BUDGET_GATE_TOTAL

    before = LLM_BUDGET_GATE_TOTAL._value.get()
    resolver, _inner, _snap_repo, _per_model_cache = _make_resolver(
        hard_cap=8000,
        tokens_used=7500,  # remaining=500 < 1000
        inner_side_effect=RateLimited("429 from openai"),
    )
    with pytest.raises(TenantBudgetRateLimited):
        await resolver.ainvoke(MagicMock())
    after = LLM_BUDGET_GATE_TOTAL._value.get()
    assert after == before + 1


# ---- 7. FastAPI handler converts to HTTP 429 with structured body ----------


@pytest.mark.asyncio
async def test_handler_returns_429_with_structured_body() -> None:
    """FastAPI handler: TenantBudgetRateLimited → HTTP 429 + JSON body."""
    from httpx import ASGITransport, AsyncClient

    from main import app

    @app.get("/_test/budget_gate/raise")
    async def _raise_gate():
        raise TenantBudgetRateLimited(
            tenant_id="t1", period="2026-10", remaining=50, threshold=1000
        )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.get("/_test/budget_gate/raise")
    assert resp.status_code == 429
    body = resp.json()
    assert body["error"] == "budget_rate_limited"
    assert body["tenant_id"] == "t1"
    assert body["period"] == "2026-10"
    assert body["remaining"] == 50
    assert body["threshold"] == 1000
