"""Agent 状态定义（LangGraph State）与 pending_action 管理。

LangGraph State 是工作流状态模型；业务关键状态（pending_action 等）
最终持久化到 PostgreSQL Conversation 行（审核修订④），图运行结束后落库。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, TypedDict

from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models.conversation import Conversation


class Route(StrEnum):
    RAG = "RAG"
    ORDER_TOOL = "ORDER_TOOL"
    LOGISTICS_TOOL = "LOGISTICS_TOOL"
    TICKET_TOOL = "TICKET_TOOL"
    AFTER_SALES = "AFTER_SALES"
    AWAIT_CONFIRMATION = "AWAIT_CONFIRMATION"
    EXECUTE_CONFIRMED = "EXECUTE_CONFIRMED"
    DIRECT_LLM = "DIRECT_LLM"
    CLARIFY = "CLARIFY"
    HUMAN = "HUMAN"


PENDING_TTL_MINUTES = 10


class AgentState(TypedDict, total=False):
    # 基础设施（图内只读使用；service 层装配时注入）
    db: Any
    user_order_ids: list[str]
    # 输入
    user_id: str
    session_id: str
    user_query: str
    # 会话上下文（进图前装载）
    recent_messages: list[dict[str, str]]
    active_order_id: str | None
    active_ticket_id: str | None
    pending_action: dict[str, Any] | None  # {type, payload, id, expires_at}
    # 结构化确认入口（POST /api/chat/confirm）：YES | NO，及卡片上的 pending_action.id；
    # 为空时按聊天文本识别确认词。两种入口走同一 check_pending → execute_confirmed 路径
    decision: str | None
    expected_pending_id: str | None
    # 中间产物
    intent: str
    confirmation: str  # YES | NO | TOPIC_SWITCH
    entity_order_id: str | None
    entity_ticket_id: str | None
    ambiguous: bool
    route: str
    tool_calls: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    rag_answer: str
    rag_sources: list[dict[str, str]]
    rag_abstained: bool
    eligibility: dict[str, Any]
    # 输出
    final_answer: str
    # 回答方式：MODEL（模型生成）/ OFFLINE_ECHO（离线回显）/ TEMPLATE（确定性模板）/ ERROR
    answer_mode: str
    error_type: str | None
    # Trace 用计量
    prompt_tokens: int
    completion_tokens: int
    steps_used: int


def pending_expired(pending: dict[str, Any] | None, now: datetime | None = None) -> bool:
    if not pending or "expires_at" not in pending:
        return True
    expires: object = pending["expires_at"]
    exp_dt = datetime.fromisoformat(expires) if isinstance(expires, str) else expires
    now_dt = now or utcnow()
    return bool(now_dt >= exp_dt)  # type: ignore[operator]


def make_pending(action_type: str, payload: dict[str, Any], pending_id: str) -> dict[str, Any]:
    return {
        "type": action_type,
        "payload": payload,
        "id": pending_id,
        "expires_at": (utcnow() + timedelta(minutes=PENDING_TTL_MINUTES)).isoformat(),
    }


def public_pending(pending: dict[str, Any] | None) -> dict[str, Any] | None:
    """前端确认卡片所需字段；已过期视为不存在（过期的操作不能再被确认）。"""
    if not pending or pending_expired(pending):
        return None
    payload = pending.get("payload") or {}
    expires = pending["expires_at"]
    return {
        "id": str(pending["id"]),
        "type": str(pending["type"]),
        "expires_at": expires if isinstance(expires, str) else expires.isoformat(),
        "order_id": payload.get("order_id"),
        "category": payload.get("category"),
        "title": payload.get("title"),
    }


# --- 持久化桥（图运行前后调用）---


def load_state_from_conversation(conv: Conversation) -> dict[str, Any]:
    pending = None
    if conv.pending_action_type:
        pending = {
            "type": conv.pending_action_type,
            "payload": conv.pending_action_payload or {},
            "id": conv.pending_action_id,
            "expires_at": conv.pending_action_expires_at.isoformat()
            if conv.pending_action_expires_at
            else None,
        }
    return {
        "active_order_id": conv.active_order_id,
        "active_ticket_id": conv.active_ticket_id,
        "pending_action": pending,
    }


def save_state_to_conversation(
    db: Session,
    conv: Conversation,
    *,
    active_order_id: str | None,
    active_ticket_id: str | None,
    pending: dict[str, Any] | None,
) -> None:
    conv.active_order_id = active_order_id
    conv.active_ticket_id = active_ticket_id
    if pending is None:
        conv.pending_action_type = None
        conv.pending_action_payload = None
        conv.pending_action_id = None
        conv.pending_action_expires_at = None
    else:
        conv.pending_action_type = str(pending["type"])
        conv.pending_action_payload = dict(pending["payload"])
        conv.pending_action_id = str(pending["id"])
        exp = pending["expires_at"]
        conv.pending_action_expires_at = datetime.fromisoformat(exp) if isinstance(exp, str) else exp
    conv.updated_at = utcnow()
    db.commit()
