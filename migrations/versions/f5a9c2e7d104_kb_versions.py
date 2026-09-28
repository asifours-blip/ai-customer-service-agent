"""kb versions: 版本表 / 版本内文档原文 / 审计；kb_chunks 按版本隔离，现有 chunk 归入 v1 并生效

Revision ID: f5a9c2e7d104
Revises: e3b8c1f4a2d6
Create Date: 2026-09-28 12:00:00.000000

upgrade：已有 chunk 时自动创建 v1（ACTIVE，source=MIGRATION）并把全部 chunk 归入 v1。
旧流程没有保存文档原文，所以 v1 没有 kb_documents 行，source_hash 取 chunk 内容哈希，向量后端记为未知。
没有任何 chunk 时不创建空的 ACTIVE 版本——否则启动初始化会因「已有生效版本」跳过，线上知识库一直为空。

downgrade：回到「全局唯一 chunk_id、单一知识库」的结构，只能保留 ACTIVE 版本的 chunk；
其余版本（READY/RETIRED/FAILED 等）的 chunk 与全部版本元数据、文档原文、审计记录会被删除（结构所限，有损）。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f5a9c2e7d104"
down_revision = "e3b8c1f4a2d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kb_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("embedding_backend", sa.String(length=32), nullable=True),
        sa.Column("doc_count", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=True),
        sa.Column("progress_done", sa.Integer(), nullable=False),
        sa.Column("progress_total", sa.Integer(), nullable=False),
        sa.Column("checks", sa.JSON(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'INGESTING', 'READY', 'FAILED', 'ACTIVE', 'RETIRED')",
            name="kb_versions_status_check",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # 同一时刻至多一个 ACTIVE：部分唯一索引，数据库层兜底
    op.create_index(
        "uq_kb_versions_single_active",
        "kb_versions",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )

    op.create_table(
        "kb_documents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(length=255), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("document_name", sa.String(length=128), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["version_id"], ["kb_versions.id"], name="kb_documents_version_id_fkey"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version_id", "path", name="uq_kb_documents_version_path"),
        sa.UniqueConstraint("version_id", "document_id", name="uq_kb_documents_version_document"),
    )

    op.create_table(
        "kb_audit_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("from_status", sa.String(length=16), nullable=True),
        sa.Column("to_status", sa.String(length=16), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["version_id"], ["kb_versions.id"], name="kb_audit_log_version_id_fkey"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_kb_audit_log_version_id", "kb_audit_log", ["version_id"])

    # kb_chunks：先加可空列 → 归档现有数据到 v1 → 再收紧为 NOT NULL
    op.add_column("kb_chunks", sa.Column("version_id", sa.Integer(), nullable=True))
    op.execute(
        """
        WITH stats AS (
            SELECT count(*) AS n,
                   count(DISTINCT document_id) AS docs,
                   encode(sha256(convert_to(
                       string_agg(chunk_id || ':' || content, E'\\n' ORDER BY chunk_id), 'UTF8')), 'hex') AS digest
            FROM kb_chunks
        ), v1 AS (
            INSERT INTO kb_versions (status, source, source_hash, note, created_by, embedding_backend, doc_count,
                                     chunk_count, progress_done, progress_total, created_at, updated_at,
                                     finished_at, activated_at)
            SELECT 'ACTIVE', 'MIGRATION', digest,
                   '迁移自版本化之前的 kb_chunks（' || n || ' 个 chunk / ' || docs || ' 篇文档）：'
                   || '旧流程未保存文档原文，source_hash 为 chunk 内容哈希，向量后端未知',
                   'system', NULL, 0, n, 0, 0, now(), now(), now(), now()
            FROM stats WHERE n > 0
            RETURNING id
        ), audit AS (
            INSERT INTO kb_audit_log (version_id, action, actor, from_status, to_status, detail, created_at)
            SELECT id, 'MIGRATED', 'system', NULL, 'ACTIVE', NULL, now() FROM v1
        )
        UPDATE kb_chunks SET version_id = (SELECT id FROM v1)
        """
    )
    op.alter_column("kb_chunks", "version_id", nullable=False)
    op.create_foreign_key("kb_chunks_version_id_fkey", "kb_chunks", "kb_versions", ["version_id"], ["id"])
    # chunk_id 不再全局唯一：唯一键改为 (version_id, chunk_id)
    op.drop_index("ix_kb_chunks_chunk_id", table_name="kb_chunks")
    op.create_index("ix_kb_chunks_chunk_id", "kb_chunks", ["chunk_id"], unique=False)
    op.create_unique_constraint("uq_kb_chunks_version_chunk", "kb_chunks", ["version_id", "chunk_id"])


def downgrade() -> None:
    # 有损：旧结构只能容纳一个知识库，保留 ACTIVE 版本的 chunk，其余删除
    op.execute(
        "DELETE FROM kb_chunks WHERE version_id NOT IN (SELECT id FROM kb_versions WHERE status = 'ACTIVE')"
    )
    op.drop_constraint("uq_kb_chunks_version_chunk", "kb_chunks", type_="unique")
    op.drop_index("ix_kb_chunks_chunk_id", table_name="kb_chunks")
    op.create_index("ix_kb_chunks_chunk_id", "kb_chunks", ["chunk_id"], unique=True)
    op.drop_constraint("kb_chunks_version_id_fkey", "kb_chunks", type_="foreignkey")
    op.drop_column("kb_chunks", "version_id")

    op.drop_index("ix_kb_audit_log_version_id", table_name="kb_audit_log")
    op.drop_table("kb_audit_log")
    op.drop_table("kb_documents")
    op.drop_index("uq_kb_versions_single_active", table_name="kb_versions")
    op.drop_table("kb_versions")
