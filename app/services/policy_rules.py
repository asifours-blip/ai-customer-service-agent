"""policy/rules.yaml 加载器：确定性业务规则的唯一来源（v1.1 补丁）。

Trace 同时记录 policy_document_version（知识库文档）与本模块的 rules_version，
两者通过 policy_version 对应，防止"文档说 7 天、代码算 5 天"。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

RULES_PATH = Path(__file__).resolve().parents[2] / "policy" / "rules.yaml"


@dataclass(frozen=True)
class PolicyRules:
    policy_version: str
    rules_version: str
    no_reason_return_within_days: int
    quality_return_within_days: int
    exchange_within_days: int
    warranty_months: int
    battery_months: int
    accessory_months: int
    allowed_order_status: tuple[str, ...]


@lru_cache
def load_rules(path: str | None = None) -> PolicyRules:
    data = yaml.safe_load((Path(path) if path else RULES_PATH).read_text(encoding="utf-8"))
    refund = data["refund"]
    exchange = data["exchange"]
    warranty = data["warranty"]
    return PolicyRules(
        policy_version=str(data["policy_version"]),
        rules_version=str(data["rules_version"]),
        no_reason_return_within_days=int(refund["no_reason_return_within_days"]),
        quality_return_within_days=int(refund["quality_return_within_days"]),
        exchange_within_days=int(exchange["within_days"]),
        warranty_months=int(warranty["months"]),
        battery_months=int(warranty["battery_months"]),
        accessory_months=int(warranty["accessory_months"]),
        allowed_order_status=tuple(refund["allowed_order_status"]),
    )
