"""Agent 纯单测：意图 / 确认词 / 实体消解 / 资格判定。零 DB（资格用内存 Order 对象）。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.agent.entity import SessionEntities, resolve_entities
from app.agent.router import Confirmation, Intent, RuleBasedIntentClassifier, detect_confirmation
from app.models.base import utcnow
from app.models.order import Order
from app.services.eligibility import evaluate_after_sales
from app.services.policy_rules import load_rules

# --- 意图 ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("这个耳机支持多久保修？", Intent.POLICY_QA),
        ("帮我查一下订单 A10001", Intent.ORDER_QUERY),
        ("A10001 到哪了？", Intent.LOGISTICS_QUERY),
        ("我的耳机用了三天坏了，想申请退款", Intent.AFTER_SALES),
        ("我的工单处理得怎么样了 T10001", Intent.TICKET_QUERY),
        ("耳机的续航是多长时间", Intent.PRODUCT_QA),
        ("你好，在吗", Intent.CHITCHAT),
        ("给我讲个笑话", Intent.UNKNOWN),
    ],
)
def test_intent_rules(text: str, expected: Intent) -> None:
    assert RuleBasedIntentClassifier().classify(text).intent == expected


# --- 确认词 ---


@pytest.mark.parametrize("text", ["好的", "可以", "确认", "帮我申请", "嗯"],)
def test_confirmation_yes(text: str) -> None:
    assert detect_confirmation(text) is Confirmation.YES


@pytest.mark.parametrize("text", ["不用了", "取消", "算了", "先不"],)
def test_confirmation_no(text: str) -> None:
    assert detect_confirmation(text) is Confirmation.NO


def test_confirmation_topic_switch() -> None:
    assert detect_confirmation("对了，耳机支持蓝牙5.4吗") is Confirmation.TOPIC_SWITCH


# --- 实体 ---


def test_entity_from_text() -> None:
    r = resolve_entities("看看 A10002 的物流", SessionEntities(), ["A10001", "A10002"])
    assert r.order_id == "A10002"
    assert not r.ambiguous


def test_entity_pronoun_uses_session() -> None:
    """Demo ③：'那它大概什么时候到' → 它 = active_order_id。"""
    r = resolve_entities("那它大概什么时候到？", SessionEntities(active_order_id="A10001"), ["A10001"])
    assert r.order_id == "A10001"


def test_entity_pronoun_without_session_ambiguous() -> None:
    r = resolve_entities("刚才那个订单到哪了", SessionEntities(), ["A10001", "A10002"])
    assert r.ambiguous  # 无 active 且多候选 → 不猜


def test_entity_ticket() -> None:
    r = resolve_entities("工单 T10001 进度", SessionEntities(), [])
    assert r.ticket_id == "T10001"


# --- 资格（确定性）---


def _order(status: str = "DELIVERED", delivered_days_ago: int | None = 3) -> Order:
    return Order(
        id="A10001",
        user_id="U001",
        product_id="P001",
        status=status,
        amount=299,
        created_at=utcnow() - timedelta(days=30),
        delivered_at=utcnow() - timedelta(days=delivered_days_ago) if delivered_days_ago is not None else None,
    )


def test_eligibility_refund_within_7_days() -> None:
    """Demo ④ 核心：签收 3 天 → 符合 7 天退货。"""
    r = evaluate_after_sales(_order(delivered_days_ago=3), "REFUND", utcnow())
    assert r.eligible
    assert r.reason_code == "WITHIN_RETURN_WINDOW"
    assert r.policy_rule == "RETURN_WITHIN_7_DAYS"
    assert r.details["delivered_days"] == 3
    assert r.details["allowed_days"] == load_rules().no_reason_return_within_days


def test_eligibility_refund_beyond_window() -> None:
    """A20001 场景：签收 20 天 → 超期拒绝。"""
    r = evaluate_after_sales(_order(delivered_days_ago=20), "REFUND", utcnow())
    assert not r.eligible
    assert r.reason_code == "BEYOND_RETURN_WINDOW"


def test_eligibility_not_delivered() -> None:
    r = evaluate_after_sales(_order(status="PENDING", delivered_days_ago=None), "REFUND", utcnow())
    assert not r.eligible
    assert r.reason_code == "ORDER_NOT_DELIVERED"


def test_eligibility_exchange_window_15() -> None:
    assert evaluate_after_sales(_order(delivered_days_ago=10), "EXCHANGE", utcnow()).eligible
    assert not evaluate_after_sales(_order(delivered_days_ago=20), "EXCHANGE", utcnow()).eligible


def test_eligibility_warranty_12_months() -> None:
    assert evaluate_after_sales(_order(delivered_days_ago=200), "REPAIR", utcnow()).eligible
    assert not evaluate_after_sales(_order(delivered_days_ago=400), "REPAIR", utcnow()).eligible


def test_eligibility_unknown_type_rejected() -> None:
    with pytest.raises(ValueError, match="未知售后类型"):
        evaluate_after_sales(_order(), "EXPLODE", utcnow())
