"""allocate Ticket business IDs through a PostgreSQL sequence

Revision ID: c314a4a9f8e2
Revises: bdc12d4c41f9
Create Date: 2026-08-24 00:00:00.000000
"""
from __future__ import annotations

from alembic import op

revision = "c314a4a9f8e2"
down_revision = "bdc12d4c41f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE malformed_ticket_id text;
        BEGIN
            SELECT id INTO malformed_ticket_id
            FROM tickets
            WHERE id !~ '^T[0-9]+$'
            LIMIT 1;

            IF malformed_ticket_id IS NOT NULL THEN
                RAISE EXCEPTION
                    'tickets contains nonconforming id %; ticket_id_sequence cannot be initialized',
                    malformed_ticket_id;
            END IF;
        END $$;
        """
    )
    op.execute("CREATE SEQUENCE ticket_id_sequence START WITH 10003")
    op.execute(
        """
        SELECT setval(
            'ticket_id_sequence',
            GREATEST(
                10002::bigint,
                COALESCE((SELECT MAX(SUBSTRING(id FROM 2)::bigint) FROM tickets), 10002::bigint)
            ),
            true
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP SEQUENCE ticket_id_sequence")
