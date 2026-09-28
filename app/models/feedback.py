"""回答反馈与审核（有帮助 / 没帮助 → 管理员审核 → 转评测用例 / 驳回）。

与 TicketFeedback（工单解决后的 1-5 星评分，每张工单一条）是两回事：这里针对对话里
每一条 assistant 回答，供答案质量复盘与评测集扩充。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow

FEEDBACK_STATUS_PENDING = "PENDING"
FEEDBACK_STATUS_CONVERTED = "CONVERTED"
FEEDBACK_STATUS_REJECTED = "REJECTED"
FEEDBACK_STATUSES = (FEEDBACK_STATUS_PENDING, FEEDBACK_STATUS_CONVERTED, FEEDBACK_STATUS_REJECTED)

REVIEW_ACTION_CONVERT = "CONVERT"
REVIEW_ACTION_REJECT = "REJECT"


class AnswerFeedback(Base):
    """客户对某条 assistant 回答的反馈：同一用户对同一条回答只能提交一次（不支持修改）。"""

    __tablename__ = "answer_feedback"
    __table_args__ = (
        # 唯一约束兜底：服务层的重复提交检查之外，数据库层绝对禁止同一用户对同一条回答提交两次
        UniqueConstraint("message_id", "user_id", name="uq_answer_feedback_message_user"),
        CheckConstraint(
            "review_status IN ('PENDING', 'CONVERTED', 'REJECTED')",
            name="answer_feedback_review_status_check",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    helpful: Mapped[bool] = mapped_column(nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_status: Mapped[str] = mapped_column(String(16), nullable=False, default=FEEDBACK_STATUS_PENDING)
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class FeedbackReviewAudit(Base):
    """反馈审核操作审计：转评测用例 / 驳回。只追加，DB 触发器拒绝 UPDATE / DELETE（与 kb_audit_log 同做法）。"""

    __tablename__ = "feedback_review_audit"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    feedback_id: Mapped[int] = mapped_column(ForeignKey("answer_feedback.id"), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # CONVERT | REJECT
    actor: Mapped[str] = mapped_column(String(64), nullable=False)  # 用户 id
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
