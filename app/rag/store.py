"""向量检索：余弦 top-k（pgvector），只在单个知识库版本内检索。

默认检索当前 ACTIVE 版本；DRAFT / INGESTING / READY / FAILED / RETIRED 的内容一律不可检索。
评测可显式指定 version_id 评测某个版本（不影响线上生效版本）。
写入由 app.kb.service 负责：导入只写新版本，从不删除或覆盖已有版本的 chunk。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.knowledge import KB_STATUS_ACTIVE, KbChunk, KbVersion


@dataclass(frozen=True)
class RetrievedChunk:
    version_id: int
    chunk_id: str
    document_id: str
    document_name: str
    section: str
    content: str
    score: float  # 余弦相似度（检索相关性，不代表答案正确概率）


def search(
    db: Session, query_vector: list[float], top_k: int = 5, *, version_id: int | None = None
) -> list[RetrievedChunk]:
    distance = KbChunk.embedding.cosine_distance(query_vector)
    stmt = select(KbChunk, distance.label("dist"))
    if version_id is None:
        # 与版本表 join 在同一条语句里完成：发布切换是单事务提交，检索看到的要么全是旧版本要么全是新版本
        stmt = stmt.join(KbVersion, KbVersion.id == KbChunk.version_id).where(KbVersion.status == KB_STATUS_ACTIVE)
    else:
        stmt = stmt.where(KbChunk.version_id == version_id)
    rows = db.execute(stmt.order_by(distance).limit(top_k)).all()
    return [
        RetrievedChunk(
            version_id=c.version_id,
            chunk_id=c.chunk_id,
            document_id=c.document_id,
            document_name=c.document_name,
            section=c.section,
            content=c.content,
            score=round(1.0 - float(dist), 6),
        )
        for c, dist in rows
    ]


def count_chunks(db: Session, *, version_id: int | None = None) -> int:
    """默认统计 ACTIVE 版本的 chunk 数。"""
    stmt = select(func.count()).select_from(KbChunk)
    if version_id is None:
        stmt = stmt.join(KbVersion, KbVersion.id == KbChunk.version_id).where(KbVersion.status == KB_STATUS_ACTIVE)
    else:
        stmt = stmt.where(KbChunk.version_id == version_id)
    return int(db.scalar(stmt) or 0)
