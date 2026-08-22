"""工具集成测试：每工具六类异常用例（规格 §21）+ 幂等语义。真实 PostgreSQL。"""

from __future__ import annotations

import pytest

from app.tools import ToolKind, build_registry

pytestmark = [pytest.mark.tools, pytest.mark.integration]


@pytest.fixture()
def registry():
    return build_registry()


def _run(registry, db, tool_name: str, user_id: str, args: dict):
    return registry.require(tool_name).execute(db, user_id, args)


# --- query_order 六类 ---


def test_query_order_happy(db, registry) -> None:
    with db() as s:
        r = _run(registry, s, "query_order", "U001", {"order_id": "A10001"})
    assert r.success
    assert r.data["status"] == "DELIVERED"
    assert r.data["order_id"] == "A10001"
    assert registry.require("query_order").kind == ToolKind.READ_ONLY


def test_query_order_not_found(db, registry) -> None:
    with db() as s:
        r = _run(registry, s, "query_order", "U001", {"order_id": "A99999"})
    assert not r.success
    assert r.error["type"] == "NOT_FOUND"


def test_query_order_other_users_order_idor(db, registry) -> None:
    with db() as s:
        r = _run(registry, s, "query_order", "U001", {"order_id": "A20001"})
    assert not r.success
    assert r.error["type"] == "PERMISSION_DENIED"
    assert "无权" in r.error["message"]


def test_query_order_missing_arg(db, registry) -> None:
    with db() as s:
        r = _run(registry, s, "query_order", "U001", {})
    assert not r.success
    assert r.error["type"] == "VALIDATION_FAILED"


def test_query_order_illegal_arg(db, registry) -> None:
    with db() as s:
        r = _run(registry, s, "query_order", "U001", {"order_id": "DROP TABLE"})
    assert not r.success
    assert r.error["type"] == "VALIDATION_FAILED"


def test_query_order_db_error_sanitized(db, registry, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import orders as order_service

    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise RuntimeError("psycopg 内部连接串 postgres://app:secret@... 泄漏细节")

    monkeypatch.setattr(order_service, "get_order", boom)
    with db() as s:
        r = _run(registry, s, "query_order", "U001", {"order_id": "A10001"})
    assert not r.success
    assert r.error["type"] == "TOOL_EXECUTION_FAILED"
    assert "secret" not in r.error["message"]  # DB 异常细节不外泄


# --- query_logistics ---


def test_query_logistics_happy(db, registry) -> None:
    with db() as s:
        r = _run(registry, s, "query_logistics", "U001", {"order_id": "A10002"})
    assert r.success
    assert r.data["status"] == "派送中"
    assert r.data["carrier"] == "中通快递"


def test_query_logistics_no_record(db, registry) -> None:
    with db() as s:
        r = _run(registry, s, "query_logistics", "U001", {"order_id": "A10003"})
    assert not r.success
    assert r.error["type"] == "NOT_FOUND"


def test_query_logistics_idor(db, registry) -> None:
    with db() as s:
        r = _run(registry, s, "query_logistics", "U001", {"order_id": "A20001"})
    assert not r.success
    assert r.error["type"] == "PERMISSION_DENIED"


# --- query_ticket ---


def test_query_ticket_happy(db, registry) -> None:
    with db() as s:
        r = _run(registry, s, "query_ticket", "U001", {"ticket_id": "T10001"})
    assert r.success
    assert r.data["status"] == "OPEN"


def test_query_ticket_idor(db, registry) -> None:
    with db() as s:
        r = _run(registry, s, "query_ticket", "U001", {"ticket_id": "T10002"})
    assert not r.success
    assert r.error["type"] == "PERMISSION_DENIED"


def test_query_ticket_not_found(db, registry) -> None:
    with db() as s:
        r = _run(registry, s, "query_ticket", "U001", {"ticket_id": "T99999"})
    assert not r.success
    assert r.error["type"] == "NOT_FOUND"


# --- create_ticket：幂等核心 ---


def test_create_ticket_happy_and_idempotent_replay(db, registry) -> None:
    args = {
        "order_id": "A10002",
        "category": "REPAIR",
        "title": "手表表带断裂",
        "description": "佩戴一周开裂",
        "idempotency_key": "pa-20260823-0001",
    }
    with db() as s:
        first = _run(registry, s, "create_ticket", "U001", args)
        assert first.success
        assert first.data["created"] is True
        assert first.idempotent_replay is False
        ticket_id = first.data["ticket_id"]

        # 模拟"写入成功但响应超时→重试"：同 key 重放必须返回同一张工单
        replay = _run(registry, s, "create_ticket", "U001", args)
        assert replay.success
        assert replay.data["ticket_id"] == ticket_id
        assert replay.data["created"] is False
        assert replay.idempotent_replay is True

        # 库里确实只有一张
        from sqlalchemy import func, select

        from app.models.ticket import Ticket

        n = s.scalar(select(func.count()).select_from(Ticket).where(Ticket.idempotency_key == args["idempotency_key"]))
        assert n == 1


def test_create_ticket_requires_idempotency_key(db, registry) -> None:
    with db() as s:
        r = _run(
            registry, s, "create_ticket", "U001",
            {"order_id": "A10002", "category": "REPAIR", "title": "无幂等键"},
        )
    assert not r.success
    assert r.error["type"] == "VALIDATION_FAILED"


def test_create_ticket_idor(db, registry) -> None:
    with db() as s:
        r = _run(
            registry, s, "create_ticket", "U001",
            {
                "order_id": "A20001",
                "category": "REFUND",
                "title": "越权建单",
                "idempotency_key": "pa-evil-0001",
            },
        )
    assert not r.success
    assert r.error["type"] == "PERMISSION_DENIED"


def test_create_ticket_duplicate_active(db, registry) -> None:
    with db() as s:
        r = _run(
            registry, s, "create_ticket", "U001",
            {
                "order_id": "A10001",
                "category": "REPAIR",
                "title": "重复报修",
                "idempotency_key": "pa-dup-00001",
            },
        )
    assert not r.success
    assert r.error["type"] == "DUPLICATE"  # seed 已有 T10001 同类进行中


def test_create_ticket_bad_category(db, registry) -> None:
    with db() as s:
        r = _run(
            registry, s, "create_ticket", "U001",
            {"category": "GRANT_ADMIN", "title": "注入类别", "idempotency_key": "pa-inj-00001"},
        )
    assert not r.success
    assert r.error["type"] == "VALIDATION_FAILED"


def test_unknown_tool_name(registry) -> None:
    import pytest as _pytest

    with _pytest.raises(KeyError):
        registry.require("format_disk")
