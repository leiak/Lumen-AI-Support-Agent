"""OpenAI-compatible chat-completions adapter.

Targets the OpenAI /chat/completions API. Also the base for `OllamaProvider`
since Ollama exposes the same endpoint at /v1/chat/completions.
"""
import json
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


def _normalize_tool_calls(
    tool_calls: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Convert OpenAI-style ``tool_calls`` to the internal normalized shape.

    OpenAI returns ``[{"type": "function", "id": ..., "function": {"name":
    ..., "arguments": "<json-string>"}}]``. The agent graph dispatcher keys
    off a top-level ``name`` and ``input`` (matching the Anthropic provider),
    so we normalise once at the adapter boundary to keep the graph
    provider-agnostic. Output entries look like
    ``{"type": "tool_use", "id": ..., "name": ..., "input": {...}}``.
    """
    if not tool_calls:
        return None
    normalized: list[dict[str, Any]] = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        name = fn.get("name") or tc.get("name")
        arguments = fn.get("arguments")
        input_args: Any = {}
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
                if isinstance(parsed, dict):
                    input_args = parsed
            except Exception:
                input_args = {}
        elif isinstance(arguments, dict):
            input_args = arguments
        normalized.append(
            {
                "type": "tool_use",
                "id": tc.get("id", ""),
                "name": name or "<missing>",
                "input": input_args,
            }
        )
    return normalized


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
            tool_calls=_normalize_tool_calls(msg.get("tool_calls")),
            raw=data,
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator["ChatResponse | str"]:
        """Stream /chat/completions in SSE mode. Yields text deltas, then a
        final ``ChatResponse`` with accumulated text + usage + finish reason.

        Tool-call deltas are accumulated across chunks and surfaced on the
        final ``ChatResponse.tool_calls`` so a streamed turn can still
        dispatch an escalation tool.
        """
        body = {
            "model": request.model,
            "messages": [m.model_dump(exclude_none=True) for m in request.messages],
            "temperature": request.temperature,
            "stream": True,
        }
        if request.max_tokens:
            body["max_tokens"] = request.max_tokens
        if request.stop:
            body["stop"] = request.stop
        if request.tools:
            body["tools"] = request.tools
        if request.tool_choice:
            body["tool_choice"] = request.tool_choice

        text_parts: list[str] = []
        tool_calls_acc: dict[int, dict[str, Any]] = {}
        model = request.model
        prompt_tokens = 0
        completion_tokens = 0
        finish_reason = ""

        try:
            stream_ctx = self._client.stream("POST", "/chat/completions", json=body)
            async with stream_ctx as resp:
                if resp.status_code == 429:
                    raise RateLimited("OpenAI rate limited")
                if 400 <= resp.status_code < 500:
                    err_text = (await resp.aread()).decode("utf-8", "replace")
                    raise InvalidRequest(f"OpenAI 4xx: {err_text[:200]}")
                if resp.status_code >= 500:
                    raise ProviderUnavailable(f"OpenAI 5xx: {resp.status_code}")

                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("model"):
                        model = obj["model"]
                    usage = obj.get("usage")
                    if usage:
                        prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                        completion_tokens = usage.get("completion_tokens", completion_tokens)
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        text_parts.append(content)
                        yield content
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        entry = tool_calls_acc.setdefault(
                            idx,
                            {
                                "id": tc.get("id", ""),
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            },
                        )
                        if tc.get("id"):
                            entry["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            entry["function"]["name"] = fn["name"]
                        if isinstance(fn.get("arguments"), str):
                            entry["function"]["arguments"] += fn["arguments"]
                    finish = choices[0].get("finish_reason")
                    if finish:
                        finish_reason = finish
        except httpx.HTTPError as e:
            raise ProviderUnavailable(f"OpenAI network error: {e}") from e

        yield ChatResponse(
            content="".join(text_parts),
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            finish_reason=finish_reason or "stop",
            tool_calls=_normalize_tool_calls(
                [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
                if tool_calls_acc
                else None
            ),
        )
