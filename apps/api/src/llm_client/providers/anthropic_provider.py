"""Anthropic Claude provider adapter.

Translates our provider-agnostic ChatRequest/ChatResponse to Anthropic's
native /v1/messages API format. Maps HTTP errors to our exception hierarchy.
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
from llm_client.types import ChatMessage, ChatRequest, ChatResponse, MessageRole

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


def _convert_messages(
    messages: list[ChatMessage],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Anthropic takes the system message as a separate top-level field.

    Returns (system, converted_messages) where:
    - system: concatenation of all SYSTEM messages (joined by blank line), or None
    - converted_messages: the remaining USER/ASSISTANT/TOOL messages with
      role as a string and content as a string
    """
    system: str | None = None
    converted: list[dict[str, Any]] = []
    for m in messages:
        if m.role == MessageRole.SYSTEM:
            system = m.content if system is None else f"{system}\n\n{m.content}"
        else:
            converted.append({"role": m.role.value, "content": m.content})
    return system, converted


class AnthropicProvider(BaseProvider):
    """Adapter for Anthropic's /v1/messages API.

    Args:
        api_key: Anthropic API key (must be non-empty).
        model: Default model ID (e.g. claude-3-5-sonnet-20241022).
        timeout: HTTP timeout in seconds.
    """

    name = "anthropic"

    def __init__(self, *, api_key: str, model: str, timeout: float = 30.0) -> None:
        if not api_key:
            raise ValueError("Anthropic API key required")
        self.api_key = api_key
        self.model = model
        self._client = httpx.AsyncClient(
            base_url="https://api.anthropic.com",
            timeout=timeout,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
        )

    async def aclose(self) -> None:
        """Close the underlying httpx client. Call on shutdown."""
        await self._client.aclose()

    async def chat(self, request: ChatRequest) -> ChatResponse:
        system, messages = _convert_messages(request.messages)
        body: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens or 1024,
        }
        if system is not None:
            body["system"] = system
        if request.stop:
            body["stop_sequences"] = request.stop
        if request.tools:
            body["tools"] = request.tools

        try:
            resp = await self._client.post(ANTHROPIC_API_URL, json=body)
        except httpx.HTTPError as e:
            raise ProviderUnavailable(f"Anthropic network error: {e}") from e

        # Status code → exception
        if resp.status_code == 429:
            raise RateLimited("Anthropic rate limited")
        if 400 <= resp.status_code < 500:
            try:
                err = resp.json().get("error", {})
                message = err.get("message", resp.text)
            except Exception:
                message = resp.text
            raise InvalidRequest(f"Anthropic 4xx: {message}")
        if resp.status_code >= 500:
            raise ProviderUnavailable(f"Anthropic 5xx: {resp.status_code}")

        # 200 — parse body
        try:
            data = resp.json()
        except Exception as e:
            raise OutputInvalid(f"Anthropic returned non-JSON: {resp.text[:200]}") from e

        content_blocks = data.get("content") or []
        text_parts = [b.get("text", "") for b in content_blocks if b.get("type") == "text"]
        tool_calls = [b for b in content_blocks if b.get("type") == "tool_use"]

        stop_reason = data.get("stop_reason", "")
        if tool_calls and not text_parts:
            finish = "tool_use"
        elif stop_reason == "end_turn":
            finish = "stop"
        else:
            finish = stop_reason

        usage = data.get("usage", {})
        return ChatResponse(
            content="".join(text_parts),
            model=data.get("model", request.model),
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            finish_reason=finish,
            tool_calls=tool_calls or None,
            raw=data,
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        # M1 defer streaming to M2 — implement when needed
        raise NotImplementedError("Anthropic streaming pending M2")
        yield ""  # unreachable; needed for AsyncIterator typing