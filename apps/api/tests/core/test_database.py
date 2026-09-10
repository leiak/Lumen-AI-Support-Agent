import pytest
from sqlalchemy import text

from core.database import get_session, set_tenant_context


@pytest.mark.integration
async def test_set_tenant_context_sets_session_var() -> None:
    """验证 set_tenant_context 真的发出 SET LOCAL app.tenant_id 命令"""
    async with get_session() as session:
        # 第一次设置
        await set_tenant_context(session, "tenant-a")
        result = await session.execute(text("SELECT current_setting('app.tenant_id', true)"))
        assert result.scalar() == "tenant-a"

        # 切换
        await set_tenant_context(session, "tenant-b")
        result = await session.execute(text("SELECT current_setting('app.tenant_id', true)"))
        assert result.scalar() == "tenant-b"