"""Tests for TenantLLMConfig ORM + repository (DB integration via testcontainer)."""
from __future__ import annotations

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

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

pytestmark = pytest.mark.integration


@pytest.fixture
def cipher() -> TenantLLMConfigCipher:
    from cryptography.fernet import Fernet
    return TenantLLMConfigCipher(Fernet.generate_key().decode())


async def test_create_and_get(cipher: TenantLLMConfigCipher) -> None:
    tenant = await TenantRepository().create(name="BYOK Test 1", plan=TenantPlan.PRO)
    repo = TenantLLMConfigRepository()
    ciphertext = cipher.encrypt("sk-round-trip-test")
    created = await repo.upsert(
        tenant_id=tenant.id,
        provider_name="minimax",
        encrypted_api_key=ciphertext,
        base_url=None,
        enabled=True,
    )
    assert created.tenant_id == tenant.id
    assert created.provider_name == "minimax"
    assert created.encrypted_api_key == ciphertext
    # Verify decryption round-trip
    assert cipher.decrypt(created.encrypted_api_key) == "sk-round-trip-test"


async def test_unique_constraint_per_tenant_provider(cipher: TenantLLMConfigCipher) -> None:
    tenant = await TenantRepository().create(name="BYOK Test 2", plan=TenantPlan.PRO)
    repo = TenantLLMConfigRepository()
    ciphertext = cipher.encrypt("sk-first")
    await repo.upsert(
        tenant_id=tenant.id,
        provider_name="minimax",
        encrypted_api_key=ciphertext,
        base_url=None,
        enabled=True,
    )
    # Upsert (not insert) should succeed and replace
    updated_ciphertext = cipher.encrypt("sk-second")
    second = await repo.upsert(
        tenant_id=tenant.id,
        provider_name="minimax",
        encrypted_api_key=updated_ciphertext,
        base_url=None,
        enabled=True,
    )
    assert cipher.decrypt(second.encrypted_api_key) == "sk-second"
    # Direct INSERT bypassing upsert should fail
    from sqlalchemy import insert

    from core.database import get_sessionmaker
    sm = get_sessionmaker()
    async with sm() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                insert(TenantLLMConfig).values(
                    id=new_id(),
                    tenant_id=tenant.id,
                    provider_name="minimax",
                    encrypted_api_key=ciphertext,
                    enabled=True,
                )
            )


async def test_list_by_tenant_returns_only_enabled(cipher: TenantLLMConfigCipher) -> None:
    tenant = await TenantRepository().create(name="BYOK Test 3", plan=TenantPlan.PRO)
    repo = TenantLLMConfigRepository()
    await repo.upsert(
        tenant_id=tenant.id, provider_name="minimax",
        encrypted_api_key=cipher.encrypt("sk-a"), base_url=None, enabled=True,
    )
    await repo.upsert(
        tenant_id=tenant.id, provider_name="anthropic",
        encrypted_api_key=cipher.encrypt("sk-b"), base_url=None, enabled=False,
    )
    rows = await repo.list_by_tenant(tenant.id, enabled_only=True)
    assert len(rows) == 1
    assert rows[0].provider_name == "minimax"


async def test_list_by_tenant_returns_empty_for_new_tenant() -> None:
    tenant = await TenantRepository().create(name="BYOK Test 4", plan=TenantPlan.PRO)
    repo = TenantLLMConfigRepository()
    rows = await repo.list_by_tenant(tenant.id)
    assert rows == []


async def test_cascade_delete_with_tenant(cipher: TenantLLMConfigCipher) -> None:
    tenant = await TenantRepository().create(name="BYOK Test 5", plan=TenantPlan.PRO)
    repo = TenantLLMConfigRepository()
    await repo.upsert(
        tenant_id=tenant.id, provider_name="minimax",
        encrypted_api_key=cipher.encrypt("sk-cascade"), base_url=None, enabled=True,
    )
    # Delete tenant — config rows should cascade
    from core.database import get_sessionmaker
    sm = get_sessionmaker()
    async with sm() as session:
        tenant_obj = await session.get(type(tenant), tenant.id)
        await session.delete(tenant_obj)
        await session.commit()
    rows = await repo.list_by_tenant(tenant.id)
    assert rows == []


# --- M5.3 Task 2 back-compat: session=... kwarg is optional ---
#
# The inner repo gained an optional ``session: AsyncSession | None`` parameter
# on its three mutating methods (``upsert`` / ``update`` /
# ``delete_by_tenant_provider``) so the M5.3 wrapper can compose the
# configuration mutation + audit INSERT in a single transaction. When
# ``session`` is omitted (the back-compat branch), the method must keep
# the old behavior: open its own session and commit at the end. These
# tests pin that behavior — extend-seed.py + existing callers depend on
# the None branch.


async def test_upsert_without_session_arg_still_works(cipher: TenantLLMConfigCipher) -> None:
    """M5.3 — inner repo's ``upsert`` must still work when called
    WITHOUT a session=... kwarg (extend-seed.py + existing tests rely
    on the back-compat branch)."""
    tenant = await TenantRepository().create(
        name="M5.3 back-compat upsert", plan=TenantPlan.PRO,
    )
    try:
        repo = TenantLLMConfigRepository()
        ciphertext = cipher.encrypt("sk-bc-upsert")
        row = await repo.upsert(
            tenant_id=tenant.id,
            provider_name="minimax",
            encrypted_api_key=ciphertext,
            base_url=None,
            enabled=True,
        )
        assert row.tenant_id == tenant.id
        assert cipher.decrypt(row.encrypted_api_key) == "sk-bc-upsert"
    finally:
        async with get_sessionmaker()() as session:
            await session.execute(delete(Tenant).where(Tenant.id == tenant.id))
            await session.commit()


async def test_update_without_session_arg_still_works(cipher: TenantLLMConfigCipher) -> None:
    """M5.3 — ``update`` back-compat: no session=... kwarg."""
    tenant = await TenantRepository().create(
        name="M5.3 back-compat update", plan=TenantPlan.PRO,
    )
    try:
        repo = TenantLLMConfigRepository()
        await repo.upsert(
            tenant_id=tenant.id, provider_name="minimax",
            encrypted_api_key=cipher.encrypt("sk-bc-upd"),
            base_url=None, enabled=True,
        )
        row = await repo.update(tenant_id=tenant.id, provider_name="minimax", enabled=False)
        assert row is not None
        assert row.enabled is False
    finally:
        async with get_sessionmaker()() as session:
            await session.execute(delete(Tenant).where(Tenant.id == tenant.id))
            await session.commit()


async def test_delete_without_session_arg_still_works(cipher: TenantLLMConfigCipher) -> None:
    """M5.3 — ``delete_by_tenant_provider`` back-compat: no session=... kwarg."""
    tenant = await TenantRepository().create(
        name="M5.3 back-compat delete", plan=TenantPlan.PRO,
    )
    try:
        repo = TenantLLMConfigRepository()
        await repo.upsert(
            tenant_id=tenant.id, provider_name="minimax",
            encrypted_api_key=cipher.encrypt("sk-bc-del"),
            base_url=None, enabled=True,
        )
        deleted = await repo.delete_by_tenant_provider(
            tenant_id=tenant.id, provider_name="minimax",
        )
        assert deleted is True
    finally:
        async with get_sessionmaker()() as session:
            await session.execute(delete(Tenant).where(Tenant.id == tenant.id))
            await session.commit()
