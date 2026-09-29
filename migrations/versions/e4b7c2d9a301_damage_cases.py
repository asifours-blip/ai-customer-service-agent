"""Reviewed logistics damage cases, without customer free text.

Revision ID: e4b7c2d9a301
Revises: c9f1a3e6b2d5
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e4b7c2d9a301"
down_revision = "c9f1a3e6b2d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "damage_cases",
        sa.Column("source_ticket_id", sa.String(16), sa.ForeignKey("tickets.id"), primary_key=True),
        sa.Column("damage_kind", sa.String(24), nullable=False),
        sa.Column("reviewed_path", sa.String(32), nullable=False),
        sa.Column("product_id", sa.String(16), nullable=False),
        sa.Column("order_status", sa.String(16), nullable=False),
        sa.Column("logistics_status", sa.String(32), nullable=True),
        sa.Column("reviewed_policy_version", sa.String(32), nullable=False),
        sa.Column("approved_by", sa.String(16), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("withdrawn_by", sa.String(16), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("damage_kind IN ('OUTER_PACKAGE', 'PRODUCT', 'BOTH')", name="damage_cases_kind_check"),
        sa.CheckConstraint(
            "reviewed_path IN ('REQUEST_EVIDENCE', 'CARRIER_INVESTIGATION', 'REPLACEMENT_REVIEW', 'REFUND_REVIEW')",
            name="damage_cases_path_check",
        ),
    )


def downgrade() -> None:
    op.drop_table("damage_cases")
