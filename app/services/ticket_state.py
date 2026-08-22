"""工单状态机（纯函数，零 DB 依赖——单测直接覆盖）。

v1.1 补丁：OPEN → PROCESSING → RESOLVED → CLOSED 单向链；
禁止非法逆向，除非未来明确增加 reopen。
"""

from __future__ import annotations

from app.models.ticket import TICKET_STATUSES
from app.services.errors import InvalidTransitionError

LEGAL_TICKET_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("OPEN", "PROCESSING"),
        ("PROCESSING", "RESOLVED"),
        ("RESOLVED", "CLOSED"),
    }
)


def can_transition(current: str, target: str) -> bool:
    return (current, target) in LEGAL_TICKET_TRANSITIONS


def assert_transition(current: str, target: str) -> None:
    """非法迁移抛 InvalidTransitionError（含合法链提示）。"""
    if target not in TICKET_STATUSES:
        raise InvalidTransitionError(f"未知工单状态: {target}")
    if not can_transition(current, target):
        raise InvalidTransitionError(
            f"不允许的状态迁移: {current} -> {target}（合法链: OPEN->PROCESSING->RESOLVED->CLOSED，禁止逆向）"
        )
