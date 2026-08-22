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
    # 优先级（顺序执行，先命中先出）：
    # 1) 工单 2) 产品使用/参数 3) 政策疑问 4) 售后动作 5) 物流 6) 订单 7) 寒暄
    # PRODUCT 先于 POLICY：避免"续航是多长时间"被 多长期 误吞；POLICY 的政策词与产品词不重叠
    (Intent.TICKET_QUERY, r"(工单|售后单|处理进度)"),
    (Intent.PRODUCT_QA,
     r"(续航|防水|蓝牙|配对|连接|怎么用|怎么设置|充电|功能|参数|说明书|降噪|怎么测|指示灯|恢复出厂|串联|表带|血氧|游泳|泡在水|泡水|洗澡|淋浴|同步数据|无法.{0,8}(连接|同步|开机|充电|发声))"),
    (Intent.POLICY_QA,
     r"(政策|保修|质保|流程|运费|包邮|到账|工作日|范围|时限|多久不更新|查件|工作时间|转人工|防诈骗|账户|个人信息|不予退款|无理由|修改.{0,4}地址|多长时间|需要多久)"),
    (Intent.AFTER_SALES,
     r"(申请|想退|想换|要退|要换|要求.{0,3}(退|换|修)|坏了|出问题|故障|报修|退货退款|维修|售后|退换)"),
    (Intent.LOGISTICS_QUERY, r"(物流|快递|运单|发货|签收|配送|到哪|什么时候.{0,2}到|多久到)"),
    (Intent.ORDER_QUERY, r"(订单|A\d{5}|买了什么|我的.{0,4}单)"),
    (Intent.CHITCHAT, r"(你好|您好|在吗|谢谢|感谢|再见|拜拜|你是谁)"),
]

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
