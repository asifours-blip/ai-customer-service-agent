"""工单与人工回复（v1.1 补丁：TicketReply 最小闭环）。

幂等（外部审核修订①）：SIDE_EFFECT_TOOL create_ticket 禁止普通重试；
idempotency_key 由 pending_action_id 派生，DB 唯一约束 (user_id, idempotency_key) 兜底，
重复调用返回首次创建的工单而非再次 INSERT；request_fingerprint 不同则视为冲突。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow

TICKET_STATUSES = ("OPEN", "PROCESSING", "RESOLVED", "CLOSED")
TICKET_CATEGORIES = ("REFUND", "REPAIR", "EXCHANGE", "OTHER")
TICKET_PRIORITIES = ("LOW", "MEDIUM", "HIGH")


class Ticket(Base):
    __tablename__ = "tickets"
    # 幂等键只在同一用户内唯一：不同用户可以恰好生成相同 key，绝不能互相命中
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="tickets_user_id_idempotency_key_key"),
    )

    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # T10001
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="MEDIUM")
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 规范化业务字段的 sha256（算法见 services.tickets.ticket_request_fingerprint）
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class TicketReply(Base):
    __tablename__ = "ticket_replies"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id"), nullable=False, index=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    author_role: Mapped[str] = mapped_column(String(16), nullable=False)  # SUPPORT | CUSTOMER
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
