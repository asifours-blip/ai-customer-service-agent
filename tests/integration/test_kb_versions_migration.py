"""迁移 f5a9c2e7d104 回归：现有 chunk 归入 v1 并生效、空库不建空版本、降级只保留 ACTIVE 版本。真实 PostgreSQL。"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from tests.conftest import ROOT, TEST_DATABASE_URL
from tests.integration.test_kb_versions import TRADEIN_MD, new_version

pytestmark = pytest.mark.integration

PREVIOUS_REVISION = "e3b8c1f4a2d6"


def _alembic_config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    return cfg


def _tables(conn) -> set[str]:  # noqa: ANN001
    return {r[0] for r in conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))}


def _chunk_id_unique(conn) -> bool:  # noqa: ANN001
    return bool(
        conn.execute(
            text("SELECT indisunique FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                 "WHERE c.relname = 'ix_kb_chunks_chunk_id'")
        ).scalar()
    )


def test_kb_versions_migration_round_trip(db, db_engine) -> None:  # noqa: ANN001
    v2 = new_version(db, {"policy-tradein.md": TRADEIN_MD})
    assert v2.status == "READY"
    with db_engine.connect() as conn:
        active_chunks = set(conn.execute(text("SELECT chunk_id FROM kb_chunks WHERE version_id = 1")).scalars())
        assert conn.execute(text("SELECT count(*) FROM kb_chunks WHERE version_id = :v"), {"v": v2.id}).scalar()

    cfg = _alembic_config()
    try:
        command.downgrade(cfg, PREVIOUS_REVISION)
        with db_engine.connect() as conn:
            assert not {"kb_versions", "kb_documents", "kb_audit_log"} & _tables(conn)
            # 旧结构只能容纳一个知识库：只保留原 ACTIVE（v1）的 chunk，chunk_id 恢复全局唯一
            assert set(conn.execute(text("SELECT chunk_id FROM kb_chunks")).scalars()) == active_chunks
            assert _chunk_id_unique(conn)

        command.upgrade(cfg, "head")
        with db_engine.connect() as conn:
            [(vid, status, source, chunk_count, doc_count, note)] = conn.execute(
                text("SELECT id, status, source, chunk_count, doc_count, note FROM kb_versions")
            ).all()
            assert (status, source, chunk_count, doc_count) == ("ACTIVE", "MIGRATION", len(active_chunks), 0)
            assert "未保存文档原文" in note
            assert set(
                conn.execute(text("SELECT chunk_id FROM kb_chunks WHERE version_id = :v"), {"v": vid}).scalars()
            ) == active_chunks
            assert conn.execute(text("SELECT action FROM kb_audit_log WHERE version_id = :v"), {"v": vid}).scalar() == (
                "MIGRATED"
            )
            assert not _chunk_id_unique(conn)
    finally:
        command.upgrade(cfg, "head")


def test_migration_on_empty_kb_creates_no_active_version(db, db_engine) -> None:  # noqa: ANN001
    cfg = _alembic_config()
    try:
        command.downgrade(cfg, PREVIOUS_REVISION)
        with db_engine.begin() as conn:
            conn.execute(text("DELETE FROM kb_chunks"))
        command.upgrade(cfg, "head")
        with db_engine.connect() as conn:
            # 空知识库不建空的 ACTIVE 版本：否则启动初始化会因「已有生效版本」跳过，线上一直为空
            assert conn.execute(text("SELECT count(*) FROM kb_versions")).scalar() == 0
    finally:
        command.upgrade(cfg, "head")
