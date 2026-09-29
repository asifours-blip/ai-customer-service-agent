"""经客服显式审核的物流破损案例；只存结构化、非客户原文。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow


class DamageCase(Base):
    __tablename__ = "damage_cases"

    source_ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id"), primary_key=True)
    damage_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    reviewed_path: Mapped[str] = mapped_column(String(32), nullable=False)
    product_id: Mapped[str] = mapped_column(String(16), nullable=False)
    order_status: Mapped[str] = mapped_column(String(16), nullable=False)
    logistics_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reviewed_policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    approved_by: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    withdrawn_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
