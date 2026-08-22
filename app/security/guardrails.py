"""Prompt Injection 检测（软防线）。

定位（规格 §17）：检测器只做"标记与保守回复"，**不是安全边界**——
即使 LLM 被完全诱导，Tool 层的权限校验依然无法越权（硬防线在 service 层）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "instruction_override",
        re.compile(r"(忽略|无视| disregard |不要遵守)(之前|以上|全部|所有)?(的)?(规则|指令|设置|约束|提示)", re.I),
    ),
    ("role_hijack", re.compile(r"你(现在|马上)?(是|扮演|变成)(管理员|开发者|root|admin|超级用户|系统管理员)", re.I)),
    (
        "system_prompt_leak",
        re.compile(
            r"((输出|打印|告诉我|泄露|原样)(你)?的?(系统提示|system ?prompt|初始指令|系统指令|角色设定)"
            r"|(系统提示|system ?prompt|初始指令|系统指令|角色设定).*(是什么|告诉我|原样|输出|打印|泄露))",
            re.I,
        ),
    ),
    ("sql_injection", re.compile(r"(执行|运行|跑一下).*(select|insert|update|delete|drop\s+table)\b.*", re.I | re.S)),
    (
        "data_exfiltration",
        re.compile(r"(所有|全部)用户的?(订单|信息|数据|记录)|把.*(数据库|所有订单).*(发|给|告诉我)", re.I),
    ),
]


@dataclass(frozen=True)
class InjectionVerdict:
    flagged: bool
    patterns: list[str]


def detect_injection(text: str) -> InjectionVerdict:
    hits = [name for name, pat in _PATTERNS if pat.search(text)]
    return InjectionVerdict(flagged=bool(hits), patterns=hits)


GUARDRAIL_REPLY = (
    "抱歉，我无法执行该类请求。我只能为您查询您本人的订单、物流与工单信息，"
    "或解答产品与政策问题。如需其他帮助请说明具体需求。"
)
