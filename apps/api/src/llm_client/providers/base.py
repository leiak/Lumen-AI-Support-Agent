"""Abstract base for LLM providers.

Each adapter (Anthropic, OpenAI, Ollama) implements this interface so
the LLMClient can switch providers without changing call sites.
"""
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from llm_client.types import ChatRequest, ChatResponse


class BaseProvider(ABC):
    """Base class for all LLM provider adapters.

    Implementations MUST set `name` (e.g. "anthropic", "openai", "ollama")
    and implement `chat` and `stream`.
    """

    name: str  # set by subclass

    @abstractmethod
    async def chat(self, request: ChatRequest) -> ChatResponse:
        """Send a non-streaming chat request. Returns the full response."""

    @abstractmethod
    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        """Send a streaming chat request. Yields content chunks as they arrive.

        Token usage and finish_reason will be reported in a final
        sentinel chunk (e.g. a ChatResponse object cast to str) — see
        the LLMClient for the exact protocol.
        """
        # Use 'yield' to make this an async generator; subclass overrides
        if False:  # pragma: no cover
            yield ""
