from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core.config import get_settings

# 当前异步上下文中的 tenant_id
_current_tenant_id: ContextVar[str | None] = ContextVar("current_tenant_id", default=None)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_pre_ping=True,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """通用 session 入口(非请求路径,Worker 用)"""
    sm = get_sessionmaker()
    async with sm() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def set_tenant_contextvar(tenant_id: str) -> Token[str | None]:
    """Set the current request's tenant_id. Returns a Token for reset."""
    return _current_tenant_id.set(tenant_id)


def reset_tenant_contextvar(token: Token[str | None]) -> None:
    """Reset the tenant_id ContextVar to its prior value. Use in a finally block."""
    _current_tenant_id.reset(token)


def get_tenant_contextvar() -> str | None:
    return _current_tenant_id.get()


async def set_tenant_context(session: AsyncSession, tenant_id: str) -> None:
    """在事务中设置 Postgres 会话变量,供 RLS 策略使用"""
    # 必须参数化,防止注入
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": tenant_id},
    )


async def reset_tenant_context(session: AsyncSession) -> None:
    await session.execute(text("SELECT set_config('app.tenant_id', '', true)"))


def reset_engine() -> None:
    """Clear the cached engine and dispose its connection pool. For test isolation only."""
    global _engine
    if _engine is not None:
        # sync dispose is fine; we're in a sync function called from a fixture
        _engine.sync_engine.dispose()
    _engine = None


def reset_sessionmaker() -> None:
    """Clear the cached sessionmaker. For test isolation only."""
    global _sessionmaker
    _sessionmaker = None