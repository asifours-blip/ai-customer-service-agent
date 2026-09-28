"""SUPPORT 人工客服 API（v1.1 补丁 §1 最小闭环 + 阶段 2 工作台，非 Agent Tool）。

- GET    /api/support/tickets                工单队列（status 过滤；scope=all|unassigned|mine）
- GET    /api/support/tickets/{id}           工单详情（含 assignee、完整时间线、反馈）
- POST   /api/support/tickets/{id}/claim     领取（行锁；他人已领 → 409 ALREADY_ASSIGNED）
- PATCH  /api/support/tickets/{id}/status    状态迁移（仅领取人，单向链）
- POST   /api/support/tickets/{id}/replies   人工回复（仅领取人，CLOSED 后不可回复）
- GET    /api/support/feedback               客户反馈列表（供复盘与评测集导出）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.ticket_views import support_detail
from app.models.user import User
from app.schemas.api import (
    SupportFeedbackOut,
    SupportTicketDetailOut,
    SupportTicketOut,
    TicketReplyCreate,
    TicketReplyOut,
    TicketStatusPatch,
)
from app.security.dependencies import get_support_user
from app.services import tickets as ticket_service
from app.services.database import get_db

router = APIRouter()


@router.get("/tickets", response_model=list[SupportTicketOut])
def list_tickets(
    status: str | None = Query(default=None, pattern="^(OPEN|PROCESSING|RESOLVED|CLOSED)$"),
    scope: str = Query(default="all", pattern="^(all|unassigned|mine)$"),
    support: User = Depends(get_support_user),
    db: Session = Depends(get_db),
) -> list[SupportTicketOut]:
    rows = ticket_service.list_tickets_for_support(db, status, scope=scope, support_user_id=support.id)
    return [SupportTicketOut.model_validate(t) for t in rows]


@router.get("/tickets/{ticket_id}", response_model=SupportTicketDetailOut)
def ticket_detail(
    ticket_id: str,
    support: User = Depends(get_support_user),
    db: Session = Depends(get_db),
) -> SupportTicketDetailOut:
    return support_detail(db, ticket_service.get_ticket(db, ticket_id, support.id, is_support=True))


@router.post("/tickets/{ticket_id}/claim", response_model=SupportTicketOut)
def claim(
    ticket_id: str,
    support: User = Depends(get_support_user),
    db: Session = Depends(get_db),
) -> SupportTicketOut:
    return SupportTicketOut.model_validate(ticket_service.claim_ticket(db, ticket_id, support.id))


@router.patch("/tickets/{ticket_id}/status", response_model=SupportTicketOut)
def update_status(
    ticket_id: str,
    body: TicketStatusPatch,
    support: User = Depends(get_support_user),
    db: Session = Depends(get_db),
) -> SupportTicketOut:
    return SupportTicketOut.model_validate(ticket_service.transition_ticket(db, ticket_id, body.status, support.id))


@router.post("/tickets/{ticket_id}/replies", response_model=TicketReplyOut, status_code=201)
def add_reply(
    ticket_id: str,
    body: TicketReplyCreate,
    support: User = Depends(get_support_user),
    db: Session = Depends(get_db),
) -> TicketReplyOut:
    return TicketReplyOut.model_validate(ticket_service.add_support_reply(db, ticket_id, support.id, body.content))


@router.get("/feedback", response_model=list[SupportFeedbackOut])
def list_feedback(
    support: User = Depends(get_support_user),
    db: Session = Depends(get_db),
) -> list[SupportFeedbackOut]:
    return [
        SupportFeedbackOut(
            ticket_id=fb.ticket_id,
            rating=fb.rating,
            comment=fb.comment,
            created_at=fb.created_at,
            user_id=fb.user_id,
            ticket_category=t.category,
            ticket_title=t.title,
            ticket_status=t.status,
            order_id=t.order_id,
        )
        for fb, t in ticket_service.list_feedback(db)
    ]
