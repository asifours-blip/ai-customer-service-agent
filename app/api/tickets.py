"""客户工单 API（仅本人）：列表 / 创建 / 详情。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.models.user import User
from app.schemas.api import TicketCreateRequest, TicketDetailOut, TicketOut, TicketReplyOut
from app.security.dependencies import get_current_user
from app.services import tickets as ticket_service
from app.services.database import get_db

router = APIRouter()


@router.get("", response_model=list[TicketOut])
def list_my_tickets(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[TicketOut]:
    return [TicketOut.model_validate(t) for t in ticket_service.list_tickets(db, user.id)]


@router.post("", response_model=TicketOut, status_code=status.HTTP_201_CREATED)
def create_ticket(
    body: TicketCreateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TicketOut:
    ticket, _created = ticket_service.create_ticket(
        db,
        user_id=user.id,
        category=body.category,
        title=body.title,
        description=body.description,
        priority=body.priority,
        order_id=body.order_id,
        idempotency_key=body.idempotency_key,
    )
    return TicketOut.model_validate(ticket)


@router.get("/{ticket_id}", response_model=TicketDetailOut)
def get_my_ticket(
    ticket_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TicketDetailOut:
    ticket = ticket_service.get_ticket(db, ticket_id, user.id)
    out = TicketDetailOut.model_validate(ticket)
    out.replies = [TicketReplyOut.model_validate(r) for r in ticket_service.list_replies(db, ticket.id)]
    return out
