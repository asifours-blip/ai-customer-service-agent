"""POST /api/chat：受控 Agent 实装（Phase 4）；POST /api/chat/confirm：确认卡片的结构化入口。

离线（NO_PAID_API=true，默认）：FakeLLM + FakeEmbedding 全链路可跑；
live：DEEPSEEK_API_KEY + NO_PAID_API=false 时切 DeepSeek。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.service import AgentService
from app.agent.state import public_pending
from app.config import get_settings
from app.llm.client import FakeLLMClient, LLMClient
from app.models.conversation import Conversation
from app.models.user import User
from app.rag.answerer import RagService
from app.rag.embedding import serving_retrieval
from app.schemas.api import ChatConfirmRequest, ChatRequest, ChatResponse, PendingActionOut
from app.security.dependencies import get_current_user
from app.services.database import get_db
from app.services.errors import NotFoundError
from app.services.permission import ensure_owner
from app.tools import build_registry

router = APIRouter()


@lru_cache
def _agent_service() -> AgentService:
    settings = get_settings()
    if settings.no_paid_api:
        llm: LLMClient = FakeLLMClient()
    else:
        from app.llm.deepseek import DeepseekClient

        llm = DeepseekClient()
    # 检索 embedder 与知识库后台导入共用同一来源（serving_retrieval），保证版本向量与查询向量同域
    embedder, threshold = serving_retrieval()
    rag = RagService(embedder, llm, score_threshold=threshold)
    return AgentService(llm, rag, build_registry())


def _to_response(result: dict[str, Any]) -> ChatResponse:
    pending = public_pending(result.get("pending_action"))
    return ChatResponse(
        answer=result["answer"],
        sources=[dict(s) for s in result["sources"]],
        tool_calls=result["tool_calls"],
        trace_id=result["trace_id"],
        route=str(result.get("route", "")),
        intent=result.get("intent"),
        abstained=bool(result.get("abstained")),
        latency_ms=int(result.get("latency_ms", 0)),
        eligibility=result.get("eligibility"),
        pending_action=PendingActionOut(**pending) if pending else None,
    )


@router.post("", response_model=ChatResponse)
def chat(
    body: ChatRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ChatResponse:
    return _to_response(_agent_service().handle(db, user.id, body.session_id, body.message))


# 结构化决策 → (图内 decision, 会话历史中记录的文本，与聊天里手输的确认词一致)
_DECISION_TEXT = {"CONFIRM": ("YES", "确认"), "CANCEL": ("NO", "取消")}


@router.post("/confirm", response_model=ChatResponse)
def confirm_pending_action(
    body: ChatConfirmRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ChatResponse:
    """确认卡片的结构化入口：不另写执行路径，而是带着 decision 进入同一张图。

    图内 check_pending 校验 pending_action_id 仍是会话当前的待确认操作且未过期，
    随后与聊天发「确认」完全相同地进入 execute_confirmed（REVALIDATE + 以 pending_action.id 为幂等键建单）。
    """
    conv = db.scalar(select(Conversation).where(Conversation.session_id == body.session_id))
    if conv is None:
        raise NotFoundError(f"会话不存在: {body.session_id}")
    ensure_owner(conv.user_id, user.id)
    decision, text = _DECISION_TEXT[body.decision]
    result = _agent_service().handle(
        db, user.id, body.session_id, text, decision=decision, expected_pending_id=body.pending_action_id
    )
    return _to_response(result)
