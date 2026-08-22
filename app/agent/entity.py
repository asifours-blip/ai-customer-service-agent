"""实体消解（v1.1 补丁 §3）：显式状态 > 对话推断 > 猜测。

- 从文本提取 order_id / ticket_id（正则，必须经后端验证存在性与权限）
- session active_* 状态参与消解（"它/这个/那个"）
- 多候选歧义 → CLARIFY，不得猜
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ORDER_RE = re.compile(r"\bA\d{5}\b")
_TICKET_RE = re.compile(r"\bT\d{5}\b")
_PRONOUN_RE = re.compile(r"(它|这个|那个|这台|这支|刚才)")


@dataclass(frozen=True)
class EntityResolution:
    order_id: str | None
    ticket_id: str | None
    ambiguous: bool = False  # True → 上层进入 CLARIFY


@dataclass(frozen=True)
class SessionEntities:
    """持久化在 Conversation 的显式状态（审核修订④）。"""

    active_order_id: str | None = None
    active_ticket_id: str | None = None


def resolve_entities(text: str, session: SessionEntities, user_order_ids: list[str]) -> EntityResolution:
    """消解当前消息涉及的订单/工单。

    user_order_ids：当前用户拥有的订单号集合（由后端查询，非 LLM 提供）。
    规则：
    1. 文本中出现 ID → 直接采用（后端随后验证存在性与权限）
    2. 指代词 + session 有唯一 active → 采用 active
    3. 无指代无 ID：
       - session 有唯一 active 且意图需要实体 → 沿用 active（"那它什么时候到"）
       - session 无 active 且意图需要实体 → None（上层追问）
    4. 歧义标记由上层结合意图决定是否 CLARIFY
    """
    orders = _ORDER_RE.findall(text)
    tickets = _TICKET_RE.findall(text)

    order_id = orders[-1] if orders else None
    ticket_id = tickets[-1] if tickets else None
    ambiguous = len(set(orders)) > 1

    if order_id is None and _PRONOUN_RE.search(text) and session.active_order_id:
        order_id = session.active_order_id
    if ticket_id is None and _PRONOUN_RE.search(text) and session.active_ticket_id:
        ticket_id = session.active_ticket_id
    # 泛指续话回退："物流更新了吗/订单怎么样"（无显式 ID、无指代词）→ 沿用最近活跃实体（对话推断层）
    if order_id is None and session.active_order_id and re.search(r"(物流|快递|订单|单子)", text):
        order_id = session.active_order_id
    if ticket_id is None and session.active_ticket_id and "工单" in text:
        ticket_id = session.active_ticket_id

    # "刚才那个订单"但 session 无 active：无法唯一确定 → 不猜
    if (
        order_id is None
        and _PRONOUN_RE.search(text)
        and re.search(r"订单|单子", text)
        and not session.active_order_id
        and len(user_order_ids) > 1
    ):
        ambiguous = True

    return EntityResolution(order_id=order_id, ticket_id=ticket_id, ambiguous=ambiguous)
