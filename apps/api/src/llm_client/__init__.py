"""LLM client package — provider-agnostic types, exceptions, and adapters."""

from llm_client.embeddings import EmbeddingError, EmbeddingResult, embed_texts
from llm_client.exceptions import (
    AttemptRecord,
    FallbackChainExhausted,
    TenantLlmNotConfigured,
)
from llm_client.resolvers import FallbackResolver
from llm_client.tenant_config_crypto import TenantLLMConfigCipher
from llm_client.tenant_resolver import (
    TenantLLMConfigCache,
    TenantResolver,
    build_tenant_resolver,
)

__all__ = [
    "AttemptRecord",
    "EmbeddingError",
    "EmbeddingResult",
    "FallbackChainExhausted",
    "FallbackResolver",
    "TenantLLMConfigCache",
    "TenantLLMConfigCipher",
    "TenantLlmNotConfigured",
    "TenantResolver",
    "build_tenant_resolver",
    "embed_texts",
]