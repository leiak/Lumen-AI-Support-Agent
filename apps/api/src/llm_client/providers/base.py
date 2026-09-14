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
    async def stream(self, request: ChatRequest) -> AsyncIterator["ChatResponse | str"]:
        """Send a streaming chat request.

        Yields one ``str`` per text-content delta as it arrives, then a final
        :class:`llm_client.types.ChatResponse` carrying the accumulated text,
        token usage and finish reason. On failure raises one of the typed
        exceptions from :mod:`llm_client.exceptions` (no partial-token retry —
        a stream cannot be resumed). Tool-use turns are not supported here;
        callers must use :meth:`chat` instead.
        """
        # Use 'yield' to make this an async generator; subclass overrides
        if False:  # pragma: no cover
            yield ""
