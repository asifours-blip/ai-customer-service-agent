"""工单工具：query_ticket（READ_ONLY）与 create_ticket（SIDE_EFFECT，幂等）。

create_ticket 幂等语义（审核修订①）：
- 禁止无条件自动重试（调用方/重试器必须遵守 kind 约束）
- idempotency_key 由 pending_action_id 派生（Phase 4 Agent 生成），DB UNIQUE 兜底
- 相同 key 重复调用 → 返回首次创建的工单（replay=True），不产生第二条
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.models.ticket import TICKET_CATEGORIES, TICKET_PRIORITIES
from app.services import tickets as ticket_service
from app.services.errors import DuplicateError
from app.tools.base import BaseTool, ToolKind, ToolResult

_ID_PATTERN = r"^(A\d{4,6}|T\d{4,6})$"


class QueryTicketArgs(BaseModel):
    ticket_id: str = Field(min_length=4, max_length=16, pattern=r"^T\d{4,6}$")


class QueryTicketTool(BaseTool):
    name = "query_ticket"
    kind = ToolKind.READ_ONLY
    args_model = QueryTicketArgs

    def _run(self, db: Session, current_user_id: str, args: BaseModel) -> ToolResult:
        assert isinstance(args, QueryTicketArgs)
        t = ticket_service.get_ticket(db, args.ticket_id, current_user_id)
        return ToolResult.ok(
            self,
            {
                "ticket_id": t.id,
                "status": t.status,
                "category": t.category,
                "title": t.title,
                "priority": t.priority,
                "order_id": t.order_id,
                "created_at": t.created_at.isoformat(),
                "updated_at": t.updated_at.isoformat(),
            },
        )


class CreateTicketArgs(BaseModel):
    order_id: str | None = Field(default=None, pattern=r"^A\d{4,6}$")
    category: str = Field(pattern=f"^({'|'.join(TICKET_CATEGORIES)})$")
    title: str = Field(min_length=2, max_length=128)
    description: str = Field(default="", max_length=4000)
    priority: str = Field(default="MEDIUM", pattern=f"^({'|'.join(TICKET_PRIORITIES)})$")
    # 幂等键：SIDE_EFFECT 工具强制要求（最小 8 位，防误传单字符）
    idempotency_key: str = Field(min_length=8, max_length=64)


class CreateTicketTool(BaseTool):
    name = "create_ticket"
    kind = ToolKind.SIDE_EFFECT
    args_model = CreateTicketArgs

    def _run(self, db: Session, current_user_id: str, args: BaseModel) -> ToolResult:
        assert isinstance(args, CreateTicketArgs)
        ticket, created = ticket_service.create_ticket(
            db,
            user_id=current_user_id,
            category=args.category,
            title=args.title,
            description=args.description,
            priority=args.priority,
            order_id=args.order_id,
            idempotency_key=args.idempotency_key,
        )
        data = {
            "ticket_id": ticket.id,
            "status": ticket.status,
            "category": ticket.category,
            "order_id": ticket.order_id,
            "created": created,
        }
        return ToolResult.ok(self, data, replay=not created)


# ---- 注册表 ----

from app.tools.base import ToolRegistry  # noqa: E402
from app.tools.order import QueryLogisticsTool, QueryOrderTool  # noqa: E402


def build_registry() -> ToolRegistry:
    reg = ToolRegistry()
    for tool in (QueryOrderTool(), QueryLogisticsTool(), QueryTicketTool(), CreateTicketTool()):
        reg.register(tool)
    return reg


__all__ = [
    "QueryOrderTool",
    "QueryLogisticsTool",
    "QueryTicketTool",
    "CreateTicketTool",
    "CreateTicketArgs",
    "build_registry",
    "DuplicateError",
    "_ID_PATTERN",
]
