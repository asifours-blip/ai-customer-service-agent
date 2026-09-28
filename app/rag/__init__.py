"""RAG 包导出。"""

from app.rag.answerer import ABSTAIN_MESSAGE, RagAnswer, RagService
from app.rag.chunker import Chunk, chunk_corpus, chunk_document
from app.rag.embedding import (
    BGE_DIM,
    FAKE_DIM,
    EmbeddingClient,
    FakeEmbedding,
    LocalBGEEmbedding,
    backend_name,
    get_embedding_client,
    serving_retrieval,
)
from app.rag.loader import KnowledgeDocument, load_corpus, load_document, parse_document
from app.rag.store import RetrievedChunk, count_chunks, search

__all__ = [
    "ABSTAIN_MESSAGE",
    "RagAnswer",
    "RagService",
    "Chunk",
    "chunk_corpus",
    "chunk_document",
    "BGE_DIM",
    "FAKE_DIM",
    "EmbeddingClient",
    "FakeEmbedding",
    "LocalBGEEmbedding",
    "backend_name",
    "get_embedding_client",
    "serving_retrieval",
    "KnowledgeDocument",
    "load_corpus",
    "load_document",
    "parse_document",
    "RetrievedChunk",
    "count_chunks",
    "search",
]
