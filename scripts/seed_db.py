"""确定性种子数据：重复执行幂等（已存在则跳过）。

演示账号（本地测试凭据，仅用于演示/测试库；务必与 README 一致）：
  客户 U001 demo_customer / demo123     —— 主演示用户
  客户 U002 second_customer / demo123   —— 提供"别人家的订单"（IDOR 测试）
  客服 SUPPORT001 support_agent / demo123
  客服 SUPPORT002 support_agent2 / demo123 —— 第二名客服（领取竞争、非领取人鉴权）

设计意图：
  A10001  DELIVERED 签收 3 天 → 命中 7 天退货政策（售后主流程 Demo ④）
  A20001  DELIVERED 签收 20 天 → 超出退货窗口（Eligibility 拒绝分支）
  A10003  PENDING 未支付 → 不可售后（状态不符分支）
  T10001  OPEN 未指派 → 客服队列「未指派」
  T10002  PROCESSING，由 SUPPORT001 领取处理中（含完整处理记录）
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
from app.models.ticket import Ticket, TicketEvent, TicketReply
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


# (id, username, email, role)；口令统一为 DEMO_PASSWORD
USERS: list[tuple[str, str, str, str]] = [
    ("U001", "demo_customer", "u001@example.com", "CUSTOMER"),
    ("U002", "second_customer", "u002@example.com", "CUSTOMER"),
    ("SUPPORT001", "support_agent", "support@example.com", "SUPPORT"),
    ("SUPPORT002", "support_agent2", "support2@example.com", "SUPPORT"),
]
DEMO_PASSWORD = "demo123"


def seed() -> int:
    db = SessionLocal()
    try:
        if db.get(User, "U001") is not None:
            # 已有库：只补齐后加入的演示账号（如 SUPPORT002），业务数据不动
            missing = [u for u in USERS if db.get(User, u[0]) is None]
            if missing:
                pwd = hash_password(DEMO_PASSWORD)
                db.add_all(User(id=i, username=n, email=e, role=r, password_hash=pwd) for i, n, e, r in missing)
                db.commit()
                print(f"seed: 已存在，补齐账号 {[u[0] for u in missing]}")
            else:
                print("seed: 已存在，跳过（幂等）")
            return 0

        now = utcnow()
        pwd = hash_password(DEMO_PASSWORD)

        db.add_all(User(id=i, username=n, email=e, role=r, password_hash=pwd) for i, n, e, r in USERS)
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
                       status="PROCESSING", priority="HIGH", assignee_id="SUPPORT001",
                       created_at=now - timedelta(hours=2), updated_at=now - timedelta(hours=1)),
            ]
        )
        db.flush()  # D-013：工单层

        reply = TicketReply(ticket_id="T10002", author_id="SUPPORT001", author_role="SUPPORT",
                            content="您好，已收到您的申请，正在核对订单签收时间，请稍候。",
                            created_at=now - timedelta(hours=1))
        db.add(reply)
        db.flush()  # D-013：回复层（处理记录引用 reply.id）

        # 处理记录与上面的工单状态保持一致
        def ev(ticket_id: str, event_type: str, actor: str, role: str, at: datetime, **kw: object) -> TicketEvent:
            return TicketEvent(ticket_id=ticket_id, event_type=event_type, actor_id=actor, actor_role=role,
                               created_at=at, **kw)

        db.add_all(
            [
                ev("T10001", "CREATED", "U001", "CUSTOMER", now, to_status="OPEN"),
                ev("T10002", "CREATED", "U002", "CUSTOMER", now - timedelta(hours=2), to_status="OPEN"),
                ev("T10002", "CLAIMED", "SUPPORT001", "SUPPORT", now - timedelta(minutes=90)),
                ev("T10002", "STATUS_CHANGED", "SUPPORT001", "SUPPORT", now - timedelta(minutes=89),
                   from_status="OPEN", to_status="PROCESSING"),
                ev("T10002", "REPLIED", "SUPPORT001", "SUPPORT", now - timedelta(hours=1), reply_id=reply.id),
            ]
        )

        # 一个空的演示会话（前端 Phase 6 使用）
        db.add(Conversation(user_id="U001", session_id="demo-session-001", title="演示会话"))

        db.commit()
        print("seed: 完成（4 用户 / 3 商品 / 6 订单 / 3 物流 / 2 工单 / 1 会话）")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(seed())
