"""集成测试：认证 / 订单（含 IDOR）/ 工单 / SUPPORT 状态机与回复。真实 PostgreSQL。"""

from __future__ import annotations

from tests.integration.conftest import auth_headers, login


def test_health(client) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200


def test_login_wrong_password(client) -> None:
    resp = client.post("/api/auth/login", json={"username": "demo_customer", "password": "nope"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["type"] == "AUTHENTICATION_FAILED"


def test_login_unknown_user(client) -> None:
    resp = client.post("/api/auth/login", json={"username": "ghost", "password": "demo123"})
    assert resp.status_code == 401  # 与口令错误同信息，不泄露用户存在性


def test_login_ok_and_identity(client) -> None:
    token = login(client, "demo_customer")
    assert token
    resp = client.get("/api/orders", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_missing_token_401(client) -> None:
    assert client.get("/api/orders").status_code == 401


# --- 订单与 IDOR ---


def test_orders_list_only_own(client) -> None:
    resp = client.get("/api/orders", headers=auth_headers(client, "demo_customer"))
    assert resp.status_code == 200
    ids = [o["id"] for o in resp.json()]
    assert ids == ["A10001", "A10002", "A10003", "A10004"]  # U001 全部订单


def test_order_idor_denied(client) -> None:
    """核心安全用例：U001 请求 U002 的合法订单 → 403 PERMISSION_DENIED。"""
    resp = client.get("/api/orders/A20001", headers=auth_headers(client, "demo_customer"))
    assert resp.status_code == 403
    assert resp.json()["detail"]["type"] == "PERMISSION_DENIED"


def test_order_not_found(client) -> None:
    resp = client.get("/api/orders/A99999", headers=auth_headers(client, "demo_customer"))
    assert resp.status_code == 404


def test_order_detail_with_logistics(client) -> None:
    resp = client.get("/api/orders/A10001", headers=auth_headers(client, "demo_customer"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "DELIVERED"
    assert body["logistics"]["carrier"] == "顺丰速运"


def test_logistics_requires_ownership(client) -> None:
    resp = client.get("/api/orders/logistics/A20001", headers=auth_headers(client, "demo_customer"))
    assert resp.status_code == 403


# --- 工单 ---


def test_ticket_create_and_idempotency(client) -> None:
    h = auth_headers(client, "demo_customer")
    payload = {
        "order_id": "A10002",
        "category": "REPAIR",
        "title": "手表表带断裂",
        "description": "佩戴一周表带开裂。",
        "idempotency_key": "pa-0001-idem-1234",
    }
    first = client.post("/api/tickets", json=payload, headers=h)
    assert first.status_code == 201, first.text
    second = client.post("/api/tickets", json=payload, headers=h)
    assert second.status_code == 201  # 幂等重放：返回首次工单而非新建
    assert first.json()["id"] == second.json()["id"]


def test_ticket_create_for_others_order_denied(client) -> None:
    resp = client.post(
        "/api/tickets",
        json={"order_id": "A20001", "category": "REFUND", "title": "越权创建"},
        headers=auth_headers(client, "demo_customer"),
    )
    assert resp.status_code == 403


def test_ticket_duplicate_category_blocked(client) -> None:
    h = auth_headers(client, "demo_customer")
    # seed 已有 T10001：U001/A10001/REPAIR/OPEN
    resp = client.post(
        "/api/tickets",
        json={"order_id": "A10001", "category": "REPAIR", "title": "重复报修"},
        headers=h,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["type"] == "DUPLICATE"


def test_ticket_idor_denied(client) -> None:
    resp = client.get("/api/tickets/T10002", headers=auth_headers(client, "demo_customer"))
    assert resp.status_code == 403


def test_ticket_validation_rejects_bad_category(client) -> None:
    resp = client.post(
        "/api/tickets",
        json={"category": "EXPLODE", "title": "x"},
        headers=auth_headers(client, "demo_customer"),
    )
    assert resp.status_code == 422  # Pydantic 层拦截


# --- SUPPORT ---


def test_support_endpoints_forbidden_to_customer(client) -> None:
    h = auth_headers(client, "demo_customer")
    assert client.get("/api/support/tickets", headers=h).status_code == 403
    patch = client.patch(
        "/api/support/tickets/T10001/status", json={"status": "PROCESSING"}, headers=h
    )
    assert patch.status_code == 403


def test_support_lists_all_tickets(client) -> None:
    resp = client.get("/api/support/tickets", headers=auth_headers(client, "support_agent"))
    assert resp.status_code == 200
    assert {t["id"] for t in resp.json()} == {"T10001", "T10002"}


def test_support_full_lifecycle_and_reverse_blocked(client) -> None:
    h = auth_headers(client, "support_agent")

    # 逆向被拒
    bad = client.patch("/api/support/tickets/T10002/status", json={"status": "OPEN"}, headers=h)
    assert bad.status_code == 409
    assert bad.json()["detail"]["type"] == "INVALID_TRANSITION"

    # 正向链：PROCESSING(seed) → RESOLVED → CLOSED
    ok1 = client.patch("/api/support/tickets/T10002/status", json={"status": "RESOLVED"}, headers=h)
    assert ok1.status_code == 200
    ok2 = client.patch("/api/support/tickets/T10002/status", json={"status": "CLOSED"}, headers=h)
    assert ok2.status_code == 200
    assert ok2.json()["status"] == "CLOSED"

    # CLOSED 后再迁移全部非法
    assert client.patch("/api/support/tickets/T10002/status", json={"status": "RESOLVED"}, headers=h).status_code == 409


def test_support_reply_visible_to_customer(client) -> None:
    s = auth_headers(client, "support_agent")
    resp = client.post(
        "/api/support/tickets/T10001/replies", json={"content": "已收到，正在安排检测。"}, headers=s
    )
    assert resp.status_code == 201

    c = auth_headers(client, "demo_customer")
    detail = client.get("/api/tickets/T10001", headers=c)
    assert detail.status_code == 200
    replies = detail.json()["replies"]
    assert any(r["content"] == "已收到，正在安排检测。" and r["author_role"] == "SUPPORT" for r in replies)


# --- 会话 ---


def test_conversations(client) -> None:
    h = auth_headers(client, "demo_customer")
    resp = client.get("/api/conversations", headers=h)
    assert resp.status_code == 200
    assert len(resp.json()) == 1  # seed 的演示会话
    detail = client.get(f"/api/conversations/{resp.json()[0]['id']}", headers=h)
    assert detail.status_code == 200
    assert detail.json()["messages"] == []
