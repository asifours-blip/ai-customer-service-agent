"""AgentTrace：每次 Agent 请求的全链路追踪记录（规格 §5 重点实体 / §30）。

Phase 1 先建表；Phase 4~6 逐步填充字段。
policy_version 双记录：RAG 政策文档版本 + 确定性业务规则版本（v1.1 补丁）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow


class AgentTrace(Base):
    __tablename__ = "agent_traces"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)

    user_query: Mapped[str] = mapped_column(Text, nullable=False, default="")
    route: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    intent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    retrieval_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    retrieved_documents: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(48), nullable=True)
    tool_arguments: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    tool_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    tool_retries: Mapped[int | None] = mapped_column(nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_document_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    business_rule_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
