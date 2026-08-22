"""会话与消息。

外部审核修订④：AgentSessionState 必须持久化到 PostgreSQL（不能只放进程内存）——
Conversation 表同时承载会话元数据与 Agent 状态字段；
pending_action 带 id 与过期时间，服务重启后可恢复。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False, default="新会话")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    # --- 持久化 AgentSessionState（规格 §3）---
    active_order_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    active_ticket_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    active_product_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_intent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pending_action_type: Mapped[str | None] = mapped_column(String(48), nullable=True)
    pending_action_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    pending_action_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    pending_action_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant | system
    content: Mapped[str] = mapped_column(Text, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
