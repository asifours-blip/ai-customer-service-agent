"""知识库：版本、版本内文档原文、chunk（pgvector）、版本审计记录。

版本状态机（D-021）：
  DRAFT → INGESTING → READY | FAILED
  READY → ACTIVE（发布）；ACTIVE → RETIRED（被新版本替换）；RETIRED → ACTIVE（回滚）
检索只看 ACTIVE；同一时刻至多一个 ACTIVE 由部分唯一索引在数据库层保证。
旧版本只标 RETIRED 不删除，历史回答引用的 (version_id, chunk_id) 始终能取回当时的原文。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow

EMBEDDING_DIM = 512

KB_STATUS_DRAFT = "DRAFT"
KB_STATUS_INGESTING = "INGESTING"
KB_STATUS_READY = "READY"
KB_STATUS_FAILED = "FAILED"
KB_STATUS_ACTIVE = "ACTIVE"
KB_STATUS_RETIRED = "RETIRED"
KB_STATUSES = (
    KB_STATUS_DRAFT,
    KB_STATUS_INGESTING,
    KB_STATUS_READY,
    KB_STATUS_FAILED,
    KB_STATUS_ACTIVE,
    KB_STATUS_RETIRED,
)

# 版本来源：迁移时归档的旧数据 / 启动时从 knowledge_base/ 初始化 / 管理员上传 / 评测按目录导入
KB_SOURCES = ("MIGRATION", "BOOTSTRAP", "UPLOAD", "EVAL")


class KbVersion(Base):
    __tablename__ = "kb_versions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT', 'INGESTING', 'READY', 'FAILED', 'ACTIVE', 'RETIRED')",
            name="kb_versions_status_check",
        ),
        # 数据库层保证「同一时刻至多一个 ACTIVE」：并发发布即使绕过应用层串行化也无法写出第二个
        Index(
            "uq_kb_versions_single_active",
            "status",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    # 来源内容哈希：sha256(按 path 排序的 "path:文档内容哈希" 行)；迁移版本无原文，取 chunk 内容哈希
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)  # 用户 id 或 system
    # 生成向量所用后端（fake | bge）；迁移归档的旧数据未知为 NULL
    embedding_backend: Mapped[str | None] = mapped_column(String(32), nullable=True)
    doc_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progress_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checks: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class KbDocument(Base):
    """版本内的文档原文快照：版本一经创建即不再修改。"""

    __tablename__ = "kb_documents"
    __table_args__ = (
        UniqueConstraint("version_id", "path", name="uq_kb_documents_version_path"),
        UniqueConstraint("version_id", "document_id", name="uq_kb_documents_version_document"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("kb_versions.id"), nullable=False)
    path: Mapped[str] = mapped_column(String(255), nullable=False)  # category/文件名
    document_id: Mapped[str] = mapped_column(String(64), nullable=False)
    document_name: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)


class KbChunk(Base):
    __tablename__ = "kb_chunks"
    __table_args__ = (UniqueConstraint("version_id", "chunk_id", name="uq_kb_chunks_version_chunk"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("kb_versions.id"), nullable=False)
    chunk_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    document_name: Mapped[str] = mapped_column(String(128), nullable=False)
    section: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class KbAuditLog(Base):
    """知识库操作审计：上传、导入开始/完成/失败、发布、退役、回滚、重启恢复。

    只追加：DB 触发器 kb_audit_log_no_update_delete 拒绝 UPDATE / DELETE（迁移 a6c3e8b2d417）。
    """

    __tablename__ = "kb_audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("kb_versions.id"), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)  # 用户 id 或 system
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
