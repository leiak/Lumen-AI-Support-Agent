"""OpenAI-compatible chat-completions adapter.

Targets the OpenAI /chat/completions API. Also the base for `OllamaProvider`
since Ollama exposes the same endpoint at /v1/chat/completions.
"""
from collections.abc import AsyncIterator
from typing import Any

import httpx

from llm_client.exceptions import (
    InvalidRequest,
    OutputInvalid,
    ProviderUnavailable,
    RateLimited,
)
from llm_client.providers.base import BaseProvider
from llm_client.types import ChatRequest, ChatResponse


class OpenAIProvider(BaseProvider):
    """Adapter for OpenAI's /chat/completions API and compatible servers.

    Args:
        api_key: API key. Required when targeting api.openai.com; optional
            for local/self-hosted servers.
        model: Default model ID (e.g. gpt-4o).
        base_url: API root. Default: https://api.openai.com/v1
    """

    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 30.0,
    ) -> None:
        if not api_key and "openai.com" in base_url:
            raise ValueError("API key required for OpenAI")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}" if api_key else "",
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        """Close the underlying httpx client."""
        await self._client.aclose()

    async def chat(self, request: ChatRequest) -> ChatResponse:
        body: dict[str, Any] = {
            "model": request.model,
            "messages": [m.model_dump(exclude_none=True) for m in request.messages],
            "temperature": request.temperature,
        }
        if request.max_tokens:
            body["max_tokens"] = request.max_tokens
        if request.stop:
            body["stop"] = request.stop
        if request.tools:
            body["tools"] = request.tools
        if request.tool_choice:
            body["tool_choice"] = request.tool_choice

        try:
            resp = await self._client.post("/chat/completions", json=body)
        except httpx.HTTPError as e:
            raise ProviderUnavailable(f"OpenAI network error: {e}") from e

        if resp.status_code == 429:
            raise RateLimited("OpenAI rate limited")
        if 400 <= resp.status_code < 500:
            raise InvalidRequest(f"OpenAI 4xx: {resp.text[:200]}")
        if resp.status_code >= 500:
            raise ProviderUnavailable(f"OpenAI 5xx: {resp.status_code}")

        try:
            data = resp.json()
        except Exception as e:
            raise OutputInvalid(f"OpenAI non-JSON: {resp.text[:200]}") from e

        choice = data["choices"][0]
        msg = choice["message"]
        return ChatResponse(
            content=msg.get("content", "") or "",
            model=data.get("model", request.model),
            prompt_tokens=data.get("usage", {}).get("prompt_tokens", 0),
            completion_tokens=data.get("usage", {}).get("completion_tokens", 0),
            finish_reason=choice.get("finish_reason", "stop"),
            tool_calls=msg.get("tool_calls"),
            raw=data,
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        # M1 defer streaming to M2
        raise NotImplementedError("OpenAI streaming pending M2")
        yield ""  # unreachable; needed for AsyncIterator typing
