"""物流破损历史案例：人工审核、结构化检索、当前事实优先。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models.damage_case import DamageCase
from app.models.logistics import Logistics
from app.models.order import Order
from app.models.ticket import Ticket
from app.services.errors import InvalidStateError, NotFoundError, PermissionDeniedError
from app.services.policy_rules import load_rules
from app.services.tickets import get_ticket

PATH_LABELS = {
    "REQUEST_EVIDENCE": "历史客服曾要求补充破损证据",
    "CARRIER_INVESTIGATION": "历史客服曾核查物流责任",
    "REPLACEMENT_REVIEW": "历史客服曾评估补发或换货",
    "REFUND_REVIEW": "历史客服曾评估退款申请",
}


def _order_context(db: Session, ticket: Ticket) -> tuple[Order, str | None]:
    if ticket.order_id is None:
        raise InvalidStateError("物流破损案例必须关联订单")
    order = db.get(Order, ticket.order_id)
    if order is None or order.user_id != ticket.user_id:
        raise InvalidStateError("工单关联订单不一致，不能生成案例建议")
    logistics = db.scalar(select(Logistics).where(Logistics.order_id == order.id))
    return order, logistics.status if logistics is not None else None


def _view(case: DamageCase) -> dict[str, Any]:
    return {
        "source_ticket_id": case.source_ticket_id,
        "damage_kind": case.damage_kind,
        "reviewed_path": case.reviewed_path,
        "reviewed_path_label": PATH_LABELS[case.reviewed_path],
        "product_id": case.product_id,
        "order_status": case.order_status,
        "logistics_status": case.logistics_status,
        "reviewed_policy_version": case.reviewed_policy_version,
        "approved_by": case.approved_by,
        "approved_at": case.approved_at,
        "withdrawn_by": case.withdrawn_by,
        "withdrawn_at": case.withdrawn_at,
    }


def get_case(db: Session, source_ticket_id: str) -> dict[str, Any] | None:
    get_ticket(db, source_ticket_id, "", is_support=True)
    case = db.get(DamageCase, source_ticket_id)
    return _view(case) if case is not None else None


def approve_case(
    db: Session, source_ticket_id: str, reviewer_id: str, damage_kind: str, reviewed_path: str
) -> dict[str, Any]:
    ticket = get_ticket(db, source_ticket_id, reviewer_id, is_support=True)
    if ticket.assignee_id != reviewer_id or ticket.status not in ("RESOLVED", "CLOSED"):
        raise PermissionDeniedError("只有已解决工单的领取客服可审核发布案例")
    order, logistics_status = _order_context(db, ticket)
    existing = db.get(DamageCase, source_ticket_id)
    if existing is not None:
        if existing.withdrawn_at is not None:
            raise InvalidStateError("该案例已撤回，不可直接重新发布")
        if existing.damage_kind != damage_kind or existing.reviewed_path != reviewed_path:
            raise InvalidStateError("该工单已发布不同的案例，不能重复覆盖")
        return _view(existing)
    case = DamageCase(
        source_ticket_id=source_ticket_id,
        damage_kind=damage_kind,
        reviewed_path=reviewed_path,
        product_id=order.product_id,
        order_status=order.status,
        logistics_status=logistics_status,
        reviewed_policy_version=load_rules().policy_version,
        approved_by=reviewer_id,
        approved_at=utcnow(),
    )
    db.add(case)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        concurrent = db.get(DamageCase, source_ticket_id)
        if concurrent is None or concurrent.withdrawn_at is not None:
            raise
        if concurrent.damage_kind != damage_kind or concurrent.reviewed_path != reviewed_path:
            raise InvalidStateError("该工单已发布不同的案例，不能重复覆盖") from exc
        return _view(concurrent)
    db.refresh(case)
    return _view(case)


def withdraw_case(db: Session, source_ticket_id: str, reviewer_id: str) -> dict[str, Any]:
    get_ticket(db, source_ticket_id, reviewer_id, is_support=True)
    case = db.get(DamageCase, source_ticket_id, with_for_update=True, populate_existing=True)
    if case is None:
        raise NotFoundError("该工单没有已审核案例")
    if case.approved_by != reviewer_id:
        raise PermissionDeniedError("只有原审核客服可撤回案例")
    if case.withdrawn_at is None:
        case.withdrawn_by = reviewer_id
        case.withdrawn_at = utcnow()
        db.commit()
        db.refresh(case)
    return _view(case)


def similar_cases(db: Session, ticket_id: str, damage_kind: str) -> dict[str, Any]:
    ticket = get_ticket(db, ticket_id, "", is_support=True)
    order, logistics_status = _order_context(db, ticket)
    current_policy_version = load_rules().policy_version
    rows = db.scalars(
        select(DamageCase).where(
            DamageCase.withdrawn_at.is_(None),
            DamageCase.damage_kind == damage_kind,
            DamageCase.source_ticket_id != ticket_id,
        )
    ).all()
    ranked: list[dict[str, Any]] = []
    for case in rows:
        comparisons = (
            ("商品", order.product_id, case.product_id),
            ("订单状态", order.status, case.order_status),
            ("物流状态", logistics_status, case.logistics_status),
        )
        similarities = ["破损类型"]
        differences: list[str] = []
        for label, actual, historical in comparisons:
            if actual is None or historical is None:
                differences.append(f"{label}待核对")
            elif actual == historical:
                similarities.append(label)
            else:
                differences.append(label)
        ranked.append({
            "source_ticket_id": case.source_ticket_id,
            "reviewed_path": case.reviewed_path,
            "reviewed_path_label": PATH_LABELS[case.reviewed_path],
            "similarities": similarities,
            "differences": differences,
            "score": len(similarities),
            "reviewed_policy_version": case.reviewed_policy_version,
            "stale_policy": case.reviewed_policy_version != current_policy_version,
        })
    ranked.sort(key=lambda row: (row["stale_policy"], -row["score"], row["source_ticket_id"]))
    cases = ranked[:3]
    current_cases = [case for case in cases if not case["stale_policy"]]
    status = "CASE_ASSISTED" if current_cases else ("HISTORICAL_ONLY" if cases else "NO_CASES")
    missing_information = ["包装及商品破损照片或说明", "物流责任与签收情况"]
    if logistics_status is None:
        missing_information.append("当前物流状态未登记")
    if status == "CASE_ASSISTED":
        first = current_cases[0]
        next_step = (
            f"参考经审核案例 {first['source_ticket_id']} 的处理步骤“{first['reviewed_path_label']}”，"
            "先补齐待核信息，再由客服对照现行政策人工选择处置。"
        )
    elif status == "HISTORICAL_ONLY":
        next_step = "仅找到旧规则版本的历史案例；先按当前订单与现行政策重新核定，历史步骤不得作为当前授权。"
    else:
        next_step = "没有可引用的已审核同类案例；先核对当前事实和破损证据，再由客服人工判断处置。"
    return {
        "ticket_id": ticket_id,
        "damage_kind": damage_kind,
        "current_order_status": order.status,
        "current_logistics_status": logistics_status,
        "current_policy_version": current_policy_version,
        "cases": cases,
        "draft": {
            "status": status,
            "current_facts": [
                f"当前订单 {order.id}：{order.status}",
                f"当前物流状态：{logistics_status or '未知'}",
                f"当前规则版本：{current_policy_version}",
            ],
            "missing_information": missing_information,
            "cited_cases": cases,
            "next_step": next_step,
            "limitation": (
                "案例只记录客服核验过的历史处理步骤，不证明退款或补发已经成功；"
                "现有物流政策没有破损专门自动处置规则，本草案不修改订单、不退款、不发送消息。"
            ),
        },
    }
