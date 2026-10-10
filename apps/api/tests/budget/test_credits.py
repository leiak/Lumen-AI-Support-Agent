"""Pack B #2 — CreditService.grant inserts immutable audit rows.

The service is the only writer for ``tenant_budget_credits``. It validates
inputs (positive tokens, non-empty note) and stamps the row with the
UTC period, a fresh ULID, and the super-admin's user id from JWT claims.
The per-model cache invalidation call is wired in even though the
PerModelBreakdownCache ships in Task 4 — Task 3's resolver + Task 4's
breakdown endpoint rely on the cache being invalidated when a credit
is granted.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from budget.credits import CreditService
from budget.models import TenantBudgetCredit


def _make_session() -> MagicMock:
    s = MagicMock()
    s.add = MagicMock()
    s.flush = AsyncMock()
    return s


def _make_cache() -> MagicMock:
    c = MagicMock()
    c.invalidate = MagicMock()
    return c


async def test_grant_inserts_row_with_current_utc_period() -> None:
    """Spec §3.3: grant inserts a row with period=YYYY-MM (UTC) and ULID id."""
    session = _make_session()
    cache = _make_cache()
    svc = CreditService(session, cache)
    credit = await svc.grant(
        tenant_id="t1", tokens=500, note="Q4 promo", granted_by="super-uid"
    )
    assert isinstance(credit, TenantBudgetCredit)
    assert credit.tenant_id == "t1"
    assert credit.tokens == 500
    assert credit.note == "Q4 promo"
    assert credit.granted_by == "super-uid"
    assert credit.period == datetime.now(UTC).strftime("%Y-%m")
    assert credit.id  # ULID is non-empty
    session.add.assert_called_once()
    session.flush.assert_awaited_once()
    cache.invalidate.assert_called_once_with("t1", period=credit.period)


async def test_grant_rejects_zero_tokens() -> None:
    svc = CreditService(_make_session(), _make_cache())
    with pytest.raises(ValueError, match="tokens must be > 0"):
        await svc.grant(tenant_id="t1", tokens=0, note="x", granted_by="u")


async def test_grant_rejects_negative_tokens() -> None:
    svc = CreditService(_make_session(), _make_cache())
    with pytest.raises(ValueError, match="tokens must be > 0"):
        await svc.grant(tenant_id="t1", tokens=-1, note="x", granted_by="u")


async def test_grant_rejects_empty_note() -> None:
    svc = CreditService(_make_session(), _make_cache())
    with pytest.raises(ValueError, match="note must be non-empty"):
        await svc.grant(tenant_id="t1", tokens=100, note="   ", granted_by="u")


async def test_grant_strips_note_whitespace() -> None:
    session = _make_session()
    cache = _make_cache()
    svc = CreditService(session, cache)
    credit = await svc.grant(
        tenant_id="t1", tokens=100, note="  padded note  ", granted_by="u"
    )
    assert credit.note == "padded note"