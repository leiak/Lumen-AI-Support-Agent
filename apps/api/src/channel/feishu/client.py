"""Feishu OpenAPI HTTP client — tenant_access_token + send text message."""
import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Final

import httpx

from channel.feishu.exceptions import FeishuAPIError

FEISHU_BASE_URL: Final[str] = "https://open.feishu.cn"
TOKEN_EXPIRY_BUFFER_SECONDS: Final[int] = 60  # safety margin before token TTL


@dataclass(frozen=True, repr=False)
class FeishuToken:
    access_token: str
    expires_at: float  # absolute time.time()

    def __repr__(self) -> str:
        return f"FeishuToken(access_token='***', expires_at={self.expires_at})"


class FeishuOpenAPIClient:
    def __init__(self, *, base_url: str = FEISHU_BASE_URL, timeout: float = 10.0) -> None:
        """Create a client with an in-memory per-app_id token cache.

        Args:
            base_url: Feishu OpenAPI base URL (override for testing).
            timeout: HTTP request timeout in seconds.
        """
        self._base_url = base_url
        self._timeout = timeout
        self._token_cache: dict[str, FeishuToken] = {}
        self._token_lock = asyncio.Lock()

    async def get_tenant_access_token(self, *, app_id: str, app_secret: str) -> str:
        cached = self._token_cache.get(app_id)
        # TODO(M2): use monotonic clock to avoid NTP backward-jump edge cases.
        if cached and cached.expires_at > time.time() + TOKEN_EXPIRY_BUFFER_SECONDS:
            return cached.access_token
        async with self._token_lock:
            cached = self._token_cache.get(app_id)
            if cached and cached.expires_at > time.time() + TOKEN_EXPIRY_BUFFER_SECONDS:
                return cached.access_token
            # M1: one short-lived client per call. Singleton long-lived client is M2.
            async with httpx.AsyncClient(timeout=self._timeout) as http:
                resp = await http.post(
                    f"{self._base_url}/open-apis/auth/v3/tenant_access_token/internal",
                    json={"app_id": app_id, "app_secret": app_secret},
                )
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
                if data.get("code") != 0:
                    raise FeishuAPIError(
                        f"tenant_access_token: code={data.get('code')} msg={data.get('msg')}"
                    )
                token = str(data["tenant_access_token"])
                expire_seconds = int(data.get("expire", 7200))
                self._token_cache[app_id] = FeishuToken(
                    access_token=token,
                    expires_at=time.time() + expire_seconds,
                )
                return token

    async def send_text_message(
        self,
        *,
        app_id: str,
        app_secret: str,
        receive_id: str,
        text: str,
    ) -> dict[str, Any]:
        access_token = await self.get_tenant_access_token(
            app_id=app_id, app_secret=app_secret
        )
        # M1: one short-lived client per call. Singleton long-lived client is M2.
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            resp = await http.post(
                f"{self._base_url}/open-apis/im/v1/messages",
                params={"receive_id_type": "open_id"},
                headers={"Authorization": f"Bearer {access_token}"},
                json={
                    "receive_id": receive_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": text}),
                },
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            if data.get("code") != 0:
                raise FeishuAPIError(
                    f"send_text_message: code={data.get('code')} msg={data.get('msg')}"
                )
            return data

    def clear_token_cache(self) -> None:
        """Test helper — clear in-memory token cache between tests."""
        self._token_cache.clear()