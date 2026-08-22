"""API 请求/响应模型（Pydantic v2）。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

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


class TicketReplyOut(ORMModel):
    id: int
    ticket_id: str
    author_id: str
    author_role: str
    content: str
    created_at: datetime


class TicketDetailOut(TicketOut):
    replies: list[TicketReplyOut] = []


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


class ConversationDetailOut(ConversationOut):
    messages: list[MessageOut] = []


# --- Chat（Phase 4 实装，Phase 1 占位）---
class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=4000)


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
