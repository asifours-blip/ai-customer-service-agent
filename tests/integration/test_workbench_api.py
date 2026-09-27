"""阶段 2 工作台后端：领取 / 指派人鉴权 / 客户回复 / 反馈 / 处理记录（只追加）。真实 PostgreSQL。

seed：T10001（U001，OPEN，未指派）；T10002（U002，PROCESSING，SUPPORT001 已领取）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from tests.conftest import auth_headers

pytestmark = pytest.mark.integration


@pytest.fixture()
def h(client) -> dict[str, dict[str, str]]:
    return {
        "u1": auth_headers(client, "demo_customer"),
        "u2": auth_headers(client, "second_customer"),
        "s1": auth_headers(client, "support_agent"),
        "s2": auth_headers(client, "support_agent2"),
    }


def _resolve_t10001(client, h) -> None:
    assert client.post("/api/support/tickets/T10001/claim", headers=h["s1"]).status_code == 200
    for target in ("PROCESSING", "RESOLVED"):
        r = client.patch("/api/support/tickets/T10001/status", json={"status": target}, headers=h["s1"])
        assert r.status_code == 200, r.text


# --- 领取 ---


def test_claim_sets_assignee_and_is_idempotent_for_same_agent(client, h) -> None:
    r = client.post("/api/support/tickets/T10001/claim", headers=h["s1"])
    assert r.status_code == 200
    assert r.json()["assignee_id"] == "SUPPORT001"
    assert r.json()["status"] == "OPEN"  # 领取只指派，不改状态

    again = client.post("/api/support/tickets/T10001/claim", headers=h["s1"])
    assert again.status_code == 200
    detail = client.get("/api/support/tickets/T10001", headers=h["s1"]).json()
    assert [e["event_type"] for e in detail["events"]].count("CLAIMED") == 1  # 重复领取不重复记录


def test_claim_by_other_agent_conflicts(client, h) -> None:
    assert client.post("/api/support/tickets/T10001/claim", headers=h["s1"]).status_code == 200
    r = client.post("/api/support/tickets/T10001/claim", headers=h["s2"])
    assert r.status_code == 409
    assert r.json()["detail"]["type"] == "ALREADY_ASSIGNED"


def test_claim_forbidden_for_customer_and_unknown_ticket_404(client, h) -> None:
    assert client.post("/api/support/tickets/T10001/claim", headers=h["u1"]).status_code == 403
    assert client.post("/api/support/tickets/T99999/claim", headers=h["s1"]).status_code == 404


def test_closed_ticket_cannot_be_claimed(client, h) -> None:
    for target in ("RESOLVED", "CLOSED"):
        client.patch("/api/support/tickets/T10002/status", json={"status": target}, headers=h["s1"])
    r = client.post("/api/support/tickets/T10002/claim", headers=h["s1"])
    assert r.status_code == 409  # 已关闭：即使是原领取人也不能再领取
    assert r.json()["detail"]["type"] == "INVALID_STATE"


def test_support_queue_scopes(client, h) -> None:
    ids = lambda r: {t["id"] for t in r.json()}  # noqa: E731
    assert ids(client.get("/api/support/tickets?scope=unassigned", headers=h["s1"])) == {"T10001"}
    assert ids(client.get("/api/support/tickets?scope=mine", headers=h["s1"])) == {"T10002"}
    assert ids(client.get("/api/support/tickets?scope=mine", headers=h["s2"])) == set()
    assert ids(client.get("/api/support/tickets?scope=all&status=PROCESSING", headers=h["s1"])) == {"T10002"}
    assert client.get("/api/support/tickets?scope=bogus", headers=h["s1"]).status_code == 422


# --- 只有领取人能推进状态和回复 ---


def test_unclaimed_ticket_cannot_be_transitioned_or_replied(client, h) -> None:
    r = client.patch("/api/support/tickets/T10001/status", json={"status": "PROCESSING"}, headers=h["s1"])
    assert r.status_code == 403
    r = client.post("/api/support/tickets/T10001/replies", json={"content": "x"}, headers=h["s1"])
    assert r.status_code == 403


def test_non_assignee_cannot_transition_or_reply(client, h) -> None:
    # T10002 由 SUPPORT001 处理
    r = client.patch("/api/support/tickets/T10002/status", json={"status": "RESOLVED"}, headers=h["s2"])
    assert r.status_code == 403
    assert r.json()["detail"]["type"] == "PERMISSION_DENIED"
    r = client.post("/api/support/tickets/T10002/replies", json={"content": "越权回复"}, headers=h["s2"])
    assert r.status_code == 403
    detail = client.get("/api/support/tickets/T10002", headers=h["s1"]).json()
    assert detail["status"] == "PROCESSING"
    assert all(rep["content"] != "越权回复" for rep in detail["replies"])


def test_support_cannot_reply_after_closed(client, h) -> None:
    for target in ("RESOLVED", "CLOSED"):
        client.patch("/api/support/tickets/T10002/status", json={"status": target}, headers=h["s1"])
    r = client.post("/api/support/tickets/T10002/replies", json={"content": "迟到的回复"}, headers=h["s1"])
    assert r.status_code == 409
    assert r.json()["detail"]["type"] == "INVALID_STATE"


# --- 客户回复 ---


def test_customer_reply_own_ticket(client, h) -> None:
    r = client.post("/api/tickets/T10001/replies", json={"content": "补充：左耳在安静环境更明显"}, headers=h["u1"])
    assert r.status_code == 201
    assert r.json()["author_role"] == "CUSTOMER"
    detail = client.get("/api/tickets/T10001", headers=h["u1"]).json()
    assert detail["replies"][-1]["content"] == "补充：左耳在安静环境更明显"
    assert detail["events"][-1]["event_type"] == "REPLIED"
    assert detail["events"][-1]["reply_id"] == r.json()["id"]


def test_customer_cannot_reply_others_ticket(client, h) -> None:
    r = client.post("/api/tickets/T10002/replies", json={"content": "越权"}, headers=h["u1"])
    assert r.status_code == 403
    assert client.post("/api/tickets/T99999/replies", json={"content": "x"}, headers=h["u1"]).status_code == 404


def test_customer_cannot_reply_closed_ticket(client, h) -> None:
    for target in ("RESOLVED", "CLOSED"):
        client.patch("/api/support/tickets/T10002/status", json={"status": target}, headers=h["s1"])
    r = client.post("/api/tickets/T10002/replies", json={"content": "还想说一句"}, headers=h["u2"])
    assert r.status_code == 409
    assert r.json()["detail"]["type"] == "INVALID_STATE"


def test_customer_endpoints_forbidden_to_support(client, h) -> None:
    """客户工单接口独立鉴权：客服 token 不能冒充客户回复、反馈或建单。"""
    assert client.get("/api/tickets", headers=h["s1"]).status_code == 403
    assert client.post("/api/tickets/T10002/replies", json={"content": "x"}, headers=h["s1"]).status_code == 403
    assert client.post("/api/tickets/T10002/feedback", json={"rating": 5}, headers=h["s1"]).status_code == 403


# --- 反馈 ---


def test_feedback_requires_resolved(client, h) -> None:
    r = client.post("/api/tickets/T10001/feedback", json={"rating": 5, "comment": "好"}, headers=h["u1"])
    assert r.status_code == 409
    assert r.json()["detail"]["type"] == "INVALID_STATE"


def test_feedback_once_then_duplicate_409_and_queryable(client, h) -> None:
    _resolve_t10001(client, h)
    r = client.post("/api/tickets/T10001/feedback", json={"rating": 4, "comment": "处理及时"}, headers=h["u1"])
    assert r.status_code == 201
    assert r.json()["rating"] == 4

    dup = client.post("/api/tickets/T10001/feedback", json={"rating": 1, "comment": "改主意"}, headers=h["u1"])
    assert dup.status_code == 409
    assert dup.json()["detail"]["type"] == "DUPLICATE"

    mine = client.get("/api/tickets/T10001/feedback", headers=h["u1"])
    assert mine.status_code == 200 and mine.json()["rating"] == 4  # 首次提交不被覆盖
    assert client.get("/api/tickets/T10001", headers=h["u1"]).json()["feedback"]["comment"] == "处理及时"

    listed = client.get("/api/support/feedback", headers=h["s2"])
    assert listed.status_code == 200
    assert listed.json() == [
        {**listed.json()[0], "ticket_id": "T10001", "rating": 4, "user_id": "U001", "ticket_category": "REPAIR",
         "ticket_status": "RESOLVED", "order_id": "A10001"}
    ]
    assert client.get("/api/support/feedback", headers=h["u1"]).status_code == 403


def test_feedback_validation_and_ownership(client, h) -> None:
    _resolve_t10001(client, h)
    for bad in (0, 6):
        assert client.post("/api/tickets/T10001/feedback", json={"rating": bad}, headers=h["u1"]).status_code == 422
    assert client.post("/api/tickets/T10001/feedback", json={"rating": 5}, headers=h["u2"]).status_code == 403
    assert client.get("/api/tickets/T10001/feedback", headers=h["u1"]).status_code == 404  # 尚未提交


# --- 处理记录 ---


def test_timeline_records_full_lifecycle_in_order(client, h) -> None:
    created = client.post(
        "/api/tickets",
        json={"order_id": "A10002", "category": "REPAIR", "title": "表带断裂", "description": "佩戴一周开裂"},
        headers=h["u1"],
    )
    tid = created.json()["id"]
    client.post(f"/api/support/tickets/{tid}/claim", headers=h["s2"])
    client.patch(f"/api/support/tickets/{tid}/status", json={"status": "PROCESSING"}, headers=h["s2"])
    client.post(f"/api/support/tickets/{tid}/replies", json={"content": "已安排换新"}, headers=h["s2"])
    client.post(f"/api/tickets/{tid}/replies", json={"content": "谢谢"}, headers=h["u1"])
    client.patch(f"/api/support/tickets/{tid}/status", json={"status": "RESOLVED"}, headers=h["s2"])
    client.post(f"/api/tickets/{tid}/feedback", json={"rating": 5}, headers=h["u1"])

    support_view = client.get(f"/api/support/tickets/{tid}", headers=h["s2"]).json()
    steps = [(e["event_type"], e["actor_role"], e["actor_id"], e["from_status"], e["to_status"])
             for e in support_view["events"]]
    assert steps == [
        ("CREATED", "CUSTOMER", "U001", None, "OPEN"),
        ("CLAIMED", "SUPPORT", "SUPPORT002", None, None),
        ("STATUS_CHANGED", "SUPPORT", "SUPPORT002", "OPEN", "PROCESSING"),
        ("REPLIED", "SUPPORT", "SUPPORT002", None, None),
        ("REPLIED", "CUSTOMER", "U001", None, None),
        ("STATUS_CHANGED", "SUPPORT", "SUPPORT002", "PROCESSING", "RESOLVED"),
        ("FEEDBACK_SUBMITTED", "CUSTOMER", "U001", None, None),
    ]
    assert support_view["assignee_id"] == "SUPPORT002"


def test_customer_view_hides_support_internal_fields(client, h) -> None:
    body = client.get("/api/tickets/T10002", headers=h["u2"]).json()
    assert "assignee_id" not in body
    assert [e["event_type"] for e in body["events"]] == ["CREATED", "CLAIMED", "STATUS_CHANGED", "REPLIED"]
    for e in body["events"]:
        assert e["actor_id"] == ("U002" if e["actor_role"] == "CUSTOMER" else None)
    assert all(r["author_id"] is None for r in body["replies"] if r["author_role"] == "SUPPORT")
    assert "assignee_id" not in client.get("/api/tickets", headers=h["u2"]).json()[0]


def test_ticket_events_are_append_only_in_database(db) -> None:
    """DB 触发器兜底：即使绕过服务层，也不能改写或删除处理记录。"""
    with db() as s:
        with pytest.raises(Exception, match="append-only"):
            s.execute(text("UPDATE ticket_events SET actor_id = 'U002' WHERE ticket_id = 'T10001'"))
        s.rollback()
        with pytest.raises(Exception, match="append-only"):
            s.execute(text("DELETE FROM ticket_events WHERE ticket_id = 'T10001'"))
        s.rollback()
        assert s.execute(text("SELECT count(*) FROM ticket_events WHERE ticket_id = 'T10001'")).scalar() == 1


def test_failed_operations_leave_no_events(client, h) -> None:
    before = len(client.get("/api/support/tickets/T10002", headers=h["s1"]).json()["events"])
    client.patch("/api/support/tickets/T10002/status", json={"status": "OPEN"}, headers=h["s1"])  # 逆向 409
    client.post("/api/support/tickets/T10002/claim", headers=h["s2"])  # 已被领取 409
    client.post("/api/tickets/T10002/feedback", json={"rating": 3}, headers=h["u2"])  # 未解决 409
    assert len(client.get("/api/support/tickets/T10002", headers=h["s1"]).json()["events"]) == before
