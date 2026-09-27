"""ticket workbench: assignee, append-only ticket events, customer feedback

Revision ID: e3b8c1f4a2d6
Revises: d7a1e5b3c9f2
Create Date: 2026-09-28 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e3b8c1f4a2d6"
down_revision = "d7a1e5b3c9f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) 领取人
    op.add_column("tickets", sa.Column("assignee_id", sa.String(length=16), nullable=True))
    op.create_foreign_key("tickets_assignee_id_fkey", "tickets", "users", ["assignee_id"], ["id"])
    op.create_index("ix_tickets_assignee_id", "tickets", ["assignee_id"])

    # 2) 处理记录（只追加）
    op.create_table(
        "ticket_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ticket_id", sa.String(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=16), nullable=False),
        sa.Column("actor_role", sa.String(length=16), nullable=False),
        sa.Column("from_status", sa.String(length=16), nullable=True),
        sa.Column("to_status", sa.String(length=16), nullable=True),
        sa.Column("reply_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"]),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["reply_id"], ["ticket_replies.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ticket_events_ticket_id", "ticket_events", ["ticket_id"])
    # DB 层兜底「只追加」：任何 UPDATE / DELETE 直接报错（TRUNCATE 不触发行级触发器，仅供测试清库）
    op.execute(
        """
        CREATE FUNCTION ticket_events_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'ticket_events is append-only: % is not allowed', TG_OP;
        END
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER ticket_events_no_update_delete
        BEFORE UPDATE OR DELETE ON ticket_events
        FOR EACH ROW EXECUTE FUNCTION ticket_events_append_only()
        """
    )
    # 历史回填：已知的创建与回复可以还原；历史状态变更无记录，不伪造
    op.execute(
        """
        INSERT INTO ticket_events (ticket_id, event_type, actor_id, actor_role, from_status, to_status, created_at)
        SELECT t.id, 'CREATED', t.user_id, 'CUSTOMER', NULL, 'OPEN', t.created_at FROM tickets t
        """
    )
    op.execute(
        """
        INSERT INTO ticket_events (ticket_id, event_type, actor_id, actor_role, reply_id, created_at)
        SELECT r.ticket_id, 'REPLIED', r.author_id, r.author_role, r.id, r.created_at FROM ticket_replies r
        """
    )

    # 3) 客户反馈：每张工单至多一条
    op.create_table(
        "ticket_feedback",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ticket_id", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.String(length=16), nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name="ticket_feedback_rating_range"),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ticket_id", name="ticket_feedback_ticket_id_key"),
    )


def downgrade() -> None:
    op.drop_table("ticket_feedback")
    op.execute("DROP TRIGGER ticket_events_no_update_delete ON ticket_events")
    op.execute("DROP FUNCTION ticket_events_append_only()")
    op.drop_index("ix_ticket_events_ticket_id", table_name="ticket_events")
    op.drop_table("ticket_events")
    op.drop_index("ix_tickets_assignee_id", table_name="tickets")
    op.drop_constraint("tickets_assignee_id_fkey", "tickets", type_="foreignkey")
    op.drop_column("tickets", "assignee_id")
