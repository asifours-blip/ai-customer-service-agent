"""Agent 编排服务：会话装载 → 图执行 → 状态/消息/Trace 落库。

Trace 原则（规格 §30）：每个请求一个 trace_id，失败 case 可完整复盘。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.graph import build_agent_graph
from app.agent.router import IntentClassifier
from app.agent.state import AgentState, load_state_from_conversation, save_state_to_conversation
from app.llm.client import LLMClient
from app.models.base import utcnow
from app.models.conversation import Conversation, Message
from app.models.trace import AgentTrace
from app.rag.answerer import RagService
from app.services.orders import list_orders
from app.services.policy_rules import load_rules
from app.tools import ToolRegistry

RECENT_MESSAGE_WINDOW = 10


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

    def handle(self, db: Session, user_id: str, session_id: str, message: str) -> dict[str, Any]:
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
        }
        initial.update(load_state_from_conversation(conv))  # type: ignore[typeddict-item]

        started = utcnow()
        final_state: AgentState = dict(initial)  # type: ignore[assignment]
        error_type: str | None = None
        try:
            final_state = self._graph.invoke(
                initial, config={"recursion_limit": 12}  # MAX_AGENT_STEPS + 少量节点裕量
            )
        except Exception as exc:  # 图内异常兜底：不向用户暴露内部细节
            error_type = type(exc).__name__
            final_state = {**initial, "final_answer": "抱歉，系统内部出现异常，请稍后重试或转人工客服。"}
        # guardrail 等节点标记的 error_type（如 INJECTION_FLAGGED:*）也写入 trace
        error_type = error_type or final_state.get("error_type")
        latency_ms = int((utcnow() - started).total_seconds() * 1000)

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
                prompt_tokens=final_state.get("prompt_tokens"),
                completion_tokens=final_state.get("completion_tokens"),
                total_tokens=(final_state.get("prompt_tokens", 0) or 0)
                + (final_state.get("completion_tokens", 0) or 0)
                or None,
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
            "prompt_tokens": final_state.get("prompt_tokens", 0) or 0,
            "completion_tokens": final_state.get("completion_tokens", 0) or 0,
            "latency_ms": latency_ms,
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
