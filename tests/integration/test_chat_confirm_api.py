"""结构化确认入口 POST /api/chat/confirm：复用 graph 的 check_pending → execute_confirmed 与同一幂等键。

以及 ChatResponse / 会话详情对前端暴露的资格依据、待确认操作与历史引用。真实 PostgreSQL + FakeLLM。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.conversation import Conversation
from app.models.ticket import Ticket
from tests.conftest import auth_headers

pytestmark = [pytest.mark.agent, pytest.mark.integration]

ASK = "A10001 买的耳机用了三天坏了，可以退款吗"


def _chat(client, headers, session: str, message: str) -> dict:
    r = client.post("/api/chat", json={"session_id": session, "message": message}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _confirm(client, headers, session: str, pending_id: str, decision: str = "CONFIRM"):
    return client.post(
        "/api/chat/confirm",
        json={"session_id": session, "pending_action_id": pending_id, "decision": decision},
        headers=headers,
    )


def test_after_sales_exposes_eligibility_and_pending_card(client) -> None:
    h = auth_headers(client, "demo_customer")
    r = _chat(client, h, "s-card", ASK)
    assert r["eligibility"]["eligible"] is True
    assert r["eligibility"]["reason_code"] == "WITHIN_RETURN_WINDOW"
    assert r["eligibility"]["details"] == {"delivered_days": 3, "allowed_days": 7}
    card = r["pending_action"]
    assert card["type"] == "CREATE_AFTER_SALES_TICKET"
    assert (card["order_id"], card["category"]) == ("A10001", "REFUND")
    assert card["id"].startswith("pa-")


def test_ineligible_after_sales_has_basis_but_no_card(client) -> None:
    r = _chat(client, auth_headers(client, "second_customer"), "s-no-card", "A20001 买的耳机用了二十天想退货退款")
    assert r["eligibility"]["eligible"] is False
    assert r["eligibility"]["reason_code"] == "BEYOND_RETURN_WINDOW"
    assert r["pending_action"] is None


def test_structured_confirm_creates_ticket_with_pending_id_as_idempotency_key(client, db) -> None:
    h = auth_headers(client, "demo_customer")
    pending_id = _chat(client, h, "s-confirm", ASK)["pending_action"]["id"]

    r = _confirm(client, h, "s-confirm", pending_id)
    assert r.status_code == 200
    body = r.json()
    assert body["route"] == "EXECUTE_CONFIRMED"
    assert body["tool_calls"][-1]["tool"] == "create_ticket"
    assert body["pending_action"] is None
    with db() as s:
        tickets = list(s.scalars(select(Ticket).where(Ticket.idempotency_key == pending_id)))
    assert len(tickets) == 1
    assert tickets[0].id in body["answer"]


def test_structured_and_text_confirm_share_idempotency_key(client, db, monkeypatch) -> None:
    """同一张卡：结构化确认遇到超时后，用户在聊天里再发「确认」仍复用同一个 pending id，不会重复开单。

    这里直接模拟「第一次结构化确认后 pending 仍保留」（超时分支语义），再用文本确认，断言只有一张工单。
    """
    from app.agent.state import load_state_from_conversation, save_state_to_conversation

    h = auth_headers(client, "demo_customer")
    pending_id = _chat(client, h, "s-share", ASK)["pending_action"]["id"]
    with db() as s:
        conv = s.scalar(select(Conversation).where(Conversation.session_id == "s-share"))
        saved = load_state_from_conversation(conv)["pending_action"]
    assert _confirm(client, h, "s-share", pending_id).status_code == 200
    with db() as s:  # 还原 pending，等价于超时后 pending 保留
        conv = s.scalar(select(Conversation).where(Conversation.session_id == "s-share"))
        save_state_to_conversation(s, conv, active_order_id="A10001", active_ticket_id=None, pending=saved)

    r2 = _chat(client, h, "s-share", "确认")
    assert "未重复创建" in r2["answer"]
    with db() as s:
        assert len(list(s.scalars(select(Ticket).where(Ticket.idempotency_key == pending_id)))) == 1


def test_stale_pending_id_does_not_execute_or_clear_current_card(client, db) -> None:
    h = auth_headers(client, "demo_customer")
    current = _chat(client, h, "s-stale", ASK)["pending_action"]["id"]

    r = _confirm(client, h, "s-stale", "pa-not-the-current-one")
    assert r.status_code == 200
    body = r.json()
    assert "已失效" in body["answer"]
    assert body["tool_calls"] == []
    assert body["pending_action"]["id"] == current  # 当前卡片仍可确认
    with db() as s:
        assert s.scalar(select(Ticket).where(Ticket.idempotency_key == current)) is None


def test_structured_cancel_clears_pending(client, db) -> None:
    h = auth_headers(client, "demo_customer")
    pending_id = _chat(client, h, "s-cancel", ASK)["pending_action"]["id"]
    r = _confirm(client, h, "s-cancel", pending_id, "CANCEL")
    assert r.status_code == 200
    assert "已取消" in r.json()["answer"]
    assert r.json()["pending_action"] is None
    # 取消后再确认同一张卡 → 失效，不建单
    again = _confirm(client, h, "s-cancel", pending_id).json()
    assert "已失效" in again["answer"]
    with db() as s:
        assert s.scalar(select(Ticket).where(Ticket.idempotency_key == pending_id)) is None


def test_confirm_other_users_session_forbidden(client) -> None:
    pending_id = _chat(client, auth_headers(client, "demo_customer"), "s-owner", ASK)["pending_action"]["id"]
    r = _confirm(client, auth_headers(client, "second_customer"), "s-owner", pending_id)
    assert r.status_code == 403
    missing = _confirm(client, auth_headers(client, "demo_customer"), "s-missing", pending_id)
    assert missing.status_code == 404
    assert client.post("/api/chat/confirm", json={"session_id": "s-owner", "pending_action_id": pending_id,
                                                  "decision": "MAYBE"},
                       headers=auth_headers(client, "demo_customer")).status_code == 422


def test_conversation_detail_restores_pending_card_and_sources(client) -> None:
    h = auth_headers(client, "demo_customer")
    rag = _chat(client, h, "s-restore", "这个耳机支持多久保修？")
    assert rag["sources"]
    pending_id = _chat(client, h, "s-restore", ASK)["pending_action"]["id"]

    conv_id = next(c["id"] for c in client.get("/api/conversations", headers=h).json()
                   if c["session_id"] == "s-restore")
    detail = client.get(f"/api/conversations/{conv_id}", headers=h).json()
    assert detail["pending_action"]["id"] == pending_id
    first_answer = detail["messages"][1]
    assert first_answer["role"] == "assistant"
    assert first_answer["sources"] == rag["sources"]
