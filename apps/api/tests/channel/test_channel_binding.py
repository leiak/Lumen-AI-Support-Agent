"""Tests for the ChannelBinding ORM model."""
import pytest
from sqlalchemy import select

from channel.enums import ChannelType
from channel.models import Channel, ChannelBinding
from channel.repository import ChannelRepository
from core.database import get_session
from core.id_gen import new_id
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository


def test_channel_binding_table_metadata() -> None:
    """Pure-Python test: verify model columns and unique constraint."""
    constraints = {c.name for c in ChannelBinding.__table__.constraints}
    assert "uq_channel_external_conv" in constraints
    column_names = {c.name for c in ChannelBinding.__table__.columns}
    expected = {
        "id",
        "channel_id",
        "external_conversation_id",
        "external_user_id",
        "internal_conversation_id",
        "tenant_id",
        "created_at",
        "updated_at",
    }
    assert expected <= column_names
    assert ChannelBinding.__tablename__ == "channel_bindings"


async def _seed_channel() -> tuple[str, str]:
    """Create a tenant + channel; return (tenant_id, channel_id)."""
    tenant_repo = TenantRepository()
    channel_repo = ChannelRepository()
    tenant = await tenant_repo.create(name="Acme Binding Test", plan=TenantPlan.FREE)
    channel = await channel_repo.create(
        tenant_id=tenant.id,
        type=ChannelType.WEB,
        name="Test Widget",
        credentials_encrypted="{}",
    )
    return tenant.id, channel.id


async def _cleanup(tenant_id: str) -> None:
    async with get_session() as session:
        t = await session.get(Tenant, tenant_id)
        if t:
            await session.delete(t)
            await session.commit()


@pytest.mark.integration
async def test_channel_binding_create_and_get() -> None:
    tenant_id, channel_id = await _seed_channel()
    try:
        binding_id = new_id()
        async with get_session() as session:
            binding = ChannelBinding(
                id=binding_id,
                channel_id=channel_id,
                external_conversation_id="widget-sess-001",
                external_user_id="user-abc",
                internal_conversation_id=new_id(),
                tenant_id=tenant_id,
            )
            session.add(binding)
            await session.flush()
            await session.commit()

        async with get_session() as session:
            result = await session.execute(
                select(ChannelBinding).where(ChannelBinding.id == binding_id)
            )
            loaded = result.scalar_one()
            assert loaded.channel_id == channel_id
            assert loaded.external_conversation_id == "widget-sess-001"
            assert loaded.external_user_id == "user-abc"
            assert loaded.tenant_id == tenant_id
    finally:
        await _cleanup(tenant_id)


@pytest.mark.integration
async def test_channel_binding_unique_constraint() -> None:
    """Same (channel_id, external_conversation_id) twice → IntegrityError."""
    from sqlalchemy.exc import IntegrityError

    tenant_id, channel_id = await _seed_channel()
    ext_conv_id = "widget-sess-dup"
    try:
        # First binding succeeds
        async with get_session() as session:
            session.add(
                ChannelBinding(
                    id=new_id(),
                    channel_id=channel_id,
                    external_conversation_id=ext_conv_id,
                    external_user_id="u1",
                    internal_conversation_id=new_id(),
                    tenant_id=tenant_id,
                )
            )
            await session.commit()

        # Second binding with same (channel_id, external_conversation_id) fails
        async with get_session() as session:
            session.add(
                ChannelBinding(
                    id=new_id(),
                    channel_id=channel_id,
                    external_conversation_id=ext_conv_id,
                    external_user_id="u2",
                    internal_conversation_id=new_id(),
                    tenant_id=tenant_id,
                )
            )
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()
    finally:
        await _cleanup(tenant_id)


@pytest.mark.integration
async def test_channel_binding_cascade_delete_with_channel() -> None:
    """Deleting the channel should cascade-delete its bindings."""
    tenant_id, channel_id = await _seed_channel()
    try:
        binding_id = new_id()
        async with get_session() as session:
            session.add(
                ChannelBinding(
                    id=binding_id,
                    channel_id=channel_id,
                    external_conversation_id="widget-sess-cascade",
                    external_user_id="u",
                    internal_conversation_id=new_id(),
                    tenant_id=tenant_id,
                )
            )
            await session.commit()

        # Delete the channel; binding should cascade
        async with get_session() as session:
            ch = await session.get(Channel, channel_id)
            await session.delete(ch)
            await session.commit()

        async with get_session() as session:
            result = await session.execute(
                select(ChannelBinding).where(ChannelBinding.id == binding_id)
            )
            assert result.scalar_one_or_none() is None
    finally:
        # tenant cascade covers channels+bindings; no-op if already deleted
        await _cleanup(tenant_id)