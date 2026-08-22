"""售后资格确定性判定（v1.1 补丁 §2：禁止交给 LLM）。

LLM 可以解释 EligibilityResult，但不允许产生它。
所有时间/状态判断都基于订单事实与 policy/rules.yaml，可单测复现。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.models.order import Order
from app.services.policy_rules import PolicyRules, load_rules

REQUEST_TYPES = ("REFUND", "EXCHANGE", "REPAIR")


@dataclass(frozen=True)
class EligibilityResult:
    eligible: bool
    reason_code: str
    policy_rule: str
    details: dict[str, Any]


def _days_since(dt: datetime, now: datetime) -> int:
    return max(0, (now - dt).days)


def evaluate_after_sales(
    order: Order,
    request_type: str,
    current_time: datetime,
    rules: PolicyRules | None = None,
) -> EligibilityResult:
    """确定性计算售后资格。

    - REFUND：签收后 no_reason_return_within_days（7）天内且订单 DELIVERED
    - EXCHANGE：签收后 exchange_within_days（15）天内且 DELIVERED
    - REPAIR：签收后 warranty_months（12）个月内（保修，不看退货窗口）
    """
    r = rules or load_rules()
    request_type = request_type.upper()
    if request_type not in REQUEST_TYPES:
        raise ValueError(f"未知售后类型: {request_type}")

    if order.status not in r.allowed_order_status:
        return EligibilityResult(
            eligible=False,
            reason_code="ORDER_NOT_DELIVERED",
            policy_rule="REQUIRES_DELIVERED",
            details={"order_status": order.status, "allowed": list(r.allowed_order_status)},
        )

    assert order.delivered_at is not None  # DELIVERED 状态必有签收时间（seed/业务保证）
    delivered_days = _days_since(order.delivered_at, current_time)

    if request_type == "REFUND":
        window = r.no_reason_return_within_days
        within = delivered_days <= window
        rule = "RETURN_WITHIN_7_DAYS"
        return EligibilityResult(
            eligible=within,
            reason_code="WITHIN_RETURN_WINDOW" if within else "BEYOND_RETURN_WINDOW",
            policy_rule=rule,
            details={"delivered_days": delivered_days, "allowed_days": window},
        )
    if request_type == "EXCHANGE":
        within, window = delivered_days <= r.exchange_within_days, r.exchange_within_days
        return EligibilityResult(
            eligible=within,
            reason_code="WITHIN_EXCHANGE_WINDOW" if within else "BEYOND_EXCHANGE_WINDOW",
            policy_rule="EXCHANGE_WITHIN_15_DAYS",
            details={"delivered_days": delivered_days, "allowed_days": window},
        )
    # REPAIR（保修维修）
    within = delivered_days <= r.warranty_months * 30
    return EligibilityResult(
        eligible=within,
        reason_code="WITHIN_WARRANTY" if within else "BEYOND_WARRANTY",
        policy_rule="WARRANTY_12_MONTHS",
        details={"delivered_days": delivered_days, "allowed_days": r.warranty_months * 30},
    )
