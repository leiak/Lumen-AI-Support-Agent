"""Tests for ChannelRepository.get_by_app_id (FEISHU-only app_id lookup)."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from channel.enums import ChannelStatus, ChannelType
from channel.models import Channel
from channel.repository import ChannelRepository
from core.database import get_session
from core.id_gen import new_id
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository

# --- Pure-Python (no DB) tests using a stubbed session ----------------------


class _StubScalarResult:
    """Minimal stand-in for ``sqlalchemy`` ScalarResult returning a fixed list."""

    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def scalars(self) -> _StubScalarResult:
        return self

    def all(self) -> list[Any]:
        return self._items


class _StubSession:
    def __init__(self, channels: list[Channel]) -> None:
        self._channels = channels

    async def execute(self, _stmt: Any) -> _StubScalarResult:
        # Only FEISHU channels should be returned by get_by_app_id's query.
        feishu = [c for c in self._channels if c.type == ChannelType.FEISHU]
        return _StubScalarResult(feishu)


@pytest.fixture
def stub_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch get_session to return a context-manager yielding a stub session."""

    def _factory(channels: list[Channel]) -> Any:
        @asynccontextmanager
        async def _cm():
            yield _StubSession(channels)

        return _cm()

    monkeypatch.setattr("channel.repository.get_session", lambda: _factory([]))


def _channel(
    *,
    type: ChannelType = ChannelType.FEISHU,
    creds: str = "{}",
    tenant_id: str | None = None,
    id: str | None = None,
) -> Channel:
    return Channel(
        id=id or new_id(),
        tenant_id=tenant_id or new_id(),
        type=type,
        name="x",
        status=ChannelStatus.ACTIVE,
        credentials_encrypted=creds,
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_get_by_app_id_returns_matching_feishu_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_by_app_id scans FEISHU channels and returns the one whose credentials.app_id matches."""
    target = _channel(creds=json.dumps({"app_id": "cli_match", "app_secret": "s"}))
    other = _channel(creds=json.dumps({"app_id": "cli_other", "app_secret": "s"}))
    web_ch = _channel(type=ChannelType.WEB, creds=json.dumps({"app_id": "cli_match"}))

    channels = [target, other, web_ch]

    @asynccontextmanager
    async def _cm():
        yield _StubSession(channels)

    monkeypatch.setattr("channel.repository.get_session", lambda: _cm())

    repo = ChannelRepository()
    result = await repo.get_by_app_id("cli_match")
    assert result is target


@pytest.mark.asyncio
async def test_get_by_app_id_returns_none_when_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No matching channel -> None."""
    a = _channel(creds=json.dumps({"app_id": "cli_a"}))
    b = _channel(creds=json.dumps({"app_id": "cli_b"}))

    @asynccontextmanager
    async def _cm():
        yield _StubSession([a, b])

    monkeypatch.setattr("channel.repository.get_session", lambda: _cm())

    repo = ChannelRepository()
    result = await repo.get_by_app_id("cli_missing")
    assert result is None


@pytest.mark.asyncio
async def test_get_by_app_id_skips_malformed_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A channel with non-JSON credentials_encrypted is skipped, not crashed on."""
    bad = _channel(creds="not-valid-json{")
    good = _channel(creds=json.dumps({"app_id": "cli_target"}))

    @asynccontextmanager
    async def _cm():
        yield _StubSession([bad, good])

    monkeypatch.setattr("channel.repository.get_session", lambda: _cm())

    repo = ChannelRepository()
    result = await repo.get_by_app_id("cli_target")
    assert result is good


@pytest.mark.asyncio
async def test_get_by_app_id_ignores_web_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A WEB channel with the same app_id in its creds is NOT returned."""
    web = _channel(type=ChannelType.WEB, creds=json.dumps({"app_id": "cli_match"}))

    @asynccontextmanager
    async def _cm():
        yield _StubSession([web])

    monkeypatch.setattr("channel.repository.get_session", lambda: _cm())

    repo = ChannelRepository()
    result = await repo.get_by_app_id("cli_match")
    assert result is None


@pytest.mark.asyncio
async def test_get_by_app_id_skips_non_dict_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """credentials_encrypted that is valid JSON but not a dict is skipped."""
    bad = _channel(creds=json.dumps(["not", "a", "dict"]))
    good = _channel(creds=json.dumps({"app_id": "cli_hit"}))

    @asynccontextmanager
    async def _cm():
        yield _StubSession([bad, good])

    monkeypatch.setattr("channel.repository.get_session", lambda: _cm())

    repo = ChannelRepository()
    result = await repo.get_by_app_id("cli_hit")
    assert result is good


# --- Integration test (live DB) ---------------------------------------------


async def _seed_tenant() -> str:
    repo = TenantRepository()
    t = await repo.create(name="Acme AppID Test", plan=TenantPlan.FREE)
    return t.id


async def _delete_tenant(tenant_id: str) -> None:
    async with get_session() as session:
        t = await session.get(Tenant, tenant_id)
        if t:
            await session.delete(t)
            await session.commit()


@pytest.mark.integration
async def test_channel_repo_get_by_app_id_integration() -> None:
    """Live DB: get_by_app_id finds the seeded channel."""
    tenant_id = await _seed_tenant()
    try:
        repo = ChannelRepository()
        await repo.create(
            tenant_id=tenant_id,
            type=ChannelType.FEISHU,
            name="Bot A",
            credentials_encrypted=json.dumps({"app_id": "cli_int_a"}),
        )
        await repo.create(
            tenant_id=tenant_id,
            type=ChannelType.FEISHU,
            name="Bot B",
            credentials_encrypted=json.dumps({"app_id": "cli_int_b"}),
        )
        # WEB with the same app_id should NOT match.
        await repo.create(
            tenant_id=tenant_id,
            type=ChannelType.WEB,
            name="Widget",
            credentials_encrypted=json.dumps({"app_id": "cli_int_a"}),
        )

        hit = await repo.get_by_app_id("cli_int_b")
        assert hit is not None
        assert hit.name == "Bot B"

        # The FEISHU "Bot A" must be returned, NOT the WEB channel.
        hit_a = await repo.get_by_app_id("cli_int_a")
        assert hit_a is not None
        assert hit_a.name == "Bot A"
        assert hit_a.type == ChannelType.FEISHU

        miss = await repo.get_by_app_id("cli_int_missing")
        assert miss is None
    finally:
        await _delete_tenant(tenant_id)
