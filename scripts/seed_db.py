"""确定性种子数据：重复执行幂等（已存在则跳过）。

演示账号（务必与 README/演示脚本一致）：
  客户 U001 demo_customer / demo123   —— 主演示用户
  客户 U002 second_customer / demo123 —— 提供"别人家的订单"（IDOR 测试）
  客服 SUPPORT001 support_agent / demo123

设计意图：
  A10001  DELIVERED 签收 3 天 → 命中 7 天退货政策（售后主流程 Demo ④）
  A20001  DELIVERED 签收 20 天 → 超出退货窗口（Eligibility 拒绝分支）
  A10003  PENDING 未支付 → 不可售后（状态不符分支）
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from decimal import Decimal

from app.models.base import utcnow
from app.models.conversation import Conversation
from app.models.logistics import Logistics
from app.models.order import Order
from app.models.product import Product
from app.models.ticket import Ticket, TicketReply
from app.models.user import User
from app.security.auth import hash_password
from app.services.database import SessionLocal

PRODUCTS: list[dict[str, str]] = [
    {
        "id": "P001",
        "name": "AirMusic Pro 无线降噪耳机",
        "model": "AMP-2026",
        "description": "主动降噪蓝牙耳机，支持 LDAC，续航 40 小时。",
    },
    {
        "id": "P002",
        "name": "PulseWatch 2 智能手表",
        "model": "PW2-44mm",
        "description": "心率/血氧监测，50 米防水，续航 14 天。",
    },
    {
        "id": "P003",
        "name": "SoundBox mini 蓝牙音箱",
        "model": "SBM-01",
        "description": "便携蓝牙音箱，IPX7 防水，续航 20 小时。",
    },
]


def seed() -> None:
    db = SessionLocal()
    try:
        if db.get(User, "U001") is not None:
            print("seed: 已存在，跳过（幂等）")
            return

        now = utcnow()
        pwd = hash_password("demo123")

        users = [
            User(id="U001", username="demo_customer", email="u001@example.com", role="CUSTOMER", password_hash=pwd),
            User(id="U002", username="second_customer", email="u002@example.com", role="CUSTOMER", password_hash=pwd),
            User(
                id="SUPPORT001",
                username="support_agent",
                email="support@example.com",
                role="SUPPORT",
                password_hash=pwd,
            ),
        ]
        db.add_all(users)
        db.add_all(Product(status="ACTIVE", **p) for p in PRODUCTS)
        db.flush()  # 决策 D-013：按依赖层级显式 flush，不依赖 UOW 跨 mapper 排序

        def t(days_ago: int) -> datetime:
            return now - timedelta(days=days_ago)

        orders = [
            Order(id="A10001", user_id="U001", product_id="P001", status="DELIVERED", amount=Decimal("299.00"),
                  paid_at=t(6), shipped_at=t(5), delivered_at=t(3)),
            Order(id="A10002", user_id="U001", product_id="P002", status="SHIPPED", amount=Decimal("1299.00"),
                  paid_at=t(2), shipped_at=t(1)),
            Order(id="A10003", user_id="U001", product_id="P003", status="PENDING", amount=Decimal("199.00")),
            Order(id="A10004", user_id="U001", product_id="P001", status="CANCELLED", amount=Decimal("299.00")),
            Order(id="A20001", user_id="U002", product_id="P001", status="DELIVERED", amount=Decimal("299.00"),
                  paid_at=t(23), shipped_at=t(22), delivered_at=t(20)),
            Order(id="A20002", user_id="U002", product_id="P002", status="PAID", amount=Decimal("1299.00"),
                  paid_at=t(1)),
        ]
        db.add_all(orders)
        db.flush()  # D-013：订单层

        db.add_all(
            [
                Logistics(order_id="A10001", carrier="顺丰速运", tracking_number="SF1234567890",
                          status="已签收", current_location="杭州滨江站"),
                Logistics(order_id="A10002", carrier="中通快递", tracking_number="ZT9876543210",
                          status="派送中", current_location="杭州余杭区派送点"),
                Logistics(order_id="A20001", carrier="圆通速递", tracking_number="YT1122334455",
                          status="已签收", current_location="宁波鄞州区"),
            ]
        )

        db.add_all(
            [
                Ticket(id="T10001", user_id="U001", order_id="A10001", category="REPAIR",
                       title="左耳降噪异常", description="开启降噪后左耳有电流声。",
                       status="OPEN", priority="MEDIUM"),
                Ticket(id="T10002", user_id="U002", order_id="A20001", category="REFUND",
                       title="超期退款申请咨询", description="购买 20 天，还能退吗？",
                       status="PROCESSING", priority="HIGH"),
            ]
        )
        db.flush()  # D-013：工单层

        db.add(TicketReply(ticket_id="T10002", author_id="SUPPORT001", author_role="SUPPORT",
                           content="您好，已收到您的申请，正在核对订单签收时间，请稍候。"))

        # 一个空的演示会话（前端 Phase 6 使用）
        db.add(Conversation(user_id="U001", session_id="demo-session-001", title="演示会话"))

        db.commit()
        print("seed: 完成（3 用户 / 3 商品 / 6 订单 / 3 物流 / 2 工单 / 1 会话）")
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(seed())
