"""工单详情视图装配：客户视图与客服视图共用数据，区别只在内部字段是否可见。

客户视图隐藏：assignee_id（schema 层不含该字段）、客服回复的 author_id、客服操作的 actor_id。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.ticket import Ticket
from app.schemas.api import (
    SupportTicketDetailOut,
    TicketDetailOut,
    TicketEventOut,
    TicketFeedbackOut,
    TicketReplyOut,
)
from app.services import tickets as ticket_service
from app.services.permission import ROLE_SUPPORT


def _feedback(db: Session, ticket_id: str) -> TicketFeedbackOut | None:
    fb = ticket_service.get_feedback(db, ticket_id)
    return TicketFeedbackOut.model_validate(fb) if fb else None


def customer_detail(db: Session, ticket: Ticket) -> TicketDetailOut:
    out = TicketDetailOut.model_validate(ticket)
    out.replies = [
        TicketReplyOut.model_validate(r).model_copy(
            update={"author_id": None} if r.author_role == ROLE_SUPPORT else {}
        )
        for r in ticket_service.list_replies(db, ticket.id)
    ]
    out.events = [
        TicketEventOut.model_validate(e).model_copy(update={"actor_id": None} if e.actor_role == ROLE_SUPPORT else {})
        for e in ticket_service.list_events(db, ticket.id)
    ]
    out.feedback = _feedback(db, ticket.id)
    return out


def support_detail(db: Session, ticket: Ticket) -> SupportTicketDetailOut:
    out = SupportTicketDetailOut.model_validate(ticket)
    out.replies = [TicketReplyOut.model_validate(r) for r in ticket_service.list_replies(db, ticket.id)]
    out.events = [TicketEventOut.model_validate(e) for e in ticket_service.list_events(db, ticket.id)]
    out.feedback = _feedback(db, ticket.id)
    return out
