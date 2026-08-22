"""工单状态机：纯单测（零 DB）。合法链全覆盖 + 全部非法对。"""

from __future__ import annotations

import pytest

from app.models.ticket import TICKET_STATUSES
from app.services.errors import InvalidTransitionError
from app.services.ticket_state import assert_transition, can_transition

LEGAL = [("OPEN", "PROCESSING"), ("PROCESSING", "RESOLVED"), ("RESOLVED", "CLOSED")]


@pytest.mark.parametrize(("current", "target"), LEGAL)
def test_legal_transitions(current: str, target: str) -> None:
    assert can_transition(current, target)
    assert_transition(current, target)  # 不抛


def test_all_illegal_pairs_rejected() -> None:
    legal = set(LEGAL)
    for current in TICKET_STATUSES:
        for target in TICKET_STATUSES:
            if (current, target) in legal:
                continue
            assert not can_transition(current, target), f"{current}->{target} 应非法"
            with pytest.raises(InvalidTransitionError):
                assert_transition(current, target)


def test_unknown_status_rejected() -> None:
    with pytest.raises(InvalidTransitionError, match="未知工单状态"):
        assert_transition("OPEN", "REOPENED")


def test_reverse_and_skip_rejected() -> None:
    # 逆向
    with pytest.raises(InvalidTransitionError, match="禁止逆向"):
        assert_transition("PROCESSING", "OPEN")
    # 跳级
    with pytest.raises(InvalidTransitionError):
        assert_transition("OPEN", "RESOLVED")
    with pytest.raises(InvalidTransitionError):
        assert_transition("OPEN", "CLOSED")
    # 自环
    with pytest.raises(InvalidTransitionError):
        assert_transition("OPEN", "OPEN")
