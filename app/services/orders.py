"""订单服务：REST API 与 Agent Tool 共用的业务层（Graph=编排，Service=业务逻辑）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.logistics import Logistics
from app.models.order import Order
from app.services.errors import NotFoundError
from app.services.permission import ensure_owner


def get_order(db: Session, order_id: str, current_user_id: str) -> Order:
    """按 ID 查订单并做资源级授权（防 IDOR）。

    不存在与无权统一 403/404 语义：不存在 → 404；存在但不属于本人 → 403。
    """
    order = db.get(Order, order_id)
    if order is None:
        raise NotFoundError(f"订单不存在: {order_id}")
    ensure_owner(order.user_id, current_user_id)
    return order


def list_orders(db: Session, user_id: str) -> list[Order]:
    # created_at 同秒平局时 PG 返回顺序不确定（CI/Linux 曾翻车），必须加 id 决胜
    return list(
        db.scalars(
            select(Order)
            .where(Order.user_id == user_id)
            .order_by(Order.created_at.desc(), Order.id.asc())
        )
    )


def get_logistics(db: Session, order_id: str, current_user_id: str) -> Logistics:
    order = get_order(db, order_id, current_user_id)  # 权限随订单校验
    logistics = db.scalar(select(Logistics).where(Logistics.order_id == order.id))
    if logistics is None:
        raise NotFoundError(f"订单 {order_id} 暂无物流记录")
    return logistics
