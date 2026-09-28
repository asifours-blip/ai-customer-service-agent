"""Agent 编排服务：会话装载 → 图执行 → 状态/消息/Trace 落库。

Trace 原则（规格 §30）：每个请求一个 trace_id，失败 case 可完整复盘。
每次 LLM 调用（成功或失败）都记入 Trace.llm_calls：类别、耗时、重试次数、usage（reported / unknown / none）；
Trace 的 token 合计取自这些记录——usage 未知的调用按上限计入，预算记账不会少算。

模型调用失败 / 知识库需重建时不回退到模板或回显答案：结果带 error，由 API 层返回明确错误。
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.graph import build_agent_graph
from app.agent.router import IntentClassifier
from app.agent.state import AgentState, load_state_from_conversation, save_state_to_conversation
from app.llm.client import (
    ANSWER_MODE_ERROR,
    ANSWER_MODE_TEMPLATE,
    USAGE_UNKNOWN,
    LLMCallRecord,
    LLMClient,
    recording_llm_calls,
)
from app.llm.errors import LLMError, LLMErrorCategory
from app.models.base import utcnow
from app.models.conversation import Conversation, Message
from app.models.trace import AgentTrace
from app.rag.answerer import KbRebuildRequiredError, RagService
from app.services.orders import list_orders
from app.services.policy_rules import load_rules
from app.tools import ToolRegistry

logger = logging.getLogger(__name__)

RECENT_MESSAGE_WINDOW = 10
KB_REBUILD_USER_MESSAGE = "知识库正在维护，暂时无法回答产品与政策问题。订单、物流、工单查询不受影响，也可以转人工客服。"


class AgentService:
    def __init__(
        self,
        llm: LLMClient,
        rag: RagService,
        tools: ToolRegistry,
        classifier: IntentClassifier | None = None,
    ) -> None:
        self.llm = llm
        self.rag = rag
        self.tools = tools
        self._graph = build_agent_graph(llm, rag, tools, classifier)

    def handle(
        self,
        db: Session,
        user_id: str,
        session_id: str,
        message: str,
        *,
        decision: str | None = None,
        expected_pending_id: str | None = None,
    ) -> dict[str, Any]:
        """处理一轮对话。

        decision/expected_pending_id 仅由结构化确认入口传入（YES|NO + 卡片上的 pending_action.id）：
        仍走同一张图的 check_pending → execute_confirmed，幂等键同样取自 pending_action.id。
        """
        conv = self._get_or_create_conversation(db, user_id, session_id)
        trace_id = uuid4().hex[:20]
        db.add(Message(conversation_id=conv.id, role="user", content=message))
        db.commit()

        rules = load_rules()
        initial: AgentState = {
            "db": db,
            "user_id": user_id,
            "session_id": session_id,
            "user_query": message,
            "recent_messages": self._recent_messages(db, conv.id),
            "user_order_ids": [o.id for o in list_orders(db, user_id)],
            "steps_used": 0,
            "decision": decision,
            "expected_pending_id": expected_pending_id,
        }
        initial.update(load_state_from_conversation(conv))  # type: ignore[typeddict-item]

        started = utcnow()
        final_state: AgentState = dict(initial)  # type: ignore[assignment]
        error_type: str | None = None
        service_error: dict[str, str] | None = None
        with recording_llm_calls() as llm_calls:
            try:
                final_state = self._graph.invoke(
                    initial, config={"recursion_limit": 12}  # MAX_AGENT_STEPS + 少量节点裕量
                )
            except LLMError as exc:
                # 模型不可用：给出分类后的用户提示，绝不悄悄换成模板 / 回显答案
                error_type = f"LLM_{exc.category.value}"
                service_error = {
                    "type": "MODEL_NOT_CONFIGURED"
                    if exc.category is LLMErrorCategory.NOT_CONFIGURED
                    else "MODEL_CALL_FAILED",
                    "category": exc.category.value,
                    "message": exc.user_message,
                }
                final_state = {**initial, "final_answer": exc.user_message}
            except KbRebuildRequiredError as exc:
                error_type = exc.code
                logger.error("拒绝检索：%s", exc.message)
                service_error = {"type": exc.code, "category": exc.code, "message": KB_REBUILD_USER_MESSAGE}
                final_state = {**initial, "final_answer": KB_REBUILD_USER_MESSAGE}
            except Exception as exc:  # 图内异常兜底：不向用户暴露内部细节
                error_type = type(exc).__name__
                final_state = {**initial, "final_answer": "抱歉，系统内部出现异常，请稍后重试或转人工客服。"}
        if error_type is not None:
            final_state["answer_mode"] = ANSWER_MODE_ERROR
        # guardrail 等节点标记的 error_type（如 INJECTION_FLAGGED:*）也写入 trace
        error_type = error_type or final_state.get("error_type")
        latency_ms = int((utcnow() - started).total_seconds() * 1000)
        answer_mode = str(final_state.get("answer_mode") or ANSWER_MODE_TEMPLATE)
        prompt_tokens, completion_tokens = _billed_tokens(llm_calls, final_state)
        usage_unknown_calls = sum(1 for c in llm_calls if c.usage.status == USAGE_UNKNOWN)

        answer = str(final_state.get("final_answer", ""))
        sources = list(final_state.get("rag_sources", []))
        tool_calls = list(final_state.get("tool_calls", []))

        # 落库：状态 + 消息 + Trace
        save_state_to_conversation(
            db,
            conv,
            active_order_id=final_state.get("active_order_id"),
            active_ticket_id=final_state.get("active_ticket_id"),
            pending=final_state.get("pending_action"),
        )
        db.add(Message(conversation_id=conv.id, role="assistant", content=answer, trace_id=trace_id))
        db.add(
            AgentTrace(
                trace_id=trace_id,
                session_id=session_id,
                user_id=user_id,
                user_query=message,
                route=str(final_state.get("route", "")),
                intent=final_state.get("intent"),
                retrieval_query=message if final_state.get("route") in ("RAG", "AFTER_SALES") else None,
                retrieved_documents=sources or None,
                tool_name=", ".join(c["tool"] for c in tool_calls) or None,
                tool_arguments={c["tool"]: c.get("args", {}) for c in tool_calls} or None,
                tool_result=(final_state.get("tool_results") or [{}])[0] or None,
                llm_model=getattr(self.llm, "model_name", ""),
                prompt_tokens=prompt_tokens or None,
                completion_tokens=completion_tokens or None,
                total_tokens=(prompt_tokens + completion_tokens) or None,
                llm_calls=[c.to_trace() for c in llm_calls] or None,
                answer_mode=answer_mode,
                latency_ms=latency_ms,
                error_type=error_type,
                error_message=None if error_type is None else "详见服务日志（不向用户暴露）",
                final_answer=answer,
                policy_document_version=rules.policy_version,
                business_rule_version=rules.rules_version,
            )
        )
        db.commit()

        return {
            "answer": answer,
            "sources": sources,
            "tool_calls": tool_calls,
            "trace_id": trace_id,
            "route": str(final_state.get("route", "")),
            "intent": final_state.get("intent"),
            "abstained": bool(final_state.get("rag_abstained")),
            "error_type": final_state.get("error_type") or error_type,
            "error": service_error,
            "answer_mode": answer_mode,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "usage_unknown_calls": usage_unknown_calls,
            "llm_calls": [c.to_trace() for c in llm_calls],
            "latency_ms": latency_ms,
            "eligibility": final_state.get("eligibility"),
            "pending_action": final_state.get("pending_action"),
        }

    def _get_or_create_conversation(self, db: Session, user_id: str, session_id: str) -> Conversation:
        conv = db.scalar(select(Conversation).where(Conversation.session_id == session_id))
        if conv is None:
            conv = Conversation(user_id=user_id, session_id=session_id, title="客服会话")
            db.add(conv)
            db.commit()
            db.refresh(conv)
        elif conv.user_id != user_id:
            from app.services.errors import PermissionDeniedError

            raise PermissionDeniedError("该会话属于其他用户")
        return conv

    def _recent_messages(self, db: Session, conversation_id: int) -> list[dict[str, str]]:
        rows = db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc())
            .limit(RECENT_MESSAGE_WINDOW)
        ).all()
        return [{"role": m.role, "content": m.content} for m in reversed(rows)]


def _billed_tokens(calls: list[LLMCallRecord], state: AgentState) -> tuple[int, int]:
    """本轮计费 token：有调用记录时以记录为准（含失败调用与按上限计入的 unknown）；
    自定义的不记录调用的客户端（测试替身）退回节点累计值。"""
    if calls:
        return sum(c.usage.prompt_tokens for c in calls), sum(c.usage.completion_tokens for c in calls)
    return int(state.get("prompt_tokens", 0) or 0), int(state.get("completion_tokens", 0) or 0)
