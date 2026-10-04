"""LLM client package — provider-agnostic types, exceptions, and adapters."""

from llm_client.embeddings import EmbeddingError, EmbeddingResult, embed_texts
from llm_client.exceptions import AttemptRecord, FallbackChainExhausted
from llm_client.resolvers import FallbackResolver

__all__ = [
    "AttemptRecord",
    "EmbeddingError",
    "EmbeddingResult",
    "FallbackChainExhausted",
    "FallbackResolver",
    "embed_texts",
]