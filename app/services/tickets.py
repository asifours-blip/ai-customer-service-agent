"""工单服务：创建（含幂等与重复检测）、查询、领取、SUPPORT 状态机、双方回复、反馈与处理记录。

幂等语义（审核修订①）：同一用户带相同 idempotency_key 重复提交相同请求 → 返回首次工单，
不产生第二条；key 只在该用户内生效，同 key 但业务字段不同 → IDEMPOTENCY_CONFLICT。
Agent 的 create_ticket Tool 与 REST POST /api/tickets 都走这里。

工作台规则（阶段 2）：
- 领取：SELECT ... FOR UPDATE 锁行后再判断 assignee_id，两名客服并发领取只有一方成功；
  不支持客服之间自行转交（见 docs/decisions.md D-019）
- 只有领取人能推进状态、以客服身份回复；CLOSED 后双方都不能再回复
- 反馈：RESOLVED / CLOSED 后客户本人可提交一次，重复提交 → DUPLICATE（409），不支持修改
- 所有写操作与对应的 ticket_events 记录在同一事务提交；ticket_events 只追加
"""

from __future__ import annotations

import hashlib

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models.ticket import (
    EVENT_CLAIMED,
    EVENT_CREATED,
    EVENT_FEEDBACK_SUBMITTED,
    EVENT_REPLIED,
    EVENT_STATUS_CHANGED,
    Ticket,
    TicketEvent,
    TicketFeedback,
    TicketReply,
)
from app.services import ticket_state
from app.services.errors import (
    AlreadyAssignedError,
    DuplicateError,
    IdempotencyConflictError,
    InvalidStateError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from app.services.orders import get_order
from app.services.permission import ROLE_CUSTOMER, ROLE_SUPPORT, ensure_owner

_IDEMPOTENCY_KEY_UNIQUE_CONSTRAINT = "tickets_user_id_idempotency_key_key"
_FEEDBACK_UNIQUE_CONSTRAINT = "ticket_feedback_ticket_id_key"
FEEDBACK_ALLOWED_STATUSES = ("RESOLVED", "CLOSED")
SUPPORT_SCOPES = ("all", "unassigned", "mine")


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


def _is_unique_violation(exc: IntegrityError, constraint_name: str) -> bool:
    """仅识别 PostgreSQL 指定名称的唯一约束冲突，其他完整性错误原样上抛。"""
    original = exc.orig
    return (
        getattr(original, "sqlstate", None) == "23505"
        and getattr(getattr(original, "diag", None), "constraint_name", None) == constraint_name
    )


def _is_idempotency_key_unique_violation(exc: IntegrityError) -> bool:
    """仅识别 PostgreSQL 的 tickets (user_id, idempotency_key) 唯一约束。"""
    return _is_unique_violation(exc, _IDEMPOTENCY_KEY_UNIQUE_CONSTRAINT)


def _record_event(
    db: Session,
    ticket: Ticket,
    event_type: str,
    actor_id: str,
    actor_role: str,
    *,
    from_status: str | None = None,
    to_status: str | None = None,
    reply_id: int | None = None,
) -> None:
    """追加一条处理记录（与业务写入同事务，调用方负责 commit）。本模块不提供修改或删除入口。"""
    db.add(
        TicketEvent(
            ticket_id=ticket.id,
            event_type=event_type,
            actor_id=actor_id,
            actor_role=actor_role,
            from_status=from_status,
            to_status=to_status,
            reply_id=reply_id,
        )
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
    try:
        db.add(ticket)
        db.flush()  # D-013：先落工单行，再写引用它的处理记录（唯一约束冲突也在这里抛出）
        _record_event(db, ticket, EVENT_CREATED, user_id, ROLE_CUSTOMER, to_status=ticket.status)
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


def list_tickets_for_support(
    db: Session, status: str | None = None, *, scope: str = "all", support_user_id: str | None = None
) -> list[Ticket]:
    """客服队列：scope=all 全部 / unassigned 未指派 / mine 我领取的。"""
    if scope not in SUPPORT_SCOPES:
        raise ValidationFailedError(f"未知队列范围: {scope}")
    stmt = select(Ticket).order_by(Ticket.created_at.desc(), Ticket.id.desc())
    if status is not None:
        stmt = stmt.where(Ticket.status == status)
    if scope == "unassigned":
        stmt = stmt.where(Ticket.assignee_id.is_(None))
    elif scope == "mine":
        if support_user_id is None:
            raise ValidationFailedError("scope=mine 需要当前客服身份")
        stmt = stmt.where(Ticket.assignee_id == support_user_id)
    return list(db.scalars(stmt))


def _lock_ticket(db: Session, ticket_id: str) -> Ticket:
    """SELECT ... FOR UPDATE：同一工单的领取、迁移、回复、反馈按行串行化，后到者基于最新状态判断。"""
    ticket = db.get(Ticket, ticket_id, with_for_update=True, populate_existing=True)
    if ticket is None:
        raise NotFoundError(f"工单不存在: {ticket_id}")
    return ticket


def ensure_claimable(ticket: Ticket, support_user_id: str) -> None:
    """领取前置条件（必须在持有行锁之后调用）：未被他人领取、未关闭。"""
    if ticket.assignee_id is not None and ticket.assignee_id != support_user_id:
        raise AlreadyAssignedError(f"工单 {ticket.id} 已被其他客服领取")
    if ticket.status == "CLOSED":
        raise InvalidStateError(f"工单 {ticket.id} 已关闭，不能领取")


def ensure_assignee(ticket: Ticket, support_user_id: str) -> None:
    """只有领取人能推进状态、以客服身份回复。"""
    if ticket.assignee_id is None:
        raise PermissionDeniedError(f"工单 {ticket.id} 尚未领取，请先领取再处理")
    if ticket.assignee_id != support_user_id:
        raise PermissionDeniedError(f"工单 {ticket.id} 由其他客服处理，仅领取人可操作")


def claim_ticket(db: Session, ticket_id: str, support_user_id: str) -> Ticket:
    """领取工单：并发领取时行锁保证只有一方成功，另一方得到 ALREADY_ASSIGNED；本人重复领取幂等返回。"""
    ticket = _lock_ticket(db, ticket_id)
    ensure_claimable(ticket, support_user_id)
    if ticket.assignee_id == support_user_id:
        db.commit()  # 结束事务、释放行锁，不重复记录
        return ticket
    ticket.assignee_id = support_user_id
    ticket.updated_at = utcnow()
    _record_event(db, ticket, EVENT_CLAIMED, support_user_id, ROLE_SUPPORT)
    db.commit()
    db.refresh(ticket)
    return ticket


def transition_ticket(db: Session, ticket_id: str, target: str, support_user_id: str) -> Ticket:
    """SUPPORT 专用状态迁移（v1.1 补丁：OPEN→PROCESSING→RESOLVED→CLOSED 单向），仅领取人可操作。

    SELECT ... FOR UPDATE：并发迁移按行串行化，后到者基于最新状态校验，
    避免两方都读到旧状态后各自「合法」迁移（双迁移 / 覆盖对方结果）。
    """
    ticket = _lock_ticket(db, ticket_id)
    ensure_assignee(ticket, support_user_id)
    ticket_state.assert_transition(ticket.status, target)
    previous = ticket.status
    ticket.status = target
    ticket.updated_at = utcnow()
    _record_event(
        db, ticket, EVENT_STATUS_CHANGED, support_user_id, ROLE_SUPPORT, from_status=previous, to_status=target
    )
    db.commit()
    db.refresh(ticket)
    return ticket


def _append_reply(db: Session, ticket: Ticket, author_id: str, author_role: str, content: str) -> TicketReply:
    if ticket.status == "CLOSED":
        raise InvalidStateError(f"工单 {ticket.id} 已关闭，不能再回复")
    reply = TicketReply(ticket_id=ticket.id, author_id=author_id, author_role=author_role, content=content)
    db.add(reply)
    db.flush()  # D-013：先拿到 reply.id，再写引用它的处理记录
    _record_event(db, ticket, EVENT_REPLIED, author_id, author_role, reply_id=reply.id)
    ticket.updated_at = utcnow()
    db.commit()
    db.refresh(reply)
    return reply


def add_support_reply(db: Session, ticket_id: str, support_user_id: str, content: str) -> TicketReply:
    ticket = _lock_ticket(db, ticket_id)
    ensure_assignee(ticket, support_user_id)
    return _append_reply(db, ticket, support_user_id, ROLE_SUPPORT, content)


def add_customer_reply(db: Session, ticket_id: str, user_id: str, content: str) -> TicketReply:
    ticket = _lock_ticket(db, ticket_id)
    ensure_owner(ticket.user_id, user_id)  # 先鉴权，再暴露状态信息
    return _append_reply(db, ticket, user_id, ROLE_CUSTOMER, content)


def list_replies(db: Session, ticket_id: str) -> list[TicketReply]:
    return list(
        db.scalars(
            select(TicketReply)
            .where(TicketReply.ticket_id == ticket_id)
            .order_by(TicketReply.created_at, TicketReply.id)
        )
    )


def list_events(db: Session, ticket_id: str) -> list[TicketEvent]:
    return list(
        db.scalars(
            select(TicketEvent)
            .where(TicketEvent.ticket_id == ticket_id)
            .order_by(TicketEvent.created_at, TicketEvent.id)
        )
    )


def submit_feedback(db: Session, ticket_id: str, user_id: str, rating: int, comment: str) -> TicketFeedback:
    """客户对已解决工单评分（1–5）：每张工单只能提交一次，不支持修改。"""
    ticket = _lock_ticket(db, ticket_id)
    ensure_owner(ticket.user_id, user_id)
    if ticket.status not in FEEDBACK_ALLOWED_STATUSES:
        raise InvalidStateError(f"工单 {ticket.id} 当前状态为 {ticket.status}，解决后才能评价")
    if get_feedback(db, ticket.id) is not None:
        raise DuplicateError(f"工单 {ticket.id} 已提交过反馈")
    feedback = TicketFeedback(ticket_id=ticket.id, user_id=user_id, rating=rating, comment=comment)
    try:
        db.add(feedback)
        db.flush()  # 行锁已串行化同一工单的提交；唯一约束兜底绕过服务层的写入
    except IntegrityError as exc:
        db.rollback()
        if _is_unique_violation(exc, _FEEDBACK_UNIQUE_CONSTRAINT):
            raise DuplicateError(f"工单 {ticket_id} 已提交过反馈") from exc
        raise
    _record_event(db, ticket, EVENT_FEEDBACK_SUBMITTED, user_id, ROLE_CUSTOMER)
    db.commit()
    db.refresh(feedback)
    return feedback


def get_feedback(db: Session, ticket_id: str) -> TicketFeedback | None:
    return db.scalar(select(TicketFeedback).where(TicketFeedback.ticket_id == ticket_id))


def list_feedback(db: Session) -> list[tuple[TicketFeedback, Ticket]]:
    """反馈及所属工单（供客服复盘与评测集导出），按提交时间升序。"""
    rows = db.execute(
        select(TicketFeedback, Ticket)
        .join(Ticket, Ticket.id == TicketFeedback.ticket_id)
        .order_by(TicketFeedback.created_at, TicketFeedback.id)
    ).all()
    return [(fb, t) for fb, t in rows]
