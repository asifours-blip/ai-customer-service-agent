"""scope ticket idempotency keys to their owner and add request fingerprints

Revision ID: d7a1e5b3c9f2
Revises: c314a4a9f8e2
Create Date: 2026-09-27 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d7a1e5b3c9f2"
down_revision = "c314a4a9f8e2"
branch_labels = None
depends_on = None

# 与 app.services.tickets.ticket_request_fingerprint 完全相同的规范化算法（SQL 版）：
# "v1|" + 按 category/title/description/priority/order_id 顺序，
# 每个字段 "<UTF-8 字节长度>:<原文>"，NULL 为 "~"，以 "|" 连接，再取 sha256 十六进制。
_FINGERPRINT_SQL = """
    encode(sha256(convert_to(
        'v1|'
        || octet_length(convert_to(category, 'UTF8')) || ':' || category
        || '|' || octet_length(convert_to(title, 'UTF8')) || ':' || title
        || '|' || octet_length(convert_to(description, 'UTF8')) || ':' || description
        || '|' || octet_length(convert_to(priority, 'UTF8')) || ':' || priority
        || '|' || COALESCE(octet_length(convert_to(order_id, 'UTF8')) || ':' || order_id, '~'),
        'UTF8'
    )), 'hex')
"""


def upgrade() -> None:
    op.add_column("tickets", sa.Column("request_fingerprint", sa.String(length=64), nullable=True))
    op.execute(f"UPDATE tickets SET request_fingerprint = {_FINGERPRINT_SQL}")
    op.drop_constraint("tickets_idempotency_key_key", "tickets", type_="unique")
    op.create_unique_constraint(
        "tickets_user_id_idempotency_key_key", "tickets", ["user_id", "idempotency_key"]
    )


def downgrade() -> None:
    # 全局唯一约束无法容纳「不同用户使用同一 key」的数据：fail-fast，不静默删改业务数据
    op.execute(
        """
        DO $$
        DECLARE shared_key text;
        BEGIN
            SELECT idempotency_key INTO shared_key
            FROM tickets
            WHERE idempotency_key IS NOT NULL
            GROUP BY idempotency_key
            HAVING COUNT(*) > 1
            LIMIT 1;

            IF shared_key IS NOT NULL THEN
                RAISE EXCEPTION
                    'tickets.idempotency_key % is used by multiple users; cannot restore global unique constraint',
                    shared_key;
            END IF;
        END $$;
        """
    )
    op.drop_constraint("tickets_user_id_idempotency_key_key", "tickets", type_="unique")
    op.create_unique_constraint("tickets_idempotency_key_key", "tickets", ["idempotency_key"])
    op.drop_column("tickets", "request_fingerprint")
