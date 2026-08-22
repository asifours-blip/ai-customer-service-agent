"""向量存储：全量重建索引 + 余弦 top-k 检索（pgvector）。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.knowledge import KbChunk
from app.rag.chunker import Chunk
from app.rag.embedding import EmbeddingClient


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: str
    document_id: str
    document_name: str
    section: str
    content: str
    score: float  # 余弦相似度（检索相关性，不代表答案正确概率）


def rebuild_index(db: Session, chunks: list[Chunk], embedder: EmbeddingClient) -> int:
    """全量重建：删旧插新（知识库小，演示域全量重建最简单且确定）。"""
    db.execute(delete(KbChunk))
    if not chunks:
        db.commit()
        return 0
    vectors = embedder.embed_documents([c.content for c in chunks])
    rows = [
        KbChunk(
            chunk_id=c.chunk_id,
            document_id=c.document_id,
            document_name=c.document_name,
            section=c.section,
            content=c.content,
            metadata_=dict(c.metadata),
            embedding=v,
        )
        for c, v in zip(chunks, vectors, strict=True)
    ]
    db.add_all(rows)
    db.commit()
    return len(rows)


def search(db: Session, query_vector: list[float], top_k: int = 5) -> list[RetrievedChunk]:
    distance = KbChunk.embedding.cosine_distance(query_vector)
    stmt = select(KbChunk, distance.label("dist")).order_by(distance).limit(top_k)
    rows = db.execute(stmt).all()
    return [
        RetrievedChunk(
            chunk_id=c.chunk_id,
            document_id=c.document_id,
            document_name=c.document_name,
            section=c.section,
            content=c.content,
            score=round(1.0 - float(dist), 6),
        )
        for c, dist in rows
    ]


def count_chunks(db: Session) -> int:
    from sqlalchemy import func

    return int(db.scalar(select(func.count()).select_from(KbChunk)) or 0)
