"""订单查询 API（仅本人）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.models.logistics import Logistics
from app.models.user import User
from app.schemas.api import LogisticsOut, OrderDetailOut, OrderOut
from app.security.dependencies import get_current_user
from app.services import orders as order_service
from app.services.database import get_db
from app.services.errors import NotFoundError

router = APIRouter()


@router.get("", response_model=list[OrderOut])
def list_my_orders(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[OrderOut]:
    return [OrderOut.model_validate(o) for o in order_service.list_orders(db, user.id)]


@router.get("/logistics/{order_id}", response_model=LogisticsOut)
def get_my_logistics(
    order_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> LogisticsOut:
    return LogisticsOut.model_validate(order_service.get_logistics(db, order_id, user.id))


@router.get("/{order_id}", response_model=OrderDetailOut)
def get_my_order(
    order_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OrderDetailOut:
    order = order_service.get_order(db, order_id, user.id)
    logistics = db.query(Logistics).filter_by(order_id=order.id).one_or_none()
    out = OrderDetailOut.model_validate(order)
    out.logistics = LogisticsOut.model_validate(logistics) if logistics else None
    if out.logistics is None and order.status in ("SHIPPED", "DELIVERED"):
        # 状态显示已发货但无物流记录属数据异常，显式暴露而非静默
        raise NotFoundError(f"订单 {order_id} 缺少物流记录（数据异常）")
    return out
