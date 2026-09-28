"""answer feedback + review audit: 回答反馈（有帮助/没帮助）与审核审计

Revision ID: c9f1a3e6b2d5
Revises: b7d2f4e9c813
Create Date: 2026-09-28 20:00:00.000000

answer_feedback：客户对某条 assistant 回答的反馈，(message_id, user_id) 唯一，不支持修改。
feedback_review_audit：管理员审核操作（CONVERT / REJECT）的审计记录，只追加，
与 kb_audit_log / ticket_events 相同的行级触发器做法。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c9f1a3e6b2d5"
down_revision = "b7d2f4e9c813"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "answer_feedback",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("message_id", sa.Integer(), sa.ForeignKey("messages.id"), nullable=False),
        sa.Column("user_id", sa.String(length=16), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("helpful", sa.Boolean(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("review_status", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column("reviewed_by", sa.String(length=64), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("message_id", "user_id", name="uq_answer_feedback_message_user"),
        sa.CheckConstraint(
            "review_status IN ('PENDING', 'CONVERTED', 'REJECTED')",
            name="answer_feedback_review_status_check",
        ),
    )
    op.create_index("ix_answer_feedback_message_id", "answer_feedback", ["message_id"])
    op.create_index("ix_answer_feedback_user_id", "answer_feedback", ["user_id"])

    op.create_table(
        "feedback_review_audit",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("feedback_id", sa.Integer(), sa.ForeignKey("answer_feedback.id"), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_feedback_review_audit_feedback_id", "feedback_review_audit", ["feedback_id"])

    op.execute(
        """
        CREATE FUNCTION feedback_review_audit_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'feedback_review_audit is append-only: % is not allowed', TG_OP;
        END
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER feedback_review_audit_no_update_delete
        BEFORE UPDATE OR DELETE ON feedback_review_audit
        FOR EACH ROW EXECUTE FUNCTION feedback_review_audit_append_only()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER feedback_review_audit_no_update_delete ON feedback_review_audit")
    op.execute("DROP FUNCTION feedback_review_audit_append_only()")
    op.drop_index("ix_feedback_review_audit_feedback_id", table_name="feedback_review_audit")
    op.drop_table("feedback_review_audit")
    op.drop_index("ix_answer_feedback_user_id", table_name="answer_feedback")
    op.drop_index("ix_answer_feedback_message_id", table_name="answer_feedback")
    op.drop_table("answer_feedback")
