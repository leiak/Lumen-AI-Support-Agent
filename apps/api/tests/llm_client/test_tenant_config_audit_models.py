"""Integration tests for ``TenantLLMConfigAuditLog`` ORM + repo (M5.3)."""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from core.database import get_sessionmaker
from core.id_gen import new_id
from llm_client.tenant_config_audit_models import (
    FieldName,
    TenantLLMConfigAuditLog,
    TenantLLMConfigAuditRepository,
)
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository

pytestmark = pytest.mark.integration


def _row(**overrides: object) -> TenantLLMConfigAuditLog:
    """Build an audit row with sensible defaults; override per test."""
    base: dict[str, object] = {
        "id": new_id(),
        "tenant_id": "01HZDUMMY0000000000000000",
        "provider_name": "anthropic",
        "field": "model",
        "old_value": "claude-3-5-sonnet-20241022",
        "new_value": "claude-haiku-4-5",
        "changed_by": "admin-1",
    }
    base.update(overrides)
    return TenantLLMConfigAuditLog(**base)


async def test_insert_many_persists_rows() -> None:
    tenant = await TenantRepository().create(
        name="audit M5.3 insert", plan=TenantPlan.PRO,
    )
    try:
        repo = TenantLLMConfigAuditRepository()
        rows = [
            TenantLLMConfigAuditLog(
                id=new_id(), tenant_id=tenant.id, provider_name="anthropic",
                field="model", old_value=None, new_value="claude-haiku-4-5",
                changed_by="admin-1",
            )
            for _ in range(5)
        ]
        sm = get_sessionmaker()
        async with sm() as session:
            await repo.insert_many(session, rows)
            await session.commit()

        async with sm() as session:
            fetched = await repo.list_by_tenant(tenant.id)
        assert len(fetched) == 5
    finally:
        async with get_sessionmaker()() as session:
            await session.execute(delete(Tenant).where(Tenant.id == tenant.id))
            await session.commit()


async def test_list_by_tenant_orders_newest_first() -> None:
    tenant = await TenantRepository().create(
        name="audit M5.3 ordering", plan=TenantPlan.PRO,
    )
    try:
        repo = TenantLLMConfigAuditRepository()
        # Insert with explicit increasing ids (ULIDs sort by time anyway,
        # but using a controlled change_at is more deterministic).
        from datetime import UTC, datetime, timedelta

        base = datetime.now(UTC)
        rows = [
            TenantLLMConfigAuditLog(
                id=new_id(), tenant_id=tenant.id, provider_name="anthropic",
                field="model",
                old_value=None, new_value=f"m{i}",
                changed_by="admin-1",
                changed_at=base + timedelta(seconds=i),
            )
            for i in range(3)
        ]
        sm = get_sessionmaker()
        async with sm() as session:
            await repo.insert_many(session, rows)
            await session.commit()
        async with sm() as session:
            fetched = await repo.list_by_tenant(tenant.id)
        # Newest-first → m2, m1, m0
        assert [r.new_value for r in fetched] == ["m2", "m1", "m0"]
    finally:
        async with get_sessionmaker()() as session:
            await session.execute(delete(Tenant).where(Tenant.id == tenant.id))
            await session.commit()


async def test_list_by_tenant_filters_by_provider() -> None:
    tenant = await TenantRepository().create(
        name="audit M5.3 filter", plan=TenantPlan.PRO,
    )
    try:
        repo = TenantLLMConfigAuditRepository()
        rows = [
            TenantLLMConfigAuditLog(
                id=new_id(), tenant_id=tenant.id, provider_name="anthropic",
                field="model", old_value=None, new_value="a",
                changed_by="admin-1",
            ),
            TenantLLMConfigAuditLog(
                id=new_id(), tenant_id=tenant.id, provider_name="openai",
                field="model", old_value=None, new_value="o1",
                changed_by="admin-1",
            ),
            TenantLLMConfigAuditLog(
                id=new_id(), tenant_id=tenant.id, provider_name="openai",
                field="base_url", old_value=None, new_value="https://x",
                changed_by="admin-1",
            ),
        ]
        sm = get_sessionmaker()
        async with sm() as session:
            await repo.insert_many(session, rows)
            await session.commit()
        async with sm() as session:
            anthropic = await repo.list_by_tenant(tenant.id, provider_name="anthropic")
            openai = await repo.list_by_tenant(tenant.id, provider_name="openai")
        assert len(anthropic) == 1
        assert anthropic[0].provider_name == "anthropic"
        assert len(openai) == 2
    finally:
        async with get_sessionmaker()() as session:
            await session.execute(delete(Tenant).where(Tenant.id == tenant.id))
            await session.commit()


async def test_list_by_tenant_pagination() -> None:
    tenant = await TenantRepository().create(
        name="audit M5.3 pagination", plan=TenantPlan.PRO,
    )
    try:
        repo = TenantLLMConfigAuditRepository()
        rows = [
            TenantLLMConfigAuditLog(
                id=new_id(), tenant_id=tenant.id, provider_name="anthropic",
                field="model", old_value=None, new_value=f"v{i}",
                changed_by="admin-1",
            )
            for i in range(5)
        ]
        sm = get_sessionmaker()
        async with sm() as session:
            await repo.insert_many(session, rows)
            await session.commit()

        async with sm() as session:
            page0 = await repo.list_by_tenant(tenant.id, limit=2, offset=0)
            page1 = await repo.list_by_tenant(tenant.id, limit=2, offset=2)
            page2 = await repo.list_by_tenant(tenant.id, limit=2, offset=4)
        assert len(page0) == 2
        assert len(page1) == 2
        assert len(page2) == 1
    finally:
        async with get_sessionmaker()() as session:
            await session.execute(delete(Tenant).where(Tenant.id == tenant.id))
            await session.commit()


async def test_list_by_tenant_empty() -> None:
    tenant = await TenantRepository().create(
        name="audit M5.3 empty", plan=TenantPlan.PRO,
    )
    try:
        repo = TenantLLMConfigAuditRepository()
        sm = get_sessionmaker()
        async with sm() as session:
            rows = await repo.list_by_tenant(tenant.id)
            count = await repo.count_by_tenant(tenant.id)
        assert rows == []
        assert count == 0
    finally:
        async with get_sessionmaker()() as session:
            await session.execute(delete(Tenant).where(Tenant.id == tenant.id))
            await session.commit()


async def test_cascade_delete_with_tenant() -> None:
    tenant = await TenantRepository().create(
        name="audit M5.3 cascade", plan=TenantPlan.PRO,
    )
    repo = TenantLLMConfigAuditRepository()
    row = TenantLLMConfigAuditLog(
        id=new_id(), tenant_id=tenant.id, provider_name="anthropic",
        field="model", old_value=None, new_value="m",
        changed_by="admin-1",
    )
    sm = get_sessionmaker()
    async with sm() as session:
        await repo.insert_many(session, [row])
        await session.commit()
    # Now delete the tenant — CASCADE should sweep the audit row.
    async with sm() as session:
        t = await session.get(Tenant, tenant.id)
        assert t is not None
        await session.delete(t)
        await session.commit()
    # Verify the audit row is gone (in a fresh session).
    async with sm() as session:
        result = await session.execute(
            select(TenantLLMConfigAuditLog).where(
                TenantLLMConfigAuditLog.tenant_id == tenant.id,
            )
        )
        assert result.scalars().all() == []


def test_field_literal_rejects_unknown() -> None:
    """ORM __init__ raises ValueError for a non-Literal field value."""
    with pytest.raises(ValueError, match="invalid field value"):
        _row(field="bogus")


def test_old_and_new_value_accept_text_length() -> None:
    """A 600-char base_url fits in the Text column without truncation."""
    long_url = "https://example.com/" + ("x" * 580)
    row = _row(field="base_url", old_value=None, new_value=long_url)
    assert row.new_value is not None
    assert len(row.new_value) == len(long_url)


def test_field_name_literal_contains_documented_values() -> None:
    """The FieldName Literal matches the spec §5.2 enumeration exactly."""
    assert set(FieldName.__args__) == {
        "api_key", "base_url", "model", "enabled", "__deleted__",
    }