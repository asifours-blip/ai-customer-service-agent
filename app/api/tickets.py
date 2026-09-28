"""客户工单 API（仅 CUSTOMER 本人）：列表 / 创建 / 详情（含时间线）/ 回复 / 反馈。

- GET    /api/tickets                    我的工单
- POST   /api/tickets                    创建（幂等键可选）
- GET    /api/tickets/{id}               详情：回复、处理记录、反馈（隐藏客服内部字段）
- POST   /api/tickets/{id}/replies       客户回复（CLOSED 后不可回复）
- GET    /api/tickets/{id}/feedback      查询本工单反馈
- POST   /api/tickets/{id}/feedback      RESOLVED/CLOSED 后提交一次评分，重复 → 409
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.ticket_views import customer_detail
from app.models.user import User
from app.schemas.api import (
    TicketCreateRequest,
    TicketDetailOut,
    TicketFeedbackCreate,
    TicketFeedbackOut,
    TicketOut,
    TicketReplyCreate,
    TicketReplyOut,
)
from app.security.dependencies import get_customer_user
from app.services import tickets as ticket_service
from app.services.database import get_db
from app.services.errors import NotFoundError

router = APIRouter()


@router.get("", response_model=list[TicketOut])
def list_my_tickets(
    user: User = Depends(get_customer_user), db: Session = Depends(get_db)
) -> list[TicketOut]:
    return [TicketOut.model_validate(t) for t in ticket_service.list_tickets(db, user.id)]


@router.post("", response_model=TicketOut, status_code=status.HTTP_201_CREATED)
def create_ticket(
    body: TicketCreateRequest,
    user: User = Depends(get_customer_user),
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
    user: User = Depends(get_customer_user),
    db: Session = Depends(get_db),
) -> TicketDetailOut:
    return customer_detail(db, ticket_service.get_ticket(db, ticket_id, user.id))


@router.post("/{ticket_id}/replies", response_model=TicketReplyOut, status_code=status.HTTP_201_CREATED)
def reply_my_ticket(
    ticket_id: str,
    body: TicketReplyCreate,
    user: User = Depends(get_customer_user),
    db: Session = Depends(get_db),
) -> TicketReplyOut:
    return TicketReplyOut.model_validate(ticket_service.add_customer_reply(db, ticket_id, user.id, body.content))


@router.get("/{ticket_id}/feedback", response_model=TicketFeedbackOut)
def get_my_feedback(
    ticket_id: str,
    user: User = Depends(get_customer_user),
    db: Session = Depends(get_db),
) -> TicketFeedbackOut:
    ticket = ticket_service.get_ticket(db, ticket_id, user.id)
    feedback = ticket_service.get_feedback(db, ticket.id)
    if feedback is None:
        raise NotFoundError(f"工单 {ticket.id} 暂无反馈")
    return TicketFeedbackOut.model_validate(feedback)


@router.post("/{ticket_id}/feedback", response_model=TicketFeedbackOut, status_code=status.HTTP_201_CREATED)
def submit_my_feedback(
    ticket_id: str,
    body: TicketFeedbackCreate,
    user: User = Depends(get_customer_user),
    db: Session = Depends(get_db),
) -> TicketFeedbackOut:
    feedback = ticket_service.submit_feedback(db, ticket_id, user.id, body.rating, body.comment)
    return TicketFeedbackOut.model_validate(feedback)
