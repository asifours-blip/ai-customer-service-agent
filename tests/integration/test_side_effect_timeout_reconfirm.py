"""SIDE_EFFECT_TIMEOUT 之后用户重新确认：只能有一张工单，且用户能查到。真实 PostgreSQL + Agent 全链路。

幂等键来源：node_execute_confirmed 使用 pending_action.id 作为 idempotency_key；
超时分支不清空 pending_action，因此 TTL 内再次「确认」会复用同一个 key。
首次执行用 Event 门控，确定性地让它在调用方已收到超时之后才真正写库。
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.agent.service import AgentService
from app.agent.state import make_pending, save_state_to_conversation
from app.api.chat import _agent_service
from app.config import get_settings
from app.models.base import utcnow
from app.models.conversation import Conversation
from app.models.ticket import Ticket
from app.services import tickets as ticket_service
from app.tools import build_registry
from tests.conftest import auth_headers

pytestmark = [pytest.mark.agent, pytest.mark.integration]

U = "U001"
ORDER = "A10001"  # 签收 3 天，REFUND 7 天内可申请
_WAIT = 10


class _GatedFirstCreate:
    """只拦截第一次 create_ticket：阻塞到 release，写完后 set done。"""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.done = threading.Event()
        self._claimed = False
        self._lock = threading.Lock()
        self._original = ticket_service.create_ticket

    def __call__(self, db, **kwargs):  # noqa: ANN001, ANN003, ANN204
        with self._lock:
            first, self._claimed = not self._claimed, True
        if not first:
            return self._original(db, **kwargs)
        try:
            assert self.release.wait(_WAIT), "测试未放行首次执行"
            return self._original(db, **kwargs)
        finally:
            self.done.set()


@pytest.fixture()
def gated(monkeypatch) -> _GatedFirstCreate:
    gate = _GatedFirstCreate()
    monkeypatch.setattr(ticket_service, "create_ticket", gate)
    return gate


@pytest.fixture()
def agent(db, monkeypatch) -> AgentService:
    monkeypatch.setattr(get_settings(), "tool_timeout_seconds", 1.0)
    base = _agent_service()
    return AgentService(base.llm, base.rag, build_registry())  # 新建以使用上面的超时


def _seed_pending(factory, session_id: str, pending_id: str) -> None:
    with factory() as s:
        conv = Conversation(session_id=session_id, user_id=U)
        s.add(conv)
        s.commit()
        pending = make_pending(
            "CREATE_AFTER_SALES_TICKET",
            {"order_id": ORDER, "category": "REFUND", "title": "P001 售后申请（REFUND）", "description": "想退货"},
            pending_id=pending_id,
        )
        pending["expires_at"] = (utcnow() + timedelta(minutes=10)).isoformat()
        save_state_to_conversation(s, conv, active_order_id=ORDER, active_ticket_id=None, pending=pending)


def _tickets_for(factory, key: str) -> list[Ticket]:
    with factory() as s:
        return list(s.scalars(select(Ticket).where(Ticket.user_id == U, Ticket.idempotency_key == key)))


def _pending_id(factory, session_id: str) -> str | None:
    with factory() as s:
        conv = s.scalar(select(Conversation).where(Conversation.session_id == session_id))
        assert conv is not None
        return conv.pending_action_id


def _confirm(agent: AgentService, factory, session_id: str) -> str:
    with factory() as s:
        return str(agent.handle(s, U, session_id, "确认")["answer"])


def _assert_user_can_see(client, ticket_id: str) -> None:
    h = auth_headers(client, "demo_customer")
    assert ticket_id in [t["id"] for t in client.get("/api/tickets", headers=h).json()]
    assert client.get(f"/api/tickets/{ticket_id}", headers=h).status_code == 200


def test_late_commit_then_reconfirm_replays_single_ticket(db, client, agent, gated) -> None:
    sid, key = "late-reconfirm-a", "pa-late-reconfirm-a1"
    _seed_pending(db, sid, key)

    first = _confirm(agent, db, sid)
    assert "响应超时" in first
    assert _tickets_for(db, key) == []  # 首次执行仍卡在写库之前
    assert _pending_id(db, sid) == key  # 超时不清空 pending：重新确认会复用同一个 key

    gated.release.set()
    assert gated.done.wait(_WAIT)  # 首次执行迟到提交
    assert len(_tickets_for(db, key)) == 1

    second = _confirm(agent, db, sid)
    tickets = _tickets_for(db, key)
    assert len(tickets) == 1
    assert f"工单 {tickets[0].id} 已存在（本次为重复确认，未重复创建）" in second
    assert _pending_id(db, sid) is None
    _assert_user_can_see(client, tickets[0].id)


def test_reconfirm_while_first_attempt_in_flight_single_ticket(db, client, agent, gated) -> None:
    sid, key = "late-reconfirm-b", "pa-late-reconfirm-b1"
    _seed_pending(db, sid, key)

    first = _confirm(agent, db, sid)
    assert "响应超时" in first

    second = _confirm(agent, db, sid)  # 首次执行尚未落库时用户再次确认
    tickets = _tickets_for(db, key)
    assert len(tickets) == 1
    assert f"已为您创建售后工单 {tickets[0].id}" in second

    gated.release.set()
    assert gated.done.wait(_WAIT)  # 迟到的首次执行命中同一 key → 重放，不再 INSERT
    assert [t.id for t in _tickets_for(db, key)] == [tickets[0].id]
    _assert_user_can_see(client, tickets[0].id)
