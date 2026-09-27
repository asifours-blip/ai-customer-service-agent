"""迁移 e3b8c1f4a2d6 回归：assignee / ticket_events / ticket_feedback 的 upgrade、历史回填与往返。真实 PostgreSQL。"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from tests.conftest import ROOT, TEST_DATABASE_URL

pytestmark = pytest.mark.integration

PREVIOUS_REVISION = "d7a1e5b3c9f2"


def _alembic_config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    return cfg


def _tables(conn) -> set[str]:
    rows = conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
    return {r[0] for r in rows}


def _has_assignee(conn) -> bool:
    return bool(
        conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'tickets' AND column_name = 'assignee_id'"
            )
        ).scalar()
    )


def _has_trigger(conn) -> bool:
    return bool(
        conn.execute(text("SELECT 1 FROM pg_trigger WHERE tgname = 'ticket_events_no_update_delete'")).scalar()
    )


def test_workbench_migration_backfills_history_and_round_trips(db, db_engine) -> None:
    cfg = _alembic_config()
    try:
        command.downgrade(cfg, PREVIOUS_REVISION)
        with db_engine.begin() as conn:
            assert not _has_assignee(conn)
            assert not {"ticket_events", "ticket_feedback"} & _tables(conn)
            assert not _has_trigger(conn)
            # 降级后写入一张「旧版本」工单与一条回复，验证升级时的历史回填
            conn.execute(
                text(
                    "INSERT INTO tickets (id, user_id, order_id, category, title, description, status, priority, "
                    "created_at, updated_at) VALUES ('T30001', 'U001', NULL, 'OTHER', '旧工单', '', 'PROCESSING', "
                    "'LOW', now() - interval '1 day', now())"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO ticket_replies (ticket_id, author_id, author_role, content, created_at) "
                    "VALUES ('T30001', 'SUPPORT001', 'SUPPORT', '历史回复', now() - interval '2 hours')"
                )
            )

        command.upgrade(cfg, "head")
        with db_engine.connect() as conn:
            assert _has_assignee(conn)
            assert {"ticket_events", "ticket_feedback"} <= _tables(conn)
            assert _has_trigger(conn)
            events = conn.execute(
                text(
                    "SELECT event_type, actor_id, actor_role, to_status, reply_id IS NOT NULL AS has_reply "
                    "FROM ticket_events WHERE ticket_id = 'T30001' ORDER BY created_at, id"
                )
            ).all()
            # 可还原的历史（创建、回复）回填；未知的状态变更不伪造
            assert [tuple(e) for e in events] == [
                ("CREATED", "U001", "CUSTOMER", "OPEN", False),
                ("REPLIED", "SUPPORT001", "SUPPORT", None, True),
            ]
            # 每张存量工单都有且只有一条 CREATED
            missing = conn.execute(
                text(
                    "SELECT count(*) FROM tickets t WHERE (SELECT count(*) FROM ticket_events e "
                    "WHERE e.ticket_id = t.id AND e.event_type = 'CREATED') <> 1"
                )
            ).scalar()
            assert missing == 0
            assert conn.execute(text("SELECT assignee_id FROM tickets WHERE id = 'T30001'")).scalar() is None

        # 往返：再降级 → 再升级
        command.downgrade(cfg, PREVIOUS_REVISION)
        with db_engine.connect() as conn:
            assert not _has_assignee(conn)
            assert not {"ticket_events", "ticket_feedback"} & _tables(conn)
            assert not conn.execute(
                text("SELECT 1 FROM pg_proc WHERE proname = 'ticket_events_append_only'")
            ).scalar()
        command.upgrade(cfg, "head")
        with db_engine.connect() as conn:
            assert _has_assignee(conn) and _has_trigger(conn)
    finally:
        command.upgrade(cfg, "head")
        db_engine.dispose()  # 丢弃迁移前建立的连接，避免后续用例复用旧的预编译语句
