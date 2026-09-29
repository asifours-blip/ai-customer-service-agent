"""物流破损经审核历史案例：真实 PostgreSQL API / 权限 / 撤回 / 草案。"""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import select

from app.models.order import Order
from app.models.ticket import Ticket, TicketEvent, TicketReply
from tests.conftest import auth_headers

pytestmark = pytest.mark.integration


@pytest.fixture()
def damage_tickets(db) -> None:
    with db() as session:
        session.add_all([
            Ticket(
                id="T90001", user_id="U002", order_id="A20001", category="OTHER",
                title="运输中包装破损", description="联系 13800138000，住址私密，照片待补",
                status="RESOLVED", priority="MEDIUM", assignee_id="SUPPORT001",
            ),
            Ticket(
                id="T90002", user_id="U001", order_id="A10001", category="OTHER",
                title="物流破损待处理", description="当前客户说明商品破损",
                status="OPEN", priority="MEDIUM",
            ),
        ])
        session.commit()


@pytest.fixture()
def headers(client, damage_tickets) -> dict[str, dict[str, str]]:
    return {
        "support": auth_headers(client, "support_agent"),
        "other_support": auth_headers(client, "support_agent2"),
        "customer": auth_headers(client, "demo_customer"),
    }


def approve(client, headers, *, path="REQUEST_EVIDENCE"):
    return client.post(
        "/api/support/tickets/T90001/damage-case",
        headers=headers["support"],
        json={
            "damage_kind": "PRODUCT", "reviewed_path": path,
            "confirmed_logistics_damage": True,
        },
    )


def suggestions(client, headers, kind="PRODUCT"):
    return client.get(
        f"/api/support/tickets/T90002/damage-cases?damage_kind={kind}",
        headers=headers["support"],
    )


def test_case_is_explicitly_approved_then_retrieved_and_withdrawn(client, headers, db) -> None:
    before = suggestions(client, headers)
    assert before.status_code == 200, before.text
    assert before.json()["draft"]["status"] == "NO_CASES"
    assert before.json()["cases"] == []

    assert client.post(
        "/api/support/tickets/T90001/damage-case", headers=headers["support"],
        json={"damage_kind": "PRODUCT", "reviewed_path": "REQUEST_EVIDENCE"},
    ).status_code == 422
    first = approve(client, headers)
    assert first.status_code == 200, first.text
    assert first.json()["source_ticket_id"] == "T90001"
    assert approve(client, headers).status_code == 200  # 同参重试幂等
    assert approve(client, headers, path="REFUND_REVIEW").status_code == 409

    found = suggestions(client, headers)
    assert found.status_code == 200, found.text
    data = found.json()
    assert data["draft"]["status"] == "CASE_ASSISTED"
    assert data["cases"][0]["source_ticket_id"] == "T90001"
    assert data["draft"]["cited_cases"] == data["cases"]
    assert "T90001" in data["draft"]["next_step"]
    assert "商品" in data["cases"][0]["similarities"]  # 两个订单均为 P001
    assert "13800138000" not in found.text
    assert "私密" not in found.text
    assert suggestions(client, headers, kind="OUTER_PACKAGE").json()["cases"] == []

    with db() as session:
        order = session.get(Order, "A10001")
        assert order is not None and order.status == "DELIVERED"
        assert session.scalars(select(TicketEvent).where(TicketEvent.ticket_id == "T90002")).all() == []
        assert session.scalars(select(TicketReply).where(TicketReply.ticket_id == "T90002")).all() == []

    withdrawn = client.post("/api/support/tickets/T90001/damage-case/withdraw", headers=headers["support"])
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["withdrawn_at"] is not None
    assert withdrawn.json()["withdrawn_by"] == "SUPPORT001"
    repeated = client.post("/api/support/tickets/T90001/damage-case/withdraw", headers=headers["support"])
    assert repeated.status_code == 200
    assert suggestions(client, headers).json()["draft"]["status"] == "NO_CASES"
    assert approve(client, headers).status_code == 409


def test_support_permissions_and_source_requirements(client, headers) -> None:
    assert suggestions(client, headers).status_code == 200
    assert client.get(
        "/api/support/tickets/T90002/damage-cases?damage_kind=PRODUCT", headers=headers["customer"]
    ).status_code == 403
    assert client.get("/api/support/tickets/T90001/damage-case", headers=headers["customer"]).status_code == 403
    assert client.get("/api/support/tickets/T90001", headers=headers["customer"]).status_code == 403
    assert client.get("/api/support/tickets/T99999/damage-case", headers=headers["support"]).status_code == 404
    assert client.post(
        "/api/support/tickets/T90001/damage-case", headers=headers["other_support"],
        json={"damage_kind": "PRODUCT", "reviewed_path": "REQUEST_EVIDENCE", "confirmed_logistics_damage": True},
    ).status_code == 403
    assert client.post(
        "/api/support/tickets/T90002/damage-case", headers=headers["support"],
        json={"damage_kind": "PRODUCT", "reviewed_path": "REQUEST_EVIDENCE", "confirmed_logistics_damage": True},
    ).status_code == 403
    assert approve(client, headers).status_code == 200
    assert client.post(
        "/api/support/tickets/T90001/damage-case/withdraw", headers=headers["other_support"]
    ).status_code == 403


def test_old_policy_and_unknown_logistics_never_authorize_from_memory(client, headers, db, monkeypatch) -> None:
    assert approve(client, headers).status_code == 200
    with db() as session:
        from app.models.logistics import Logistics
        logistics = session.scalar(select(Logistics).where(Logistics.order_id == "A10001"))
        assert logistics is not None
        session.delete(logistics)
        session.commit()
    from app.services import damage_cases
    original = damage_cases.load_rules
    monkeypatch.setattr(damage_cases, "load_rules", lambda: replace(original(), policy_version="2027.01"))
    result = suggestions(client, headers)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["draft"]["status"] == "HISTORICAL_ONLY"
    assert data["cases"][0]["stale_policy"] is True
    assert "当前授权" in data["draft"]["next_step"]
    assert "当前物流状态未登记" in data["draft"]["missing_information"]
    assert "物流状态" not in data["cases"][0]["similarities"]
