"""工单、人工回复、处理记录与客户反馈。

幂等（外部审核修订①）：SIDE_EFFECT_TOOL create_ticket 禁止普通重试；
idempotency_key 由 pending_action_id 派生，DB 唯一约束 (user_id, idempotency_key) 兜底，
重复调用返回首次创建的工单而非再次 INSERT；request_fingerprint 不同则视为冲突。

工作台（阶段 2）：
- assignee_id：领取人（SUPPORT）；只有领取人能推进状态与回复
- TicketEvent：只追加的处理记录（DB 触发器禁止 UPDATE/DELETE，见迁移 e3b8c1f4a2d6）
- TicketFeedback：每张工单至多一条客户评分，ticket_id 唯一约束兜底并发重复提交
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, SmallInteger, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow

TICKET_STATUSES = ("OPEN", "PROCESSING", "RESOLVED", "CLOSED")
TICKET_CATEGORIES = ("REFUND", "REPAIR", "EXCHANGE", "OTHER")
TICKET_PRIORITIES = ("LOW", "MEDIUM", "HIGH")

# 处理记录事件类型
EVENT_CREATED = "CREATED"
EVENT_CLAIMED = "CLAIMED"
EVENT_STATUS_CHANGED = "STATUS_CHANGED"
EVENT_REPLIED = "REPLIED"
EVENT_FEEDBACK_SUBMITTED = "FEEDBACK_SUBMITTED"
TICKET_EVENT_TYPES = (EVENT_CREATED, EVENT_CLAIMED, EVENT_STATUS_CHANGED, EVENT_REPLIED, EVENT_FEEDBACK_SUBMITTED)


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
    # 领取人（SUPPORT）；NULL = 未指派。属内部字段，不出现在客户侧响应中
    assignee_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
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


class TicketEvent(Base):
    """工单处理记录（只追加）。回复内容不在此冗余，按 reply_id 关联 ticket_replies。"""

    __tablename__ = "ticket_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(16), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reply_id: Mapped[int | None] = mapped_column(ForeignKey("ticket_replies.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class TicketFeedback(Base):
    __tablename__ = "ticket_feedback"
    __table_args__ = (
        # 每张工单至多一条反馈；服务层按约束名识别并发重复提交
        UniqueConstraint("ticket_id", name="ticket_feedback_ticket_id_key"),
        CheckConstraint("rating BETWEEN 1 AND 5", name="ticket_feedback_rating_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
