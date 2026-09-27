"""工单服务：创建（含幂等与重复检测）、查询、SUPPORT 状态机与回复。

幂等语义（审核修订①）：同一用户带相同 idempotency_key 重复提交相同请求 → 返回首次工单，
不产生第二条；key 只在该用户内生效，同 key 但业务字段不同 → IDEMPOTENCY_CONFLICT。
Agent 的 create_ticket Tool 与 REST POST /api/tickets 都走这里。
"""

from __future__ import annotations

import hashlib

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.ticket import Ticket, TicketReply
from app.services import ticket_state
from app.services.errors import (
    DuplicateError,
    IdempotencyConflictError,
    NotFoundError,
    ValidationFailedError,
)
from app.services.orders import get_order
from app.services.permission import ensure_owner

_IDEMPOTENCY_KEY_UNIQUE_CONSTRAINT = "tickets_user_id_idempotency_key_key"


def ticket_request_fingerprint(
    *, category: str, title: str, description: str, priority: str, order_id: str | None
) -> str:
    """规范化业务字段 → sha256 十六进制。

    规范化：固定字段顺序；每个字段编码为「UTF-8 字节长度:原文」，NULL 编码为 "~"；
    以 "|" 连接并加版本前缀 "v1|"。长度前缀保证任意内容都不会拼接歧义。
    Alembic 迁移 d7a1e5b3c9f2 用 PostgreSQL sha256() 按同一算法回填历史数据，两边必须同步修改。
    """
    parts: list[str] = []
    for value in (category, title, description, priority, order_id):
        parts.append("~" if value is None else f"{len(value.encode('utf-8'))}:{value}")
    canonical = "v1|" + "|".join(parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _find_by_idempotency_key(db: Session, user_id: str, idempotency_key: str) -> Ticket | None:
    """幂等键查询必须带 user_id：key 的作用域是用户，而不是全局。"""
    return db.scalar(
        select(Ticket).where(Ticket.user_id == user_id, Ticket.idempotency_key == idempotency_key)
    )


def _replay(existing: Ticket, fingerprint: str) -> tuple[Ticket, bool]:
    """命中幂等键：指纹一致才是重放，否则是同 key 的另一份请求。"""
    if existing.request_fingerprint != fingerprint:
        raise IdempotencyConflictError(f"该幂等键已用于内容不同的工单请求（工单 {existing.id}），请更换幂等键")
    return existing, False


def _is_idempotency_key_unique_violation(exc: IntegrityError) -> bool:
    """仅识别 PostgreSQL 的 tickets (user_id, idempotency_key) 唯一约束。"""
    original = exc.orig
    return (
        getattr(original, "sqlstate", None) == "23505"
        and getattr(getattr(original, "diag", None), "constraint_name", None)
        == _IDEMPOTENCY_KEY_UNIQUE_CONSTRAINT
    )


def next_ticket_id(db: Session) -> str:
    """从 PostgreSQL sequence 分配数字，再保持既有 T<number> 业务格式。"""
    number = db.scalar(text("SELECT nextval('ticket_id_sequence')"))
    if not isinstance(number, int):
        raise ValidationFailedError("ticket_id_sequence 未返回整数")
    return f"T{number}"


def create_ticket(
    db: Session,
    *,
    user_id: str,
    category: str,
    title: str,
    description: str = "",
    priority: str = "MEDIUM",
    order_id: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[Ticket, bool]:
    """创建工单。返回 (ticket, created)；幂等重放时 created=False。

    - order_id 提供时：必须存在且属于当前用户（权限由后端判定，LLM 无权决定）
    - 同一 order + category + OPEN 状态的重复申请 → DUPLICATE（防用户重复提交）
    - 本人 idempotency_key 命中且请求指纹一致 → 直接返回首次工单（SIDE_EFFECT 重试安全）；
      指纹不同 → IDEMPOTENCY_CONFLICT；其他用户的同名 key 互不影响
    """
    if order_id is not None:
        get_order(db, order_id, user_id)  # 不存在/非本人 → 404/403

    fingerprint = ticket_request_fingerprint(
        category=category, title=title, description=description, priority=priority, order_id=order_id
    )
    if idempotency_key is not None:
        existing = _find_by_idempotency_key(db, user_id, idempotency_key)
        if existing is not None:
            return _replay(existing, fingerprint)

    if order_id is not None:
        dup = db.scalar(
            select(Ticket).where(
                Ticket.user_id == user_id,
                Ticket.order_id == order_id,
                Ticket.category == category,
                Ticket.status.in_(("OPEN", "PROCESSING")),
            )
        )
        if dup is not None:
            if idempotency_key is not None:
                existing = _find_by_idempotency_key(db, user_id, idempotency_key)
                if existing is not None:
                    return _replay(existing, fingerprint)
            raise DuplicateError(f"该订单已存在同类进行中的工单: {dup.id}")

    ticket = Ticket(
        id=next_ticket_id(db),
        user_id=user_id,
        order_id=order_id,
        category=category,
        title=title,
        description=description,
        priority=priority,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
    )
    db.add(ticket)
    try:
        db.commit()
    except IntegrityError as exc:
        if idempotency_key is None or not _is_idempotency_key_unique_violation(exc):
            raise
        db.rollback()
        existing = _find_by_idempotency_key(db, user_id, idempotency_key)
        if existing is None:
            raise
        return _replay(existing, fingerprint)
    db.refresh(ticket)
    return ticket, True


def get_ticket(db: Session, ticket_id: str, current_user_id: str, *, is_support: bool = False) -> Ticket:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise NotFoundError(f"工单不存在: {ticket_id}")
    if not is_support:
        ensure_owner(ticket.user_id, current_user_id)
    return ticket


def list_tickets(db: Session, user_id: str) -> list[Ticket]:
    return list(
        db.scalars(
            select(Ticket)
            .where(Ticket.user_id == user_id)
            .order_by(Ticket.created_at.desc(), Ticket.id.desc())
        )
    )


def list_tickets_for_support(db: Session, status: str | None = None) -> list[Ticket]:
    stmt = select(Ticket).order_by(Ticket.created_at.desc(), Ticket.id.desc())
    if status is not None:
        stmt = stmt.where(Ticket.status == status)
    return list(db.scalars(stmt))


def transition_ticket(db: Session, ticket_id: str, target: str, support_user_id: str) -> Ticket:
    """SUPPORT 专用状态迁移（v1.1 补丁：OPEN→PROCESSING→RESOLVED→CLOSED 单向）。"""
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise NotFoundError(f"工单不存在: {ticket_id}")
    ticket_state.assert_transition(ticket.status, target)
    ticket.status = target
    _ = support_user_id  # 操作者身份由 API 层依赖注入保证，这里不重复校验
    db.commit()
    db.refresh(ticket)
    return ticket


def add_reply(db: Session, ticket_id: str, author_id: str, author_role: str, content: str) -> TicketReply:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise NotFoundError(f"工单不存在: {ticket_id}")
    reply = TicketReply(ticket_id=ticket.id, author_id=author_id, author_role=author_role, content=content)
    db.add(reply)
    db.commit()
    db.refresh(reply)
    return reply


def list_replies(db: Session, ticket_id: str) -> list[TicketReply]:
    return list(
        db.scalars(
            select(TicketReply)
            .where(TicketReply.ticket_id == ticket_id)
            .order_by(TicketReply.created_at, TicketReply.id)
        )
    )
