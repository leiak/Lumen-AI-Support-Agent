"""Tests for TenantFallbackChain ORM + repository (DB integration).

Marked ``@pytest.mark.integration`` so the default selector
(``pytest -m "not integration"``) skips it. Mirrors the style of
``tests/llm_client/integration/test_tenant_resolver_e2e.py`` — autouse
fixture resetting settings/engine/sessionmaker between tests, and a
``tenant_row`` factory fixture that cascade-deletes its tenant row in
teardown.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete

from core.database import get_sessionmaker
from core.id_gen import new_id
from llm_client.tenant_fallback_chain_models import (
    TenantFallbackChain,
    TenantFallbackChainRepository,
)
from tenant.models import Tenant

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _reset_singletons():
    """Reset settings/engine/sessionmaker between tests.

    Mirrors the autouse pattern from ``test_tenant_resolver_e2e.py``.
    Each test gets a fresh event loop; without resets the SQLAlchemy
    engine is bound to a closed loop.
    """
    from core.config import reset_settings
    from core.database import reset_engine, reset_sessionmaker

    reset_settings()
    reset_engine()
    reset_sessionmaker()
    yield
    reset_settings()
    reset_engine()
    reset_sessionmaker()


@pytest_asyncio.fixture
async def tenant_row():
    """Create a fresh tenant; cascade-delete it on teardown."""
    from tenant.enums import TenantPlan
    from tenant.repository import TenantRepository

    tenant = await TenantRepository().create(
        name=f"Test tenant {new_id()[:8]}",
        plan=TenantPlan.PRO,
    )
    try:
        yield tenant
    finally:
        # Cascade-delete via direct DELETE — TenantRepository.delete
        # would also work but a raw DELETE keeps the test independent
        # of any repository-side soft-delete behavior.
        sm = get_sessionmaker()
        async with sm() as session:
            await session.execute(delete(Tenant).where(Tenant.id == tenant.id))
            await session.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_returns_none_for_missing_tenant(tenant_row: Tenant) -> None:
    """A freshly-created tenant has no chain yet → ``get`` returns None."""
    repo = TenantFallbackChainRepository()
    result = await repo.get(tenant_row.id)
    assert result is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_creates_new_row(tenant_row: Tenant) -> None:
    """First ``upsert`` inserts a row with the expected id + fields."""
    repo = TenantFallbackChainRepository()
    steps = [
        {
            "provider": "anthropic",
            "model": "claude-3-5-sonnet-20241022",
            "enabled": True,
        },
    ]
    row = await repo.upsert(tenant_id=tenant_row.id, steps=steps, enabled=True)
    assert row.id, "ULID must be non-empty"
    assert row.tenant_id == tenant_row.id
    assert row.steps == steps
    assert row.enabled is True

    # Round-trip: a fresh ``get`` returns the same row.
    fetched = await repo.get(tenant_row.id)
    assert fetched is not None
    assert fetched.id == row.id
    assert fetched.steps == steps


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_updates_existing_row(tenant_row: Tenant) -> None:
    """Second ``upsert`` with the same tenant_id replaces the row's steps."""
    repo = TenantFallbackChainRepository()
    initial = [
        {
            "provider": "anthropic",
            "model": "claude-3-5-sonnet-20241022",
            "enabled": True,
        },
    ]
    await repo.upsert(tenant_id=tenant_row.id, steps=initial, enabled=True)

    new_steps = [
        {"provider": "openai", "model": "gpt-4o", "enabled": True},
        {"provider": "minimax", "model": "MiniMax-M3", "enabled": True},
    ]
    await repo.upsert(tenant_id=tenant_row.id, steps=new_steps, enabled=True)

    fetched = await repo.get(tenant_row.id)
    assert fetched is not None
    assert fetched.steps == new_steps


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_with_disabled_true(tenant_row: Tenant) -> None:
    """``enabled=True`` is honored on initial insert."""
    repo = TenantFallbackChainRepository()
    steps = [
        {"provider": "minimax", "model": "MiniMax-M3", "enabled": True},
    ]
    row = await repo.upsert(tenant_id=tenant_row.id, steps=steps, enabled=True)
    assert row.enabled is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_with_disabled_false(tenant_row: Tenant) -> None:
    """``enabled=False`` is honored on initial insert (ops-level kill switch)."""
    repo = TenantFallbackChainRepository()
    steps = [
        {"provider": "minimax", "model": "MiniMax-M3", "enabled": True},
    ]
    row = await repo.upsert(tenant_id=tenant_row.id, steps=steps, enabled=False)
    assert row.enabled is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_rejects_unknown_provider(tenant_row: Tenant) -> None:
    """A step with an unknown provider is rejected with ``unknown provider``."""
    repo = TenantFallbackChainRepository()
    steps = [
        {"provider": "bogus", "model": "x", "enabled": True},
    ]
    with pytest.raises(ValueError, match="unknown provider"):
        await repo.upsert(tenant_id=tenant_row.id, steps=steps, enabled=True)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_rejects_empty_enabled_chain(tenant_row: Tenant) -> None:
    """A chain where every step is disabled is rejected."""
    repo = TenantFallbackChainRepository()
    steps = [
        {"provider": "minimax", "model": "MiniMax-M3", "enabled": False},
        {"provider": "openai", "model": "gpt-4o", "enabled": False},
    ]
    with pytest.raises(
        ValueError, match="chain must contain at least one enabled step",
    ):
        await repo.upsert(tenant_id=tenant_row.id, steps=steps, enabled=True)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_rejects_empty_steps(tenant_row: Tenant) -> None:
    """An empty steps list is rejected with the same message as all-disabled."""
    repo = TenantFallbackChainRepository()
    with pytest.raises(
        ValueError, match="chain must contain at least one enabled step",
    ):
        await repo.upsert(tenant_id=tenant_row.id, steps=[], enabled=True)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_validates_model_length(tenant_row: Tenant) -> None:
    """Empty or oversized model strings are rejected."""
    repo = TenantFallbackChainRepository()
    with pytest.raises(ValueError):
        await repo.upsert(
            tenant_id=tenant_row.id,
            steps=[{"provider": "minimax", "model": "", "enabled": True}],
            enabled=True,
        )
    with pytest.raises(ValueError):
        await repo.upsert(
            tenant_id=tenant_row.id,
            steps=[
                {
                    "provider": "minimax",
                    "model": "x" * 129,  # one over the limit
                    "enabled": True,
                },
            ],
            enabled=True,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_removes_row(tenant_row: Tenant) -> None:
    """``delete`` returns ``True`` on hit and the row is gone afterwards."""
    repo = TenantFallbackChainRepository()
    steps = [
        {"provider": "minimax", "model": "MiniMax-M3", "enabled": True},
    ]
    await repo.upsert(tenant_id=tenant_row.id, steps=steps, enabled=True)

    deleted = await repo.delete(tenant_row.id)
    assert deleted is True

    fetched = await repo.get(tenant_row.id)
    assert fetched is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_idempotent(tenant_row: Tenant) -> None:
    """``delete`` on a tenant with no row returns ``False`` (idempotent)."""
    repo = TenantFallbackChainRepository()
    deleted = await repo.delete(tenant_row.id)
    assert deleted is False


# Silence F401 on the imported symbol — tests use the class indirectly
# through the repository, but having the import ensures the ORM is
# registered with the metadata for any future migration sanity check.
_ = TenantFallbackChain