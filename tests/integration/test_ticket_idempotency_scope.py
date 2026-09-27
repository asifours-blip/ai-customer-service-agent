"""幂等键作用域与请求指纹回归：真实 PostgreSQL。

- 幂等键只在同一用户内生效：B 提交 A 用过的 key 不得拿到 A 的工单
- 同一用户、同一 key、业务字段不同 → 冲突（409），不能静默当成重放
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models.ticket import Ticket
from app.services import tickets as ticket_service
from app.services.errors import AppError
from tests.conftest import auth_headers

pytestmark = pytest.mark.integration

_BASE_REQUEST = {
    "category": "OTHER",
    "title": "发票抬头修改",
    "description": "请把发票抬头改成公司名称。",
    "priority": "MEDIUM",
    "order_id": None,
}


def _count_by_key(factory, key: str) -> int:
    with factory() as s:
        return int(s.scalar(select(func.count()).select_from(Ticket).where(Ticket.idempotency_key == key)) or 0)


# ---------- 问题 1：跨用户泄露 ----------


def test_service_idempotency_key_is_scoped_to_owner(db) -> None:
    key = "pa-scope-svc-0001"
    with db() as s:
        a_ticket, a_created = ticket_service.create_ticket(s, user_id="U001", idempotency_key=key, **_BASE_REQUEST)
        a_id = a_ticket.id
    assert a_created is True

    with db() as s:
        b_ticket, b_created = ticket_service.create_ticket(s, user_id="U002", idempotency_key=key, **_BASE_REQUEST)
        b_id, b_owner = b_ticket.id, b_ticket.user_id

    # B 必须得到自己的新工单，而不是 A 的工单
    assert b_owner == "U002"
    assert b_id != a_id
    assert b_created is True
    assert _count_by_key(db, key) == 2
    with db() as s:
        a_after = s.get(Ticket, a_id)
        assert a_after is not None and a_after.user_id == "U001"


def test_rest_idempotency_key_does_not_leak_other_users_ticket(client) -> None:
    key = "pa-scope-api-0001"
    payload = {"category": "OTHER", "title": "发票抬头修改", "idempotency_key": key}
    first = client.post("/api/tickets", json=payload, headers=auth_headers(client, "demo_customer"))
    assert first.status_code == 201, first.text

    stolen = client.post("/api/tickets", json=payload, headers=auth_headers(client, "second_customer"))
    assert stolen.status_code == 201, stolen.text
    body = stolen.json()
    # 修复前：返回 U001 的工单（id/user_id/description 全部泄露给 U002）
    assert body["user_id"] == "U002"
    assert body["id"] != first.json()["id"]

    # A 的原工单仍只属于 A，B 仍无法读取
    denied = client.get(f"/api/tickets/{first.json()['id']}", headers=auth_headers(client, "second_customer"))
    assert denied.status_code == 403


# ---------- 问题 2：同 key 不同请求 ----------

_CHANGED_FIELDS = [
    ("category", "REPAIR"),
    ("title", "发票抬头修改（改）"),
    ("description", "换一个描述"),
    ("priority", "HIGH"),
    ("order_id", "A10002"),
]


@pytest.mark.parametrize(("field", "value"), _CHANGED_FIELDS)
def test_service_same_key_different_request_conflicts(db, field: str, value: str) -> None:
    key = f"pa-fp-svc-{field}"
    with db() as s:
        original, created = ticket_service.create_ticket(s, user_id="U001", idempotency_key=key, **_BASE_REQUEST)
        original_id = original.id
    assert created is True

    changed = {**_BASE_REQUEST, field: value}
    with db() as s, pytest.raises(AppError) as exc_info:
        ticket_service.create_ticket(s, user_id="U001", idempotency_key=key, **changed)
    assert exc_info.value.code == "IDEMPOTENCY_CONFLICT"
    assert exc_info.value.http_status == 409
    assert _count_by_key(db, key) == 1

    # 完全相同的请求仍是合法重放
    with db() as s:
        replay, replay_created = ticket_service.create_ticket(s, user_id="U001", idempotency_key=key, **_BASE_REQUEST)
        assert replay.id == original_id
        assert replay_created is False


def test_rest_same_key_different_request_returns_409(client) -> None:
    h = auth_headers(client, "demo_customer")
    key = "pa-fp-api-0001"
    first = client.post(
        "/api/tickets",
        json={"order_id": "A10002", "category": "REPAIR", "title": "手表表带断裂", "idempotency_key": key},
        headers=h,
    )
    assert first.status_code == 201, first.text

    conflict = client.post(
        "/api/tickets",
        json={"order_id": "A10002", "category": "EXCHANGE", "title": "想换个颜色", "idempotency_key": key},
        headers=h,
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["type"] == "IDEMPOTENCY_CONFLICT"

    listed = client.get("/api/tickets", headers=h).json()
    assert [t["id"] for t in listed].count(first.json()["id"]) == 1
    assert all(t["category"] != "EXCHANGE" for t in listed)
