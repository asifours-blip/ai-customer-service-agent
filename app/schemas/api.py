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




class DamageCaseApprove(BaseModel):
    damage_kind: Literal["OUTER_PACKAGE", "PRODUCT", "BOTH"]
    reviewed_path: Literal["REQUEST_EVIDENCE", "CARRIER_INVESTIGATION", "REPLACEMENT_REVIEW", "REFUND_REVIEW"]
    confirmed_logistics_damage: Literal[True]


class DamageCaseOut(BaseModel):
    source_ticket_id: str
    damage_kind: str
    reviewed_path: str
    reviewed_path_label: str
    product_id: str
    order_status: str
    logistics_status: str | None
    reviewed_policy_version: str
    approved_by: str
    approved_at: datetime
    withdrawn_by: str | None
    withdrawn_at: datetime | None


class SimilarDamageCaseOut(BaseModel):
    source_ticket_id: str
    reviewed_path: str
    reviewed_path_label: str
    similarities: list[str]
    differences: list[str]
    score: int
    reviewed_policy_version: str
    stale_policy: bool


class DamageCaseDraft(BaseModel):
    status: Literal["NO_CASES", "HISTORICAL_ONLY", "CASE_ASSISTED"]
    current_facts: list[str]
    missing_information: list[str]
    cited_cases: list[SimilarDamageCaseOut]
    next_step: str
    limitation: str


class DamageCaseSuggestionOut(BaseModel):
    ticket_id: str
    damage_kind: str
    current_order_status: str
    current_logistics_status: str | None
    current_policy_version: str
    cases: list[SimilarDamageCaseOut]
    draft: DamageCaseDraft


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
    # 助手消息的回答方式（取自同 trace）：刷新后仍能区分模型生成 / 离线回显 / 模板 / 出错提示
    answer_mode: str | None = None


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
    # 回答方式：MODEL（模型生成）/ OFFLINE_ECHO（离线回显检索原文，未调用模型）/ TEMPLATE（确定性模板）
    answer_mode: str = "TEMPLATE"
    # 售后资格判定依据（EligibilityService 确定性结果）与待确认操作
    eligibility: dict[str, Any] | None = None
    pending_action: PendingActionOut | None = None


# --- 知识库版本（D-021）---
class KbVersionOut(ORMModel):
    id: int
    status: str
    source: str
    source_hash: str
    note: str | None
    created_by: str
    embedding_backend: str | None
    doc_count: int
    chunk_count: int | None
    progress_done: int
    progress_total: int
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    activated_at: datetime | None


class KbVersionListOut(BaseModel):
    active_version_id: int | None
    versions: list[KbVersionOut]


class KbDocumentOut(ORMModel):
    path: str
    document_id: str
    document_name: str
    content_hash: str
    size_bytes: int


class KbAuditOut(ORMModel):
    id: int
    version_id: int
    action: str
    actor: str
    from_status: str | None
    to_status: str | None
    detail: dict[str, Any] | None
    created_at: datetime


class KbVersionDetailOut(KbVersionOut):
    checks: dict[str, Any] | None = None
    documents: list[KbDocumentOut] = []
    audit: list[KbAuditOut] = []


class KbPublishRequest(BaseModel):
    """比较交换：调用方必须给出它看到的当前生效版本（没有则 null），与实际不一致返回 409。"""

    expected_active_version_id: int | None


class CitationOut(BaseModel):
    document: str | None
    section: str | None
    chunk_id: str | None
    version_id: int | None
    found: bool
    version_status: str | None = None
    content: str | None = None
    reason: str | None = None


# --- 回答反馈与审核 ---


class AnswerFeedbackCreate(BaseModel):
    helpful: bool
    note: str = Field(default="", max_length=2000)


class AnswerFeedbackOut(ORMModel):
    id: int
    message_id: int
    user_id: str
    helpful: bool
    note: str | None
    review_status: str
    reviewed_by: str | None
    reviewed_at: datetime | None
    created_at: datetime


class FeedbackQueueItemOut(BaseModel):
    """管理员审核队列条目：问题、回答、引用、知识库版本、trace 一次性给全。"""

    feedback: AnswerFeedbackOut
    question: str | None
    answer: str | None
    trace_id: str | None
    route: str | None
    citations: list[CitationOut] = []
    kb_version_ids: list[int] = []


class FeedbackConvertRequest(BaseModel):
    """驳回不需要额外字段；转评测用例必须给期望结果与期望引用，字段对齐 eval/loader.py。"""

    expected_outcome: str = Field(pattern="^(SUCCESS|REFUSED|BLOCKED|CLARIFY|NOT_FOUND|DUPLICATE)$")
    expected_document: str | None = Field(default=None, max_length=128)
    category: str = Field(default="rag", max_length=32)
    note: str = Field(default="", max_length=500)


class FeedbackReviewAuditOut(BaseModel):
    id: int
    feedback_id: int
    action: str
    actor: str
    detail: dict[str, Any] | None
    created_at: datetime
