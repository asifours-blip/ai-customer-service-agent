"""向量检索：余弦 top-k（pgvector），只在单个知识库版本内检索。

默认检索当前 ACTIVE 版本；DRAFT / INGESTING / READY / FAILED / RETIRED 的内容一律不可检索。
评测可显式指定 version_id 评测某个版本（不影响线上生效版本）。
写入由 app.kb.service 负责：导入只写新版本，从不删除或覆盖已有版本的 chunk。
检索前校验查询向量维度与目标版本导入时记录的维度一致：换了向量模型却没重建版本时拒绝检索。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.knowledge import EMBEDDING_DIM, KB_STATUS_ACTIVE, KbChunk, KbVersion
from app.services.errors import AppError


class KbRebuildRequiredError(AppError):
    """查询向量维度与知识库版本记录的维度不一致：换了向量模型却没重建版本，检索结果没有意义。"""

    code = "KB_REBUILD_REQUIRED"
    http_status = 503


def recorded_dimension(version: KbVersion) -> int:
    """版本导入时校验并记录的向量维度；迁移归档的旧版本 / 导入中的版本还没有记录，以列宽为准。"""
    dim_check = (version.checks or {}).get("embedding_dim") or {}
    actual = dim_check.get("actual") if dim_check.get("passed") else None
    return int(actual) if isinstance(actual, int) else EMBEDDING_DIM


def ensure_dimension_matches(*, version_id: int, recorded_dim: int, query_dim: int) -> None:
    if recorded_dim != query_dim:
        raise KbRebuildRequiredError(
            f"当前向量模型输出 {query_dim} 维，知识库 v{version_id} 记录的是 {recorded_dim} 维：拒绝检索。"
            "请用当前向量模型重建知识库版本并发布后再试"
        )


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
    if version_id is None:
        target = db.scalar(select(KbVersion).where(KbVersion.status == KB_STATUS_ACTIVE))
    else:
        target = db.get(KbVersion, version_id)
    if target is not None:
        ensure_dimension_matches(
            version_id=target.id, recorded_dim=recorded_dimension(target), query_dim=len(query_vector)
        )
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
