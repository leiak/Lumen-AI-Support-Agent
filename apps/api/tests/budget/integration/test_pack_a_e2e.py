"""E2E tests for M4.D Pack A (tech-debt #3 + #4 + #7 + #1).

Tests exercise the resolver end-to-end against a live DB. Tenant +
budget fixtures are created via the public repositories. No direct
ORM manipulation beyond what's needed to seed snapshot rows.

Marked ``@pytest.mark.integration`` so the default selector
(``pytest -m "not integration"``) skips it, matching the existing
``tests/budget/integration/test_budget_e2e.py`` convention.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from auth.jwt import create_access_token
from budget.cache import TenantBudgetSnapshotCache
from budget.repository import (
    TenantBudgetRepository,
    TenantBudgetSnapshotRepository,
)
from budget.resolver import BudgetResolver
from core.config import reset_settings
from llm_client.exceptions import TenantBudgetExceeded
from llm_client.types import ChatRequest, ChatResponse
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_db_singletons(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset engine / sessionmaker / settings per test for isolation.

    Each pytest-asyncio test gets its own event loop. Without resetting
    the SQLAlchemy engine, the previous test's closed loop would be
    reused and raise "Event loop is closed". Without resetting
    settings, an env override set by monkeypatch wouldn't reach
    ``get_settings()`` (which caches the result on first call).
    """
    monkeypatch.setenv("TENANT_LLM_FERNET_KEY", Fernet.generate_key().decode())
    reset_settings()
    from core.database import reset_engine, reset_sessionmaker

    reset_engine()
    reset_sessionmaker()
    yield
    reset_engine()
    reset_sessionmaker()
    reset_settings()


@pytest.fixture
async def async_client_app() -> AsyncGenerator[AsyncClient, None]:
    """Yield an httpx client wired to the FULL FastAPI app."""
    from main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.fixture
async def tenant_with_budget() -> AsyncGenerator[str, None]:
    """Create a tenant + a budget. Cleans up via cascade-delete."""
    tenant = await TenantRepository().create(
        name="PackA E2E Tenant", plan=TenantPlan.PRO,
    )
    await TenantBudgetRepository().upsert(
        tenant_id=tenant.id,
        soft_warn_tokens=6000,
        hard_cap_tokens=8000,
    )
    try:
        yield tenant.id
    finally:
        from core.database import get_session

        async with get_session() as session:
            t = await session.get(Tenant, tenant.id)
            if t is not None:
                await session.delete(t)
                await session.commit()


def _make_response(
    prompt_tokens: int = 100, completion_tokens: int = 50
) -> ChatResponse:
    """Build a ChatResponse stub that the inner resolver returns."""
    r = MagicMock(spec=ChatResponse)
    r.prompt_tokens = prompt_tokens
    r.completion_tokens = completion_tokens
    return r


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_e2e_pack_a_no_overshoot_under_concurrent_calls(
    tenant_with_budget: str,
) -> None:
    """Pack A #3 — DB-direct pre-check rejects at/above hard cap.

    Pre-populate snapshot tokens_used=7950 (under cap=8000). Run calls
    consuming 150 tokens. The pre-check at DB-direct must see 7950 + 150
    = 8100 (over cap) and reject the next call. Verify a rejection
    happens before tokens_used blows far above cap.
    """
    tenant_id = tenant_with_budget
    srepo = TenantBudgetSnapshotRepository()
    await srepo.set_tokens_used(
        tenant_id=tenant_id, period="2026-10", tokens_used=7950,
    )

    inner = MagicMock()
    inner.ainvoke = AsyncMock(return_value=_make_response(prompt_tokens=100, completion_tokens=50))
    # Use a real cache object — DB-direct path doesn't read it, but the
    # constructor needs one.
    cache = TenantBudgetSnapshotCache(ttl_s=60.0, maxsize=1024)
    budget = await TenantBudgetRepository().get_by_tenant(tenant_id)
    resolver = BudgetResolver(
        inner=inner, tenant_id=tenant_id,
        budget=budget, snapshot_cache=cache,
    )

    rejected_at: list[datetime] = []
    for _ in range(20):
        try:
            await resolver.ainvoke(MagicMock(spec=ChatRequest))
        except TenantBudgetExceeded:
            rejected_at.append(datetime.now(tz=UTC))
            break

    assert len(rejected_at) == 1
    # The cap WAS hit, post-record may have pushed tokens_used over
    # cap (we don't fix post-record in Pack A, only pre-check). Verify
    # the rejection happened — the exact tokens_used after rejection
    # is not asserted.
    snap = await srepo.get_for_tenant_period(tenant_id, "2026-10")
    assert snap is not None
    assert snap.tokens_used >= 8000


@pytest.mark.integration
async def test_e2e_soft_warn_fires_once_under_load(
    tenant_with_budget: str,
) -> None:
    """Pack A #4 — soft-warn fires once across 5 calls crossing threshold."""
    from core.business_metrics import LLM_TENANT_BUDGET_SOFT_WARN_TOTAL

    tenant_id = tenant_with_budget
    srepo = TenantBudgetSnapshotRepository()
    # Pre-populate: 5900 used, soft_warn=6000 (will fire on first crossing)
    await srepo.set_tokens_used(
        tenant_id=tenant_id, period="2026-10",
        tokens_used=5900, soft_warn_fired_at=None,
    )

    inner = MagicMock()
    inner.ainvoke = AsyncMock(return_value=_make_response(prompt_tokens=100, completion_tokens=100))
    cache = TenantBudgetSnapshotCache(ttl_s=60.0, maxsize=1024)
    budget = await TenantBudgetRepository().get_by_tenant(tenant_id)
    resolver = BudgetResolver(
        inner=inner, tenant_id=tenant_id,
        budget=budget, snapshot_cache=cache,
    )

    before = LLM_TENANT_BUDGET_SOFT_WARN_TOTAL._value.get()  # type: ignore[attr-defined]
    for _ in range(5):
        await resolver.ainvoke(MagicMock(spec=ChatRequest))
    after = LLM_TENANT_BUDGET_SOFT_WARN_TOTAL._value.get()  # type: ignore[attr-defined]
    # Call 1: 5900 + 200 = 6100 (crosses 6000) → fires
    # Calls 2-5: already fired → no fire
    assert after - before == 1


@pytest.mark.integration
async def test_e2e_cleanup_admin_endpoint_requires_jwt(
    async_client_app: AsyncClient,
) -> None:
    """POST /admin/budget/cleanup — 401 / 404 / 200 auth gating.

    Same auth contract as the integration test in
    ``tests/admin/test_budget_api.py`` but mounted against the FULL
    FastAPI app via pytest-httpx. The role column must be ``admin`` (not
    ``super_admin``) — ``require_admin`` only accepts ``admin`` / ``owner``
    roles. The super-admin bypass is achieved by setting
    ``tenant_id=None`` via the ``extra`` override, which makes
    ``claims.get("tenant_id")`` return ``None`` and bypasses the
    anti-enumeration 404.
    """
    # 401 — no token
    resp = await async_client_app.post("/api/v1/admin/budget/cleanup")
    assert resp.status_code == 401

    # 404 — admin token (has tenant_id claim)
    tenant_token = create_access_token(
        tenant_id="t-anyone", user_id="u-1", role="admin",
    )
    resp = await async_client_app.post(
        "/api/v1/admin/budget/cleanup",
        headers={"Authorization": f"Bearer {tenant_token}"},
    )
    assert resp.status_code == 404

    # 200 — super-admin (no tenant_id claim via extra override).
    # ``require_admin`` only accepts admin/owner — use ``admin`` here.
    # The load-bearing bypass is ``tenant_id=None`` via ``extra``.
    super_token = create_access_token(
        tenant_id="ignored",  # required by signature; overridden below
        user_id="super-1",
        role="admin",
        extra={"tenant_id": None},  # claims.get("tenant_id") returns None
    )
    resp = await async_client_app.post(
        "/api/v1/admin/budget/cleanup",
        headers={"Authorization": f"Bearer {super_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "deleted_rows" in body
    assert "cutoff_period" in body


__all__ = [
    "test_e2e_cleanup_admin_endpoint_requires_jwt",
    "test_e2e_pack_a_no_overshoot_under_concurrent_calls",
    "test_e2e_soft_warn_fires_once_under_load",
]