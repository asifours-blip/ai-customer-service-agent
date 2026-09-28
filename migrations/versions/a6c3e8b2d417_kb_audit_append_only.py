"""kb audit log append-only: DB 触发器拒绝 kb_audit_log 的 UPDATE / DELETE

Revision ID: a6c3e8b2d417
Revises: f5a9c2e7d104
Create Date: 2026-09-28 16:00:00.000000

与 ticket_events 相同的做法：行级 BEFORE UPDATE OR DELETE 触发器直接报错，审计记录只能追加。
TRUNCATE 不触发行级触发器，仅供测试清库使用。
"""
from __future__ import annotations

from alembic import op

revision = "a6c3e8b2d417"
down_revision = "f5a9c2e7d104"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION kb_audit_log_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'kb_audit_log is append-only: % is not allowed', TG_OP;
        END
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER kb_audit_log_no_update_delete
        BEFORE UPDATE OR DELETE ON kb_audit_log
        FOR EACH ROW EXECUTE FUNCTION kb_audit_log_append_only()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER kb_audit_log_no_update_delete ON kb_audit_log")
    op.execute("DROP FUNCTION kb_audit_log_append_only()")
