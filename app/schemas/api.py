"""API 请求/响应模型（Pydantic v2）。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.ticket import TICKET_CATEGORIES, TICKET_PRIORITIES


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Auth ---
class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    role: str


# --- Orders ---
class OrderOut(ORMModel):
    id: str
    user_id: str
    product_id: str
    status: str
    amount: Decimal
    created_at: datetime
    paid_at: datetime | None = None
    shipped_at: datetime | None = None
    delivered_at: datetime | None = None


class LogisticsOut(ORMModel):
    order_id: str
    carrier: str
    tracking_number: str
    status: str
    current_location: str
    updated_at: datetime


class OrderDetailOut(OrderOut):
    logistics: LogisticsOut | None = None


# --- Tickets ---
class TicketCreateRequest(BaseModel):
    order_id: str | None = Field(default=None, min_length=1, max_length=16)
    category: str = Field(pattern=f"^({'|'.join(TICKET_CATEGORIES)})$")
    title: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=4000)
    priority: str = Field(default="MEDIUM", pattern=f"^({'|'.join(TICKET_PRIORITIES)})$")
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=64)


class TicketOut(ORMModel):
    id: str
    user_id: str
    order_id: str | None
    category: str
    title: str
    description: str
    status: str
    priority: str
    created_at: datetime
    updated_at: datetime


class SupportTicketOut(TicketOut):
    """客服视图：在客户视图基础上带内部字段 assignee_id。"""

    assignee_id: str | None = None


class TicketReplyOut(ORMModel):
    id: int
    ticket_id: str
    # 客户视图中客服回复的 author_id 置空（客服账号属内部信息），只保留 author_role
    author_id: str | None
    author_role: str
    content: str
    created_at: datetime


class TicketEventOut(ORMModel):
    id: int
    event_type: str
    # 客户视图中客服操作者的 actor_id 置空，只保留 actor_role
    actor_id: str | None
    actor_role: str
    from_status: str | None = None
    to_status: str | None = None
    reply_id: int | None = None
    created_at: datetime


class TicketFeedbackCreate(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=2000)


class TicketFeedbackOut(ORMModel):
    ticket_id: str
    rating: int
    comment: str
    created_at: datetime


class SupportFeedbackOut(TicketFeedbackOut):
    """客服侧反馈列表：附带工单上下文，供复盘与评测集导出。"""

    user_id: str
    ticket_category: str
    ticket_title: str
    ticket_status: str
    order_id: str | None


class TicketDetailOut(TicketOut):
    replies: list[TicketReplyOut] = []
    events: list[TicketEventOut] = []
    feedback: TicketFeedbackOut | None = None


class SupportTicketDetailOut(SupportTicketOut):
    replies: list[TicketReplyOut] = []
    events: list[TicketEventOut] = []
    feedback: TicketFeedbackOut | None = None


class TicketStatusPatch(BaseModel):
    status: str = Field(pattern="^(OPEN|PROCESSING|RESOLVED|CLOSED)$")


class TicketReplyCreate(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


# --- Conversations ---
class ConversationOut(ORMModel):
    id: int
    session_id: str
    title: str
    created_at: datetime
    updated_at: datetime


class MessageOut(ORMModel):
    id: int
    role: str
    content: str
    trace_id: str | None
    created_at: datetime
    # 助手消息的引用来源（取自同 trace 的 retrieved_documents），刷新后历史仍可展示依据
    sources: list[dict[str, str]] = []


class PendingActionOut(BaseModel):
    """待确认操作（确认卡片数据）。id 即确认时使用的幂等键来源。"""

    id: str
    type: str
    expires_at: str
    order_id: str | None = None
    category: str | None = None
    title: str | None = None


class ConversationDetailOut(ConversationOut):
    messages: list[MessageOut] = []
    pending_action: PendingActionOut | None = None


# --- Chat（Phase 4 实装，Phase 1 占位）---
class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=4000)


class ChatConfirmRequest(BaseModel):
    """结构化确认入口：必须指明确认的是哪一个 pending_action，防止误确认已被替换的操作。"""

    session_id: str = Field(min_length=1, max_length=64)
    pending_action_id: str = Field(min_length=1, max_length=64)
    decision: Literal["CONFIRM", "CANCEL"]


class ChatResponse(BaseModel):
    answer: str
    sources: list[dict[str, str]] = []
    tool_calls: list[dict[str, object]] = []
    trace_id: str
    # Trace 面板数据（Phase 6）
    route: str = ""
    intent: str | None = None
    abstained: bool = False
    latency_ms: int = 0
    # 售后资格判定依据（EligibilityService 确定性结果）与待确认操作
    eligibility: dict[str, Any] | None = None
    pending_action: PendingActionOut | None = None
