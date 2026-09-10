"""Tests for the Channel model and repository."""
import pytest

from channel.enums import ChannelStatus, ChannelType
from channel.models import Channel
from channel.repository import ChannelRepository
from core.database import get_session
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository


async def _seed_tenant() -> str:
    repo = TenantRepository()
    t = await repo.create(name="Acme Channel Test", plan=TenantPlan.FREE)
    return t.id


async def _delete_tenant(tenant_id: str) -> None:
    async with get_session() as session:
        t = await session.get(Tenant, tenant_id)
        if t:
            await session.delete(t)
            await session.commit()


def test_channel_table_metadata() -> None:
    """Pure-Python test (no DB): verify Channel model columns."""
    column_names = {c.name for c in Channel.__table__.columns}
    expected = {
        "id",
        "tenant_id",
        "type",
        "name",
        "credentials_encrypted",
        "status",
        "config_json",
        "created_at",
        "updated_at",
    }
    assert expected <= column_names
    assert Channel.__tablename__ == "channels"


@pytest.mark.integration
async def test_channel_repository_create_and_get() -> None:
    tenant_id = await _seed_tenant()
    try:
        repo = ChannelRepository()
        channel = await repo.create(
            tenant_id=tenant_id,
            type=ChannelType.FEISHU,
            name="Acme Feishu Bot",
            credentials_encrypted='{"app_id":"cli_xxx","app_secret":"yyy"}',
            config_json={"webhook_url": "https://example.com/webhook"},
        )
        assert channel.id
        assert len(channel.id) == 26
        assert channel.type == ChannelType.FEISHU
        assert channel.status == ChannelStatus.ACTIVE

        loaded = await repo.get_by_id(channel.id)
        assert loaded is not None
        assert loaded.name == "Acme Feishu Bot"
        assert loaded.credentials_encrypted == '{"app_id":"cli_xxx","app_secret":"yyy"}'
        assert loaded.config_json == {"webhook_url": "https://example.com/webhook"}
    finally:
        await _delete_tenant(tenant_id)


@pytest.mark.integration
async def test_channel_repository_list_for_tenant() -> None:
    tenant_id = await _seed_tenant()
    try:
        repo = ChannelRepository()
        await repo.create(
            tenant_id=tenant_id,
            type=ChannelType.FEISHU,
            name="Bot 1",
            credentials_encrypted="{}",
        )
        await repo.create(
            tenant_id=tenant_id,
            type=ChannelType.WEB,
            name="Widget",
            credentials_encrypted="{}",
        )

        channels = await repo.list_for_tenant(tenant_id)
        assert len(channels) == 2
        names = {c.name for c in channels}
        assert names == {"Bot 1", "Widget"}
    finally:
        await _delete_tenant(tenant_id)


@pytest.mark.integration
async def test_channel_repository_get_by_id_missing() -> None:
    repo = ChannelRepository()
    result = await repo.get_by_id("01ARZ3NDEKTSV4RRFFQ69G5FAV")  # a valid ULID, doesn't exist
    assert result is None


@pytest.mark.integration
async def test_channel_cascade_delete_with_tenant() -> None:
    """Deleting the tenant should cascade-delete its channels."""
    tenant_id = await _seed_tenant()
    repo = ChannelRepository()
    channel = await repo.create(
        tenant_id=tenant_id,
        type=ChannelType.WEB,
        name="Will be cascaded",
        credentials_encrypted="{}",
    )
    channel_id = channel.id

    await _delete_tenant(tenant_id)

    # Channel should be gone via CASCADE
    loaded = await repo.get_by_id(channel_id)
    assert loaded is None