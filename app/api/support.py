"""SUPPORT 人工客服 API（v1.1 补丁 §1：最小闭环，非 Agent Tool）。

- GET    /api/support/tickets                所有待处理工单（可按状态过滤）
- GET    /api/support/tickets/{id}           工单详情
- PATCH  /api/support/tickets/{id}/status    状态迁移（仅 SUPPORT，单向链）
- POST   /api/support/tickets/{id}/replies   人工回复
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.models.user import User
from app.schemas.api import TicketDetailOut, TicketOut, TicketReplyCreate, TicketReplyOut, TicketStatusPatch
from app.security.dependencies import get_support_user
from app.services import tickets as ticket_service
from app.services.database import get_db

router = APIRouter()


@router.get("/tickets", response_model=list[TicketOut])
def list_tickets(
    status: str | None = Query(default=None, pattern="^(OPEN|PROCESSING|RESOLVED|CLOSED)$"),
    support: User = Depends(get_support_user),
    db: Session = Depends(get_db),
) -> list[TicketOut]:
    return [TicketOut.model_validate(t) for t in ticket_service.list_tickets_for_support(db, status)]


@router.get("/tickets/{ticket_id}", response_model=TicketDetailOut)
def ticket_detail(
    ticket_id: str,
    support: User = Depends(get_support_user),
    db: Session = Depends(get_db),
) -> TicketDetailOut:
    ticket = ticket_service.get_ticket(db, ticket_id, support.id, is_support=True)
    out = TicketDetailOut.model_validate(ticket)
    out.replies = [TicketReplyOut.model_validate(r) for r in ticket_service.list_replies(db, ticket.id)]
    return out


@router.patch("/tickets/{ticket_id}/status", response_model=TicketOut)
def update_status(
    ticket_id: str,
    body: TicketStatusPatch,
    support: User = Depends(get_support_user),
    db: Session = Depends(get_db),
) -> TicketOut:
    return TicketOut.model_validate(ticket_service.transition_ticket(db, ticket_id, body.status, support.id))


@router.post("/tickets/{ticket_id}/replies", response_model=TicketReplyOut, status_code=201)
def add_reply(
    ticket_id: str,
    body: TicketReplyCreate,
    support: User = Depends(get_support_user),
    db: Session = Depends(get_db),
) -> TicketReplyOut:
    reply = ticket_service.add_reply(db, ticket_id, support.id, support.role, body.content)
    return TicketReplyOut.model_validate(reply)
