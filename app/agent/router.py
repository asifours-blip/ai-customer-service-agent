"""意图分类与确认词识别。

- RuleBasedIntentClassifier：确定性关键词规则（离线/CI/兜底）
- LLMIntentClassifier：Structured Output JSON（live，deepseek json_object）
- ConfirmationDetector：YES / NO / TOPIC_SWITCH 三分支（v1.1 补丁 §4）
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.llm.client import LLMClient


class Intent(StrEnum):
    PRODUCT_QA = "PRODUCT_QA"
    POLICY_QA = "POLICY_QA"
    ORDER_QUERY = "ORDER_QUERY"
    LOGISTICS_QUERY = "LOGISTICS_QUERY"
    AFTER_SALES = "AFTER_SALES"
    TICKET_QUERY = "TICKET_QUERY"
    CHITCHAT = "CHITCHAT"
    UNKNOWN = "UNKNOWN"


INTENTS: tuple[str, ...] = tuple(i.value for i in Intent)


@dataclass(frozen=True)
class IntentResult:
    intent: Intent
    confidence: float
    required_tool: str | None


_RULES: list[tuple[Intent, str]] = [
    # 顺序即优先级：AFTER_SALES 动作词优先于 POLICY_QA 疑问词
    (Intent.AFTER_SALES, r"(退款|退货|换货|换一个|维修|报修|坏了|坏了|出问题|故障|售后|申请退|想退|想换)"),
    (Intent.TICKET_QUERY, r"(工单|售后单|处理进度|我的单子怎么样)"),
    (Intent.LOGISTICS_QUERY, r"(物流|快递|到哪|什么时候到|多久到|配送|签收|发货|运单)"),
    (Intent.ORDER_QUERY, r"(订单|买了什么|我的.*单|订单号|查.*A\d{5})"),
    (Intent.POLICY_QA, r"(政策|保修|质保|退.*流程|运费|几天|多久能|能不能退|支持.*吗)"),  # noqa: E501
    (Intent.PRODUCT_QA,
     r"(续航|防水|蓝牙|配对|连接|怎么用|怎么设置|充电|功能|参数|说明书|降噪|怎么测|指示灯|恢复出厂)"),
    (Intent.CHITCHAT, r"(你好|您好|在吗|谢谢|感谢|再见|拜拜|你是谁)"),
]

# POLICY_QA 的动作型关键词让位给 AFTER_SALES 后，纯疑问保留在这里
_COMPILED = [(intent, re.compile(pat)) for intent, pat in _RULES]


class IntentClassifier(Protocol):
    def classify(self, text: str) -> IntentResult: ...


class RuleBasedIntentClassifier:
    def classify(self, text: str) -> IntentResult:
        for intent, pat in _COMPILED:
            if pat.search(text):
                return IntentResult(intent, 0.9, None)
        return IntentResult(Intent.UNKNOWN, 0.3, None)


_TOOL_BY_INTENT: dict[Intent, str | None] = {
    Intent.ORDER_QUERY: "query_order",
    Intent.LOGISTICS_QUERY: "query_logistics",
    Intent.TICKET_QUERY: "query_ticket",
    Intent.AFTER_SALES: "create_ticket",
}


class LLMIntentClassifier:
    """live 路径：JSON Structured Output；解析失败回退规则分类（可靠性优先）。"""

    def __init__(self, llm: LLMClient, fallback: IntentClassifier | None = None) -> None:
        self.llm = llm
        self.fallback = fallback or RuleBasedIntentClassifier()

    def classify(self, text: str) -> IntentResult:
        system = (
            "你是客服意图分类器。只输出 JSON："
            '{"intent": "PRODUCT_QA|POLICY_QA|ORDER_QUERY|LOGISTICS_QUERY|AFTER_SALES|TICKET_QUERY|CHITCHAT|UNKNOWN", '
            '"confidence": 0.0-1.0}。AFTER_SALES=用户想申请售后动作（退/换/修）；'
            "POLICY_QA=询问政策规则；PRODUCT_QA=产品使用问题。"
        )
        try:
            resp = self.llm.complete(system, text, max_tokens=100)
            data = json.loads(resp.content.strip().removeprefix("```json").removesuffix("```").strip())
            intent = Intent(str(data["intent"]))
            confidence = float(data.get("confidence", 0.8))
            return IntentResult(intent, confidence, _TOOL_BY_INTENT.get(intent))
        except Exception:  # noqa: BLE001
            fb = self.fallback.classify(text)
            return IntentResult(fb.intent, fb.confidence, _TOOL_BY_INTENT.get(fb.intent, fb.required_tool))


# --- 确认识别（v1.1 补丁 §4）---

_CONFIRM_YES = re.compile(r"^(好的?|可以|确认|是|嗯|行|帮我(申请|办|处理)|没问题|OK|ok)[。！!～~\s了]*$")
_CONFIRM_NO = re.compile(r"^(不用了?|不要|取消了?|算了|先不|再想想|NO|no)[。！!～~\s了]*$")


class Confirmation(StrEnum):
    YES = "YES"
    NO = "NO"
    TOPIC_SWITCH = "TOPIC_SWITCH"


def detect_confirmation(text: str) -> Confirmation:
    t = text.strip()
    if _CONFIRM_YES.match(t):
        return Confirmation.YES
    if _CONFIRM_NO.match(t):
        return Confirmation.NO
    return Confirmation.TOPIC_SWITCH
