"""订单/物流查询工具（READ_ONLY）。权限由 service 层判定，LLM 无法越权。"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.services import orders as order_service
from app.tools.base import BaseTool, ToolKind, ToolResult


class QueryOrderArgs(BaseModel):
    order_id: str = Field(min_length=4, max_length=16, pattern=r"^A\d{4,6}$")


class QueryOrderTool(BaseTool):
    name = "query_order"
    kind = ToolKind.READ_ONLY
    args_model = QueryOrderArgs

    def _run(self, db: Session, current_user_id: str, args: BaseModel) -> ToolResult:
        assert isinstance(args, QueryOrderArgs)
        order = order_service.get_order(db, args.order_id, current_user_id)  # 404/403 由 AppError 映射
        return ToolResult.ok(
            self,
            {
                "order_id": order.id,
                "status": order.status,
                "product_id": order.product_id,
                "amount": str(Decimal(order.amount)),
                "created_at": order.created_at.isoformat(),
                "paid_at": order.paid_at.isoformat() if order.paid_at else None,
                "shipped_at": order.shipped_at.isoformat() if order.shipped_at else None,
                "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
            },
        )


class QueryLogisticsArgs(BaseModel):
    order_id: str = Field(min_length=4, max_length=16, pattern=r"^A\d{4,6}$")


class QueryLogisticsTool(BaseTool):
    name = "query_logistics"
    kind = ToolKind.READ_ONLY
    args_model = QueryLogisticsArgs

    def _run(self, db: Session, current_user_id: str, args: BaseModel) -> ToolResult:
        assert isinstance(args, QueryLogisticsArgs)
        logi = order_service.get_logistics(db, args.order_id, current_user_id)
        return ToolResult.ok(
            self,
            {
                "order_id": logi.order_id,
                "carrier": logi.carrier,
                "tracking_number": logi.tracking_number,
                "status": logi.status,
                "current_location": logi.current_location,
                "updated_at": logi.updated_at.isoformat(),
            },
        )
