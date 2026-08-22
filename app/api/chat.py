"""POST /api/chat：受控 Agent 实装（Phase 4）。

离线（NO_PAID_API=true，默认）：FakeLLM + FakeEmbedding 全链路可跑；
live：DEEPSEEK_API_KEY + NO_PAID_API=false 时切 DeepSeek。
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.agent.service import AgentService
from app.config import get_settings
from app.llm.client import FakeLLMClient, LLMClient
from app.models.user import User
from app.rag.answerer import RagService
from app.rag.embedding import FakeEmbedding, get_embedding_client
from app.schemas.api import ChatRequest, ChatResponse
from app.security.dependencies import get_current_user
from app.services.database import get_db
from app.tools import build_registry

router = APIRouter()


@lru_cache
def _agent_service() -> AgentService:
    settings = get_settings()
    if settings.no_paid_api:
        llm: LLMClient = FakeLLMClient()
        from app.rag.embedding import EmbeddingClient

        embedder: EmbeddingClient = FakeEmbedding()
        threshold = settings.retrieval_score_threshold_fake
    else:
        from app.llm.deepseek import DeepseekClient

        llm = DeepseekClient()
        embedder = get_embedding_client(settings.embedding_backend, settings.bge_model_name)
        threshold = (
            settings.retrieval_score_threshold_bge
            if settings.embedding_backend == "bge"
            else settings.retrieval_score_threshold_fake
        )
    rag = RagService(embedder, llm, score_threshold=threshold)
    return AgentService(llm, rag, build_registry())


@router.post("", response_model=ChatResponse)
def chat(
    body: ChatRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ChatResponse:
    result = _agent_service().handle(db, user.id, body.session_id, body.message)
    return ChatResponse(
        answer=result["answer"],
        sources=[dict(s) for s in result["sources"]],
        tool_calls=result["tool_calls"],
        trace_id=result["trace_id"],
    )
