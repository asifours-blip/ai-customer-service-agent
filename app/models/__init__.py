"""SQLAlchemy 模型。所有表集中在此包导出，Alembic autogenerate 依赖统一 metadata。"""

from app.models.base import Base
from app.models.conversation import Conversation, Message
from app.models.feedback import AnswerFeedback, FeedbackReviewAudit
from app.models.knowledge import KbAuditLog, KbChunk, KbDocument, KbVersion
from app.models.logistics import Logistics
from app.models.order import Order
from app.models.product import Product
from app.models.ticket import Ticket, TicketEvent, TicketFeedback, TicketReply
from app.models.trace import AgentTrace
from app.models.user import User

__all__ = [
    "Base",
    "User",
    "Product",
    "Order",
    "Logistics",
    "Ticket",
    "TicketReply",
    "TicketEvent",
    "TicketFeedback",
    "Conversation",
    "Message",
    "AgentTrace",
    "KbVersion",
    "KbDocument",
    "KbChunk",
    "KbAuditLog",
    "AnswerFeedback",
    "FeedbackReviewAudit",
]
