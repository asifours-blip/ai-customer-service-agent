"""Trace 复盘 API：按 trace_id 查询全链路记录（本人或 SUPPORT）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.kb.service import resolve_citations
from app.models.trace import AgentTrace
from app.models.user import User
from app.schemas.api import CitationOut
from app.security.dependencies import get_current_user
from app.services.database import get_db
from app.services.errors import NotFoundError, PermissionDeniedError

router = APIRouter()


class TraceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    trace_id: str
    session_id: str
    user_id: str
    user_query: str
    route: str
    intent: str | None
    retrieval_query: str | None
    retrieved_documents: list[dict[str, Any]] | None
    tool_name: str | None
    tool_arguments: dict[str, Any] | None
    tool_result: dict[str, Any] | None
    llm_model: str | None
    llm_calls: list[dict[str, Any]] | None
    answer_mode: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    latency_ms: int | None
    error_type: str | None
    error_message: str | None
    final_answer: str | None
    policy_document_version: str | None
    business_rule_version: str | None
    created_at: datetime


def _visible_trace(db: Session, trace_id: str, user: User) -> AgentTrace:
    trace = db.query(AgentTrace).filter_by(trace_id=trace_id).one_or_none()
    if trace is None:
        raise NotFoundError(f"trace 不存在: {trace_id}")
    if trace.user_id != user.id and user.role != "SUPPORT":
        raise PermissionDeniedError("无权查看该 trace")
    return trace


@router.get("/{trace_id}", response_model=TraceOut)
def get_trace(
    trace_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TraceOut:
    return TraceOut.model_validate(_visible_trace(db, trace_id, user))


@router.get("/{trace_id}/citations", response_model=list[CitationOut])
def get_trace_citations(
    trace_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[CitationOut]:
    """回答引用的原文：按 (version_id, chunk_id) 取回；该版本被替换（RETIRED）后仍可查。"""
    trace = _visible_trace(db, trace_id, user)
    return [CitationOut(**c) for c in resolve_citations(db, list(trace.retrieved_documents or []))]
