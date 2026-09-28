"""agent_traces: record every LLM call and the answer mode

Revision ID: b7d2f4e9c813
Revises: a6c3e8b2d417
Create Date: 2026-09-28 20:00:00.000000

llm_calls：本轮每次 LLM 调用的结果类别、耗时、重试次数、usage（reported / unknown / none）。
answer_mode：MODEL / OFFLINE_ECHO / TEMPLATE / ERROR。旧记录两列均为 NULL（当时未记录，不回填猜测值）。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b7d2f4e9c813"
down_revision = "a6c3e8b2d417"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_traces", sa.Column("llm_calls", sa.JSON(), nullable=True))
    op.add_column("agent_traces", sa.Column("answer_mode", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_traces", "answer_mode")
    op.drop_column("agent_traces", "llm_calls")
