"""会话查询 API（只读）：列表 / 详情（含历史消息引用与当前待确认操作）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.state import load_state_from_conversation, public_pending
from app.models.conversation import Conversation, Message
from app.models.trace import AgentTrace
from app.models.user import User
from app.schemas.api import ConversationDetailOut, ConversationOut, MessageOut, PendingActionOut
from app.security.dependencies import get_current_user
from app.services.database import get_db
from app.services.errors import NotFoundError
from app.services.permission import ensure_owner

router = APIRouter()


@router.get("", response_model=list[ConversationOut])
def list_my_conversations(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[ConversationOut]:
    rows = db.scalars(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
    )
    return [ConversationOut.model_validate(c) for c in rows]


@router.get("/{conversation_id}", response_model=ConversationDetailOut)
def get_my_conversation(
    conversation_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ConversationDetailOut:
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise NotFoundError(f"会话不存在: {conversation_id}")
    ensure_owner(conv.user_id, user.id)
    messages = list(
        db.scalars(
            select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at, Message.id)
        )
    )
    # 引用来源来自同一 trace 的检索记录（trace 与会话同属本人，已随会话鉴权）
    trace_ids = [m.trace_id for m in messages if m.trace_id]
    sources_by_trace: dict[str, list[dict[str, str]]] = {}
    mode_by_trace: dict[str, str | None] = {}
    if trace_ids:
        for trace in db.scalars(select(AgentTrace).where(AgentTrace.trace_id.in_(trace_ids))):
            docs = trace.retrieved_documents or []
            sources_by_trace[trace.trace_id] = [{k: str(v) for k, v in d.items()} for d in docs]
            mode_by_trace[trace.trace_id] = trace.answer_mode
    out = ConversationDetailOut.model_validate(conv)
    out.messages = [
        MessageOut.model_validate(m).model_copy(
            update={
                "sources": sources_by_trace.get(m.trace_id or "", []),
                "answer_mode": mode_by_trace.get(m.trace_id or ""),
            }
        )
        for m in messages
    ]
    pending = public_pending(load_state_from_conversation(conv)["pending_action"])
    out.pending_action = PendingActionOut(**pending) if pending else None
    return out
