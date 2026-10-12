"""Wrapper integration tests for the M5.3 audit log.

Covers:
  * 8 happy-path tests (POST/PATCH/DELETE → expected audit rows)
  * 3 atomicity tests (monkey-patch audit_repo.insert_many to raise;
    assert the mutation also rolled back — the whole point of M5.3)
  * 1 back-compat test (inner repo without session=... still works)

All tests use the same autouse fixtures as the rest of the admin
suite: ``_reset_db_singletons`` + ``_ensure_fernet_key`` from
``tests/admin/conftest.py``.
"""
from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest
from sqlalchemy import delete, select

from admin.repository import AdminTenantLLMConfigRepository
from admin.schemas.tenant_llm_config import (
    TenantLLMConfigCreate,
    TenantLLMConfigUpdate,
)
from core.config import get_settings
from core.database import get_sessionmaker
from llm_client.tenant_config_audit_models import (
    TenantLLMConfigAuditLog,
    TenantLLMConfigAuditRepository,
)
from llm_client.tenant_config_crypto import TenantLLMConfigCipher
from llm_client.tenant_config_models import (
    TenantLLMConfig,
    TenantLLMConfigRepository,
)
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository

pytestmark = pytest.mark.integration


# --- fixtures ---


@pytest.fixture
def cipher() -> TenantLLMConfigCipher:
    return TenantLLMConfigCipher(get_settings().tenant_llm_fernet_key)


@pytest.fixture
async def tenant() -> AsyncIterator[str]:
    t = await TenantRepository().create(
        name="M5.3 wrapper test", plan=TenantPlan.PRO,
    )
    try:
        yield t.id
    finally:
        sm = get_sessionmaker()
        async with sm() as session:
            await session.execute(delete(Tenant).where(Tenant.id == t.id))
            await session.commit()


async def _count_audit_rows(tenant_id: str) -> int:
    sm = get_sessionmaker()
    async with sm():
        repo = TenantLLMConfigAuditRepository()
        return await repo.count_by_tenant(tenant_id)


async def _list_audit_rows(tenant_id: str) -> list[TenantLLMConfigAuditLog]:
    sm = get_sessionmaker()
    async with sm():
        repo = TenantLLMConfigAuditRepository()
        return await repo.list_by_tenant(tenant_id, limit=100)


# --- happy-path tests ---


async def test_post_creates_audit_rows(tenant: str, cipher: TenantLLMConfigCipher) -> None:
    payload = TenantLLMConfigCreate(
        provider_name="anthropic", api_key="sk-post",
        base_url="https://proxy", model="claude-haiku-4-5",
    )
    sha256 = hashlib.sha256(b"sk-post").hexdigest()
    row = await AdminTenantLLMConfigRepository().upsert(
        tenant_id=tenant, payload=payload, changed_by="admin-1",
        api_key_sha256=sha256,
    )
    assert row.provider_name == "anthropic"
    audit = await _list_audit_rows(tenant)
    fields = {r.field for r in audit}
    assert fields == {"api_key", "base_url", "model", "enabled"}
    api_key_row = next(r for r in audit if r.field == "api_key")
    assert api_key_row.old_value == "sha256:initial"
    assert api_key_row.new_value == f"sha256:{sha256}"
    assert api_key_row.changed_by == "admin-1"


async def test_post_update_writes_old_new_hashes(
    tenant: str, cipher: TenantLLMConfigCipher,
) -> None:
    """Second POST: api_key.old is the prior hash (via latest_field_value
    helper), new is the new hash."""
    first_payload = TenantLLMConfigCreate(
        provider_name="anthropic", api_key="sk-first",
    )
    first_sha = hashlib.sha256(b"sk-first").hexdigest()
    await AdminTenantLLMConfigRepository().upsert(
        tenant_id=tenant, payload=first_payload, changed_by="admin-1",
        api_key_sha256=first_sha,
    )
    second_payload = TenantLLMConfigCreate(
        provider_name="anthropic", api_key="sk-second",
    )
    second_sha = hashlib.sha256(b"sk-second").hexdigest()
    await AdminTenantLLMConfigRepository().upsert(
        tenant_id=tenant, payload=second_payload, changed_by="admin-1",
        api_key_sha256=second_sha,
    )
    audit = await _list_audit_rows(tenant)
    api_key_rows = [r for r in audit if r.field == "api_key"]
    assert len(api_key_rows) == 2
    # Second rotation's old_value is the previous hash (from latest_field_value).
    # Newest-first → index 0 is the second POST.
    assert api_key_rows[0].new_value == f"sha256:{second_sha}"
    assert api_key_rows[0].old_value == f"sha256:{first_sha}"


async def test_post_no_op_rotation_writes_zero_audit_rows(tenant: str) -> None:
    """A POST with the SAME api_key as the existing row must produce
    ZERO audit rows (no-op). The wrapper reads the prior hash via
    latest_field_value and _diff_upsert suppresses the no-op."""
    payload = TenantLLMConfigCreate(
        provider_name="anthropic", api_key="sk-noop",
    )
    sha = hashlib.sha256(b"sk-noop").hexdigest()
    # First POST — writes audit rows
    await AdminTenantLLMConfigRepository().upsert(
        tenant_id=tenant, payload=payload, changed_by="admin-1", api_key_sha256=sha,
    )
    rows_before = await _count_audit_rows(tenant)
    # Second POST with same key — 0 audit rows
    await AdminTenantLLMConfigRepository().upsert(
        tenant_id=tenant, payload=payload, changed_by="admin-1", api_key_sha256=sha,
    )
    rows_after = await _count_audit_rows(tenant)
    assert rows_after == rows_before  # no churn


async def test_patch_one_field_audit_one_row(tenant: str, cipher: TenantLLMConfigCipher) -> None:
    # Seed a row first.
    seed = TenantLLMConfigCreate(provider_name="anthropic", api_key="sk-seed")
    seed_sha = hashlib.sha256(b"sk-seed").hexdigest()
    await AdminTenantLLMConfigRepository().upsert(
        tenant_id=tenant, payload=seed, changed_by="admin-1", api_key_sha256=seed_sha,
    )
    rows_before = await _count_audit_rows(tenant)

    # PATCH only the model.
    payload = TenantLLMConfigUpdate(model="claude-haiku-4-5")
    row = await AdminTenantLLMConfigRepository().update(
        tenant_id=tenant, provider_name="anthropic",
        payload=payload, changed_by="admin-1",
    )
    assert row is not None
    assert row.model == "claude-haiku-4-5"
    rows_after = await _count_audit_rows(tenant)
    assert rows_after == rows_before + 1
    audit = await _list_audit_rows(tenant)
    latest = max(audit, key=lambda r: r.changed_at)
    assert latest.field == "model"
    assert latest.old_value is None  # was None on seed
    assert latest.new_value == "claude-haiku-4-5"


async def test_patch_no_actual_change_no_audit(tenant: str, cipher: TenantLLMConfigCipher) -> None:
    seed = TenantLLMConfigCreate(provider_name="anthropic", api_key="sk-seed", enabled=False)
    seed_sha = hashlib.sha256(b"sk-seed").hexdigest()
    await AdminTenantLLMConfigRepository().upsert(
        tenant_id=tenant, payload=seed, changed_by="admin-1", api_key_sha256=seed_sha,
    )
    rows_before = await _count_audit_rows(tenant)
    payload = TenantLLMConfigUpdate(enabled=False)  # same as current
    await AdminTenantLLMConfigRepository().update(
        tenant_id=tenant, provider_name="anthropic",
        payload=payload, changed_by="admin-1",
    )
    rows_after = await _count_audit_rows(tenant)
    assert rows_after == rows_before  # no audit for no-op


async def test_delete_writes_deleted_marker(tenant: str, cipher: TenantLLMConfigCipher) -> None:
    seed = TenantLLMConfigCreate(provider_name="anthropic", api_key="sk-seed")
    seed_sha = hashlib.sha256(b"sk-seed").hexdigest()
    await AdminTenantLLMConfigRepository().upsert(
        tenant_id=tenant, payload=seed, changed_by="admin-1", api_key_sha256=seed_sha,
    )
    rows_before = await _count_audit_rows(tenant)
    deleted = await AdminTenantLLMConfigRepository().delete(
        tenant_id=tenant, provider_name="anthropic", changed_by="admin-1",
    )
    assert deleted is True
    rows_after = await _count_audit_rows(tenant)
    assert rows_after == rows_before + 1
    audit = await _list_audit_rows(tenant)
    marker = next(r for r in audit if r.field == "__deleted__")
    assert marker.old_value == "anthropic"
    assert marker.new_value is None
    assert marker.changed_by == "admin-1"


async def test_delete_nonexistent_idempotent_no_audit(tenant: str) -> None:
    rows_before = await _count_audit_rows(tenant)
    deleted = await AdminTenantLLMConfigRepository().delete(
        tenant_id=tenant, provider_name="ghost", changed_by="admin-1",
    )
    assert deleted is False
    rows_after = await _count_audit_rows(tenant)
    assert rows_after == rows_before  # 0 audit for no-op


async def test_changed_by_comes_from_param(tenant: str, cipher: TenantLLMConfigCipher) -> None:
    seed = TenantLLMConfigCreate(provider_name="anthropic", api_key="sk")
    sha = hashlib.sha256(b"sk").hexdigest()
    await AdminTenantLLMConfigRepository().upsert(
        tenant_id=tenant, payload=seed, changed_by="user-42", api_key_sha256=sha,
    )
    audit = await _list_audit_rows(tenant)
    assert all(r.changed_by == "user-42" for r in audit)


async def test_api_key_sha256_matches_payload(tenant: str, cipher: TenantLLMConfigCipher) -> None:
    plaintext = "sk-audit-roundtrip"
    payload = TenantLLMConfigCreate(provider_name="anthropic", api_key=plaintext)
    sha = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    await AdminTenantLLMConfigRepository().upsert(
        tenant_id=tenant, payload=payload, changed_by="admin-1", api_key_sha256=sha,
    )
    audit = await _list_audit_rows(tenant)
    api_key_row = next(r for r in audit if r.field == "api_key")
    assert api_key_row.new_value == f"sha256:{sha}"


# --- ⭐ atomicity tests ---


async def test_audit_atomicity_post_rolls_back_on_audit_failure(
    tenant: str, cipher: TenantLLMConfigCipher,
) -> None:
    """⭐ M5.3 core guarantee: if audit write fails, the mutation
    must NOT persist. Verify by monkey-patching insert_many to raise.
    """
    payload = TenantLLMConfigCreate(
        provider_name="anthropic", api_key="sk-atom",
    )
    sha = hashlib.sha256(b"sk-atom").hexdigest()

    sm = get_sessionmaker()
    async with sm() as session:
        rows_before = await session.execute(
            select(TenantLLMConfig).where(
                TenantLLMConfig.tenant_id == tenant,
                TenantLLMConfig.provider_name == "anthropic",
            )
        )
        assert rows_before.scalar_one_or_none() is None

    with patch.object(
        TenantLLMConfigAuditRepository,
        "insert_many",
        side_effect=RuntimeError("simulated audit failure"),
    ):
        with pytest.raises(RuntimeError, match="simulated audit failure"):
            await AdminTenantLLMConfigRepository().upsert(
                tenant_id=tenant, payload=payload, changed_by="admin-1",
                api_key_sha256=sha,
            )

    # The source row must NOT be there.
    async with sm() as session:
        rows_after = await session.execute(
            select(TenantLLMConfig).where(
                TenantLLMConfig.tenant_id == tenant,
                TenantLLMConfig.provider_name == "anthropic",
            )
        )
        assert rows_after.scalar_one_or_none() is None
    # And no audit rows.
    assert await _count_audit_rows(tenant) == 0


async def test_audit_atomicity_patch_rolls_back(
    tenant: str, cipher: TenantLLMConfigCipher,
) -> None:
    seed = TenantLLMConfigCreate(provider_name="anthropic", api_key="sk-seed")
    seed_sha = hashlib.sha256(b"sk-seed").hexdigest()
    await AdminTenantLLMConfigRepository().upsert(
        tenant_id=tenant, payload=seed, changed_by="admin-1", api_key_sha256=seed_sha,
    )
    sm = get_sessionmaker()
    async with sm() as session:
        row_before = (
            await session.execute(
                select(TenantLLMConfig).where(
                    TenantLLMConfig.tenant_id == tenant,
                    TenantLLMConfig.provider_name == "anthropic",
                )
            )
        ).scalar_one()
        assert row_before.model is None

    with patch.object(
        TenantLLMConfigAuditRepository,
        "insert_many",
        side_effect=RuntimeError("simulated audit failure"),
    ):
        with pytest.raises(RuntimeError):
            await AdminTenantLLMConfigRepository().update(
                tenant_id=tenant, provider_name="anthropic",
                payload=TenantLLMConfigUpdate(model="claude-haiku-4-5"),
                changed_by="admin-1",
            )

    # PATCH was rolled back — model still None.
    async with sm() as session:
        row_after = (
            await session.execute(
                select(TenantLLMConfig).where(
                    TenantLLMConfig.tenant_id == tenant,
                    TenantLLMConfig.provider_name == "anthropic",
                )
            )
        ).scalar_one()
        assert row_after.model is None


async def test_audit_atomicity_delete_rolls_back(
    tenant: str, cipher: TenantLLMConfigCipher,
) -> None:
    seed = TenantLLMConfigCreate(provider_name="anthropic", api_key="sk-seed")
    seed_sha = hashlib.sha256(b"sk-seed").hexdigest()
    await AdminTenantLLMConfigRepository().upsert(
        tenant_id=tenant, payload=seed, changed_by="admin-1", api_key_sha256=seed_sha,
    )
    sm = get_sessionmaker()
    async with sm() as session:
        row_before = (
            await session.execute(
                select(TenantLLMConfig).where(
                    TenantLLMConfig.tenant_id == tenant,
                    TenantLLMConfig.provider_name == "anthropic",
                )
            )
        ).scalar_one()
        assert row_before is not None

    with patch.object(
        TenantLLMConfigAuditRepository,
        "insert_many",
        side_effect=RuntimeError("simulated audit failure"),
    ):
        with pytest.raises(RuntimeError):
            await AdminTenantLLMConfigRepository().delete(
                tenant_id=tenant, provider_name="anthropic", changed_by="admin-1",
            )

    # DELETE was rolled back — row still present.
    async with sm() as session:
        row_after = (
            await session.execute(
                select(TenantLLMConfig).where(
                    TenantLLMConfig.tenant_id == tenant,
                    TenantLLMConfig.provider_name == "anthropic",
                )
            )
        ).scalar_one()
        assert row_after is not None


# --- back-compat: inner repo without session=... still works ---


async def test_inner_repo_upsert_without_session(cipher: TenantLLMConfigCipher) -> None:
    tenant = await TenantRepository().create(
        name="M5.3 inner back-compat", plan=TenantPlan.PRO,
    )
    try:
        repo = TenantLLMConfigRepository()
        row = await repo.upsert(
            tenant_id=tenant.id, provider_name="minimax",
            encrypted_api_key=cipher.encrypt("sk-bc"),
            base_url=None, enabled=True,
        )
        assert row.tenant_id == tenant.id
    finally:
        sm = get_sessionmaker()
        async with sm() as session:
            await session.execute(delete(Tenant).where(Tenant.id == tenant.id))
            await session.commit()
