"""迁移 d7a1e5b3c9f2 回归：历史数据指纹回填与 upgrade/downgrade 往返。真实 PostgreSQL。"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.services import tickets as ticket_service
from tests.conftest import ROOT, TEST_DATABASE_URL

pytestmark = pytest.mark.integration

PREVIOUS_REVISION = "c314a4a9f8e2"

# 覆盖：NULL order_id、空描述、多字节字符、含分隔符与数字的标题（验证长度前缀无歧义）
_LEGACY_ROWS = [
    ("T20001", "U001", "A10002", "REPAIR", "手表表带断裂", "佩戴一周开裂", "HIGH", "pa-legacy-0001"),
    ("T20002", "U001", None, "OTHER", "3:ab|4:cd", "", "MEDIUM", "pa-legacy-0002"),
    ("T20003", "U002", None, "OTHER", "emoji 🙂 标题", "多行\n描述\t制表", "LOW", None),
]


def _alembic_config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    return cfg


def _constraint_names(conn) -> set[str]:
    rows = conn.execute(
        text("SELECT conname FROM pg_constraint WHERE conrelid = 'tickets'::regclass AND contype = 'u'")
    )
    return {r[0] for r in rows}


def _has_fingerprint_column(conn) -> bool:
    return bool(
        conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'tickets' AND column_name = 'request_fingerprint'"
            )
        ).scalar()
    )


def test_migration_backfills_fingerprint_and_round_trips(db, db_engine) -> None:
    cfg = _alembic_config()
    try:
        command.downgrade(cfg, PREVIOUS_REVISION)
        with db_engine.begin() as conn:
            assert not _has_fingerprint_column(conn)
            assert _constraint_names(conn) == {"tickets_idempotency_key_key"}
            for row in _LEGACY_ROWS:
                conn.execute(
                    text(
                        "INSERT INTO tickets (id, user_id, order_id, category, title, description, status, "
                        "priority, idempotency_key, created_at, updated_at) VALUES (:id, :user_id, :order_id, "
                        ":category, :title, :description, 'OPEN', :priority, :key, now(), now())"
                    ),
                    dict(zip(("id", "user_id", "order_id", "category", "title", "description", "priority", "key"),
                             row, strict=True)),
                )

        command.upgrade(cfg, "head")
        with db_engine.connect() as conn:
            assert _constraint_names(conn) == {"tickets_user_id_idempotency_key_key"}
            rows = conn.execute(
                text("SELECT id, category, title, description, priority, order_id, request_fingerprint FROM tickets")
            ).all()
        assert {r.id for r in rows} >= {row[0] for row in _LEGACY_ROWS}
        for r in rows:  # 种子工单 + 历史工单都必须按与应用层相同的算法回填
            expected = ticket_service.ticket_request_fingerprint(
                category=r.category, title=r.title, description=r.description, priority=r.priority,
                order_id=r.order_id,
            )
            assert r.request_fingerprint == expected, r.id

        # 回填后的历史工单可被同内容请求正常重放
        with db() as s:
            replay, created = ticket_service.create_ticket(
                s, user_id="U001", category="REPAIR", title="手表表带断裂", description="佩戴一周开裂",
                priority="HIGH", order_id="A10002", idempotency_key="pa-legacy-0001",
            )
            assert (replay.id, created) == ("T20001", False)

        # 往返：再降级 → 再升级
        command.downgrade(cfg, PREVIOUS_REVISION)
        with db_engine.connect() as conn:
            assert not _has_fingerprint_column(conn)
            assert _constraint_names(conn) == {"tickets_idempotency_key_key"}
        command.upgrade(cfg, "head")
        with db_engine.connect() as conn:
            assert _has_fingerprint_column(conn)
            assert _constraint_names(conn) == {"tickets_user_id_idempotency_key_key"}
    finally:
        command.upgrade(cfg, "head")
        db_engine.dispose()  # 丢弃迁移前建立的连接，避免后续用例复用旧的预编译语句


def test_downgrade_refuses_when_key_is_shared_across_users(db, db_engine) -> None:
    for user_id in ("U001", "U002"):
        with db() as s:
            ticket_service.create_ticket(
                s, user_id=user_id, category="OTHER", title="共享 key", idempotency_key="pa-shared-0001"
            )

    cfg = _alembic_config()
    try:
        with pytest.raises(Exception, match="used by multiple users"):
            command.downgrade(cfg, PREVIOUS_REVISION)
        with db_engine.connect() as conn:  # 事务性 DDL：失败后仍停留在 head
            assert _has_fingerprint_column(conn)
            assert _constraint_names(conn) == {"tickets_user_id_idempotency_key_key"}
    finally:
        command.upgrade(cfg, "head")
        db_engine.dispose()
