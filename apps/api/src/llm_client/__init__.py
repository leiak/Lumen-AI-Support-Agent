"""LLM client package — provider-agnostic types, exceptions, and adapters."""

from llm_client.embeddings import EmbeddingError, EmbeddingResult, embed_texts

__all__ = [
    "EmbeddingError",
    "EmbeddingResult",
    "embed_texts",
]