"""Ollama provider — uses Ollama's OpenAI-compatible /v1/chat/completions endpoint.

Ollama serves the OpenAI protocol at http://localhost:11434/v1 by default,
so we just subclass OpenAIProvider and override the `name`.
"""
from llm_client.providers.openai_provider import OpenAIProvider


class OllamaProvider(OpenAIProvider):
    """Ollama adapter. Inherits all behavior from OpenAIProvider."""

    name = "ollama"

    def __init__(self, *, base_url: str, model: str, timeout: float = 60.0) -> None:
        super().__init__(
            api_key="ollama",  # dummy, Ollama ignores auth header
            model=model,
            base_url=base_url,
            timeout=timeout,
        )
