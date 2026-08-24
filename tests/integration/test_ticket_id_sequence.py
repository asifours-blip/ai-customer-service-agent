"""Ticket 编号分配与幂等唯一约束恢复的 PostgreSQL 回归。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from app.services import tickets as ticket_service

pytestmark = pytest.mark.integration


def _integrity_error(constraint_name: str) -> IntegrityError:
    original = SimpleNamespace(
        sqlstate="23505",
        diag=SimpleNamespace(constraint_name=constraint_name),
    )
    return IntegrityError("INSERT INTO tickets", {}, original)


def test_idempotency_unique_violation_requires_exact_postgresql_constraint_name() -> None:
    assert ticket_service._is_idempotency_key_unique_violation(  # type: ignore[attr-defined]
        _integrity_error("tickets_idempotency_key_key")
    )
    assert not ticket_service._is_idempotency_key_unique_violation(  # type: ignore[attr-defined]
        _integrity_error("tickets_pkey")
    )
    assert not ticket_service._is_idempotency_key_unique_violation(  # type: ignore[attr-defined]
        _integrity_error("some_other_unique_constraint")
    )


def test_create_ticket_does_not_treat_primary_key_integrity_error_as_replay(db, monkeypatch) -> None:
    with db() as session:
        def fail_commit() -> None:
            raise _integrity_error("tickets_pkey")

        monkeypatch.setattr(session, "commit", fail_commit)
        with pytest.raises(IntegrityError, match="INSERT INTO tickets"):
            ticket_service.create_ticket(
                session,
                user_id="U001",
                category="OTHER",
                title="non-idempotency-integrity-error",
                idempotency_key="pa-ticket-pkey-0001",
            )
        session.rollback()
