"""Tests for the M5.2 ``model`` column on ``TenantLLMConfig``.

Validates the three new code paths:

1. ``upsert`` with ``model=...`` persists the value; without it the
   column stays NULL (back-compat).
2. ``update(model=...)`` writes a new value.
3. ``update(...)`` without ``model`` is a no-op on the column (the
   ``_UNSET`` sentinel guarantees backwards compat).
4. ``update(model=None)`` clears the column.

Mirrors the autouse / ``tenant_row`` pattern from
``test_tenant_fallback_chain_models.py``. Each test gets its own Fernet
key so cipher / cache never leak between tests.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import delete

from core.database import get_sessionmaker
from core.id_gen import new_id
from llm_client.tenant_config_crypto import TenantLLMConfigCipher
from llm_client.tenant_config_models import (
    TenantLLMConfig,
    TenantLLMConfigRepository,
)
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singletons(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset settings/engine/sessionmaker + fresh Fernet key per test.

    Forces a brand-new ``TENANT_LLM_FERNET_KEY`` so the cipher never
    decrypts ciphertext encrypted under a previous test's key.
    """
    from core.config import reset_settings
    from core.database import reset_engine, reset_sessionmaker

    monkeypatch.setenv("TENANT_LLM_FERNET_KEY", Fernet.generate_key().decode())
    reset_settings()
    reset_engine()
    reset_sessionmaker()
    yield
    reset_settings()
    reset_engine()
    reset_sessionmaker()


@pytest.fixture
def cipher() -> TenantLLMConfigCipher:
    """A cipher built from the autouse-fixture's fresh Fernet key.

    Imported from ``settings`` so it matches the env-resolved key.
    """
    from core.config import get_settings

    return TenantLLMConfigCipher(get_settings().tenant_llm_fernet_key)


@pytest_asyncio.fixture
async def tenant_row() -> Tenant:
    """Create a fresh tenant; cascade-delete it on teardown."""
    tenant = await TenantRepository().create(
        name=f"Model override tenant {new_id()[:8]}",
        plan=TenantPlan.PRO,
    )
    try:
        yield tenant
    finally:
        sm = get_sessionmaker()
        async with sm() as session:
            await session.execute(delete(Tenant).where(Tenant.id == tenant.id))
            await session.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_with_model_persists_value(
    cipher: TenantLLMConfigCipher, tenant_row: Tenant,
) -> None:
    """``upsert(model=...)`` writes the model string into the row."""
    repo = TenantLLMConfigRepository()
    row = await repo.upsert(
        tenant_id=tenant_row.id,
        provider_name="anthropic",
        encrypted_api_key=cipher.encrypt("sk-test-placeholder"),
        base_url=None,
        enabled=True,
        model="claude-haiku-4-5",
    )
    assert row.model == "claude-haiku-4-5"

    # Round-trip through the DB: a fresh ``list_by_tenant`` returns
    # the same value.
    rows = await repo.list_by_tenant(tenant_row.id, enabled_only=False)
    assert len(rows) == 1
    assert rows[0].model == "claude-haiku-4-5"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_without_model_defaults_null(
    cipher: TenantLLMConfigCipher, tenant_row: Tenant,
) -> None:
    """``upsert()`` without ``model=`` leaves the column NULL (back-compat)."""
    repo = TenantLLMConfigRepository()
    row = await repo.upsert(
        tenant_id=tenant_row.id,
        provider_name="anthropic",
        encrypted_api_key=cipher.encrypt("sk-test-placeholder"),
        base_url=None,
        enabled=True,
    )
    assert row.model is None

    rows = await repo.list_by_tenant(tenant_row.id, enabled_only=False)
    assert rows[0].model is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_model_to_specific_value(
    cipher: TenantLLMConfigCipher, tenant_row: Tenant,
) -> None:
    """``update(model=...)`` overwrites the column with the new value."""
    repo = TenantLLMConfigRepository()
    # Seed an initial value via upsert.
    await repo.upsert(
        tenant_id=tenant_row.id,
        provider_name="anthropic",
        encrypted_api_key=cipher.encrypt("sk-test-placeholder"),
        base_url=None,
        enabled=True,
        model="claude-haiku-4-5",
    )

    # Now overwrite via update (no encrypted_api_key round-trip).
    updated = await repo.update(
        tenant_id=tenant_row.id,
        provider_name="anthropic",
        model="claude-3-5-sonnet-20241022",
    )
    assert updated is not None
    assert updated.model == "claude-3-5-sonnet-20241022"

    # Confirm persisted.
    rows = await repo.list_by_tenant(tenant_row.id, enabled_only=False)
    assert rows[0].model == "claude-3-5-sonnet-20241022"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_model_omitted_no_op(
    cipher: TenantLLMConfigCipher, tenant_row: Tenant,
) -> None:
    """``update(enabled=False)`` (no model kwarg) does NOT touch model.

    Back-compat — pre-M5.2 callers (and the existing admin SPA endpoints)
    that only flip ``enabled`` must leave the model column alone. The
    ``_UNSET`` sentinel guarantees this.
    """
    repo = TenantLLMConfigRepository()
    await repo.upsert(
        tenant_id=tenant_row.id,
        provider_name="anthropic",
        encrypted_api_key=cipher.encrypt("sk-test-placeholder"),
        base_url=None,
        enabled=True,
        model="claude-haiku-4-5",
    )

    # update with no model kwarg — should be a no-op on the column.
    updated = await repo.update(
        tenant_id=tenant_row.id,
        provider_name="anthropic",
        enabled=False,
    )
    assert updated is not None
    assert updated.enabled is False
    assert updated.model == "claude-haiku-4-5"  # unchanged


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_model_to_null_clears(
    cipher: TenantLLMConfigCipher, tenant_row: Tenant,
) -> None:
    """``update(model=None)`` clears the column (back to project default)."""
    repo = TenantLLMConfigRepository()
    await repo.upsert(
        tenant_id=tenant_row.id,
        provider_name="anthropic",
        encrypted_api_key=cipher.encrypt("sk-test-placeholder"),
        base_url=None,
        enabled=True,
        model="claude-haiku-4-5",
    )

    updated = await repo.update(
        tenant_id=tenant_row.id,
        provider_name="anthropic",
        model=None,
    )
    assert updated is not None
    assert updated.model is None

    rows = await repo.list_by_tenant(tenant_row.id, enabled_only=False)
    assert rows[0].model is None


# Silence F401 — ORM is imported so the table is registered with the
# metadata for the integration suite.
_ = TenantLLMConfig