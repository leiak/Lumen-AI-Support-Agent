"""Tests for Feishu OpenAPI client (outbound)."""
import pytest

from channel.feishu.client import FeishuOpenAPIClient
from channel.feishu.exceptions import FeishuAPIError

FEISHU_BASE = "https://open.feishu.cn"


@pytest.fixture
def client() -> FeishuOpenAPIClient:
    return FeishuOpenAPIClient(base_url=FEISHU_BASE, timeout=5.0)


@pytest.mark.asyncio
async def test_get_tenant_access_token_success(client, httpx_mock) -> None:
    """Happy path: token endpoint returns 200 + code 0."""
    httpx_mock.add_response(
        method="POST",
        url=f"{FEISHU_BASE}/open-apis/auth/v3/tenant_access_token/internal",
        json={
            "code": 0,
            "msg": "ok",
            "tenant_access_token": "t-abc123",
            "expire": 7200,
        },
    )
    token = await client.get_tenant_access_token(
        app_id="cli_app_1",
        app_secret="secret_1",  # noqa: S106
    )
    assert token == "t-abc123"  # noqa: S105


@pytest.mark.asyncio
async def test_get_tenant_access_token_caches(client, httpx_mock) -> None:
    """A cached token (still fresh) should not trigger a new HTTP call."""
    httpx_mock.add_response(
        url=f"{FEISHU_BASE}/open-apis/auth/v3/tenant_access_token/internal",
        json={
            "code": 0,
            "msg": "ok",
            "tenant_access_token": "t-first",
            "expire": 7200,
        },
    )
    first = await client.get_tenant_access_token(
        app_id="cli_app_1",
        app_secret="secret_1",  # noqa: S106
    )
    second = await client.get_tenant_access_token(
        app_id="cli_app_1",
        app_secret="secret_1",  # noqa: S106
    )
    assert first == second == "t-first"
    # httpx_mock should report only 1 request was made (token cached).
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_get_tenant_access_token_error_raises(client, httpx_mock) -> None:
    """Non-zero code should raise FeishuAPIError."""
    httpx_mock.add_response(
        url=f"{FEISHU_BASE}/open-apis/auth/v3/tenant_access_token/internal",
        json={"code": 10003, "msg": "invalid app_secret"},
    )
    with pytest.raises(FeishuAPIError, match="invalid app_secret"):
        await client.get_tenant_access_token(
            app_id="cli_app_1",
            app_secret="wrong",  # noqa: S106
        )


@pytest.mark.asyncio
async def test_send_text_message_success(client, httpx_mock) -> None:
    """Happy path: send a text message."""
    httpx_mock.add_response(
        url=f"{FEISHU_BASE}/open-apis/auth/v3/tenant_access_token/internal",
        json={
            "code": 0,
            "msg": "ok",
            "tenant_access_token": "t-abc",
            "expire": 7200,
        },
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{FEISHU_BASE}/open-apis/im/v1/messages?receive_id_type=open_id",
        json={"code": 0, "msg": "ok", "data": {"message_id": "om_out_1"}},
    )
    result = await client.send_text_message(
        app_id="cli_app_1",
        app_secret="secret_1",  # noqa: S106
        receive_id="ou_user_1",
        text="hello back",
    )
    assert result["code"] == 0
    # Two requests: token + send.
    assert len(httpx_mock.get_requests()) == 2


@pytest.mark.asyncio
async def test_send_text_message_api_error_raises(client, httpx_mock) -> None:
    httpx_mock.add_response(
        url=f"{FEISHU_BASE}/open-apis/auth/v3/tenant_access_token/internal",
        json={
            "code": 0,
            "msg": "ok",
            "tenant_access_token": "t-abc",
            "expire": 7200,
        },
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{FEISHU_BASE}/open-apis/im/v1/messages?receive_id_type=open_id",
        json={"code": 230001, "msg": "user not found"},
    )
    with pytest.raises(FeishuAPIError, match="user not found"):
        await client.send_text_message(
            app_id="cli_app_1",
            app_secret="secret_1",  # noqa: S106
            receive_id="ou_bogus",
            text="hi",
        )


@pytest.mark.asyncio
async def test_send_text_message_uses_cached_token(client, httpx_mock) -> None:
    """Sending multiple messages should only fetch the token once."""
    httpx_mock.add_response(
        url=f"{FEISHU_BASE}/open-apis/auth/v3/tenant_access_token/internal",
        json={
            "code": 0,
            "msg": "ok",
            "tenant_access_token": "t-abc",
            "expire": 7200,
        },
    )
    for i in range(3):
        httpx_mock.add_response(
            method="POST",
            url=f"{FEISHU_BASE}/open-apis/im/v1/messages?receive_id_type=open_id",
            json={"code": 0, "msg": "ok", "data": {"message_id": f"om_{i}"}},
        )
    for i in range(3):
        await client.send_text_message(
            app_id="cli_app_1",
            app_secret="secret_1",  # noqa: S106
            receive_id=f"ou_user_{i}",
            text=f"msg {i}",
        )
    # 1 token + 3 messages = 4 requests
    assert len(httpx_mock.get_requests()) == 4


@pytest.mark.asyncio
async def test_clear_token_cache(client, httpx_mock) -> None:
    """clear_token_cache() should force a new token fetch."""
    httpx_mock.add_response(
        url=f"{FEISHU_BASE}/open-apis/auth/v3/tenant_access_token/internal",
        json={
            "code": 0,
            "msg": "ok",
            "tenant_access_token": "t-first",
            "expire": 7200,
        },
    )
    httpx_mock.add_response(
        url=f"{FEISHU_BASE}/open-apis/auth/v3/tenant_access_token/internal",
        json={
            "code": 0,
            "msg": "ok",
            "tenant_access_token": "t-second",
            "expire": 7200,
        },
    )
    first = await client.get_tenant_access_token(
        app_id="cli_app_1",
        app_secret="secret_1",  # noqa: S106
    )
    client.clear_token_cache()
    second = await client.get_tenant_access_token(
        app_id="cli_app_1",
        app_secret="secret_1",  # noqa: S106
    )
    assert first == "t-first"
    assert second == "t-second"
    assert len(httpx_mock.get_requests()) == 2