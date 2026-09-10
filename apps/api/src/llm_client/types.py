"""Core types for the LLM client.

These are provider-agnostic — adapters translate to/from each provider's
native format.
"""
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class MessageRole(StrEnum):
    """Role of a chat message. Aligns to OpenAI's conventions; Anthropic
    treats 'system' as a separate system-prompt block but we normalize here.
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ChatMessage(BaseModel):
    """A single message in a chat conversation."""

    role: MessageRole
    content: str
    name: str | None = None
    tool_call_id: str | None = None


class ChatRequest(BaseModel):
    """Request to an LLM provider."""

    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)
    stop: list[str] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    """Response from an LLM provider."""

    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str
    tool_calls: list[dict[str, Any]] | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        """Total tokens consumed by this request (prompt + completion)."""
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class EmbeddingResult:
    """Result of an embedding batch call.

    `vectors[i]` corresponds to `texts[i]` (OpenAI preserves input order).
    Token counts are aggregated across all batches in the original call.
    """

    vectors: list[list[float]]
    model: str
    prompt_tokens: int
    total_tokens: int


class EmbeddingError(Exception):
    """Raised when embedding generation fails after retries.

    Wraps rate-limit exhaustion and non-retryable API errors (4xx, auth, etc.).
    """
