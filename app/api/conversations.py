"""会话查询 API（Phase 4 实装写入，这里先提供只读）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.conversation import Conversation, Message
from app.models.user import User
from app.schemas.api import ConversationDetailOut, ConversationOut, MessageOut
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
    messages = db.scalars(
        select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at, Message.id)
    )
    out = ConversationDetailOut.model_validate(conv)
    out.messages = [MessageOut.model_validate(m) for m in messages]
    return out
