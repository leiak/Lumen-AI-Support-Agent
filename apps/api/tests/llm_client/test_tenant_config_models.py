"""Tests for TenantLLMConfig ORM + repository (DB integration via testcontainer)."""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from core.id_gen import new_id
from llm_client.tenant_config_crypto import TenantLLMConfigCipher
from llm_client.tenant_config_models import (
    TenantLLMConfig,
    TenantLLMConfigRepository,
)
from tenant.enums import TenantPlan
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
