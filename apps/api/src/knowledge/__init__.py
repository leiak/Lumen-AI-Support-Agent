"""Public re-exports for the knowledge base (RAG) package."""
from knowledge.rag_service import RAGService, RagContext
from knowledge.repository import (
    ArticleRepository,
    ChunkRepository,
    KnowledgeBaseRepository,
)
from knowledge.retriever import (
    KnowledgeBaseNotFoundError,
    RetrievedChunk,
    retrieve_chunks,
)
from knowledge.service import ArticleService, KnowledgeBaseService
from knowledge.worker import (
    IndexResult,
    ReindexResult,
    delete_article_vectors,
    index_article,
    reindex_article,
)

__all__ = [
    "ArticleRepository",
    "ArticleService",
    "ChunkRepository",
    "IndexResult",
    "KnowledgeBaseNotFoundError",
    "KnowledgeBaseRepository",
    "KnowledgeBaseService",
    "RAGService",
    "RagContext",
    "ReindexResult",
    "RetrievedChunk",
    "delete_article_vectors",
    "index_article",
    "reindex_article",
    "retrieve_chunks",
]