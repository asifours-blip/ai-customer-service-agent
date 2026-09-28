"""回答反馈服务：客户提交「有帮助/没帮助」→ 管理员审核队列 → 转评测用例 / 驳回。

- submit_feedback：只能对自己会话里的 assistant 回答提交，(message_id, user_id) 唯一，不支持修改
- 审核队列条目一次性带全问题、回答、引用、知识库版本、trace，供人工判断是否值得转成评测用例
- 审核动作（CONVERT / REJECT）写进只追加的 feedback_review_audit（DB 触发器拒绝 UPDATE/DELETE）
- 转评测用例：写进 eval/datasets/converted/<version>.jsonl，绝不触碰固定 110 条数据集
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.kb.service import resolve_citations
from app.models.base import utcnow
from app.models.conversation import Conversation, Message
from app.models.feedback import (
    FEEDBACK_STATUS_CONVERTED,
    FEEDBACK_STATUS_PENDING,
    FEEDBACK_STATUS_REJECTED,
    REVIEW_ACTION_CONVERT,
    REVIEW_ACTION_REJECT,
    AnswerFeedback,
    FeedbackReviewAudit,
)
from app.models.trace import AgentTrace
from app.services.errors import DuplicateError, InvalidStateError, NotFoundError, PermissionDeniedError

_FEEDBACK_UNIQUE_CONSTRAINT = "uq_answer_feedback_message_user"


def _is_unique_violation(exc: IntegrityError, constraint_name: str) -> bool:
    original = exc.orig
    return (
        getattr(original, "sqlstate", None) == "23505"
        and getattr(getattr(original, "diag", None), "constraint_name", None) == constraint_name
    )


def submit_feedback(db: Session, message_id: int, user_id: str, helpful: bool, note: str) -> AnswerFeedback:
    """客户对自己会话里的一条 assistant 回答提交反馈；同一用户对同一条回答只能提交一次。"""
    message = db.get(Message, message_id)
    if message is None or message.role != "assistant":
        raise NotFoundError(f"回答消息不存在: {message_id}")
    conversation = db.get(Conversation, message.conversation_id)
    if conversation is None or conversation.user_id != user_id:
        raise PermissionDeniedError("无权对该回答提交反馈")
    feedback = AnswerFeedback(message_id=message_id, user_id=user_id, helpful=helpful, note=note or None)
    try:
        db.add(feedback)
        db.flush()  # 唯一约束兜底：并发重复提交也只成功一条
    except IntegrityError as exc:
        db.rollback()
        if _is_unique_violation(exc, _FEEDBACK_UNIQUE_CONSTRAINT):
            raise DuplicateError("你已经对这条回答提交过反馈了") from exc
        raise
    db.commit()
    db.refresh(feedback)
    return feedback


def _queue_row(db: Session, feedback: AnswerFeedback) -> dict[str, Any]:
    message = db.get(Message, feedback.message_id)
    question: str | None = None
    trace: AgentTrace | None = None
    if message is not None:
        prior = db.scalar(
            select(Message)
            .where(
                Message.conversation_id == message.conversation_id,
                Message.id < message.id,
                Message.role == "user",
            )
            .order_by(Message.id.desc())
            .limit(1)
        )
        question = prior.content if prior is not None else None
        if message.trace_id:
            trace = db.scalar(select(AgentTrace).where(AgentTrace.trace_id == message.trace_id))
    citations: list[dict[str, Any]] = []
    kb_version_ids: list[int] = []
    if trace is not None and trace.retrieved_documents:
        citations = resolve_citations(db, list(trace.retrieved_documents))
        kb_version_ids = sorted({c["version_id"] for c in citations if c.get("version_id") is not None})
    return {
        "feedback": feedback,
        "question": question,
        "answer": message.content if message is not None else None,
        "trace_id": message.trace_id if message is not None else None,
        "route": trace.route if trace is not None else None,
        "citations": citations,
        "kb_version_ids": kb_version_ids,
    }


def list_review_queue(db: Session, *, status: str = FEEDBACK_STATUS_PENDING) -> list[dict[str, Any]]:
    """管理员审核队列：问题、回答、引用、知识库版本、trace 一次性给全。"""
    rows = db.scalars(
        select(AnswerFeedback).where(AnswerFeedback.review_status == status).order_by(AnswerFeedback.created_at)
    ).all()
    return [_queue_row(db, fb) for fb in rows]


def get_queue_item(db: Session, feedback_id: int) -> dict[str, Any]:
    feedback = db.get(AnswerFeedback, feedback_id)
    if feedback is None:
        raise NotFoundError(f"反馈不存在: {feedback_id}")
    return _queue_row(db, feedback)


def _require_pending(feedback: AnswerFeedback) -> None:
    if feedback.review_status != FEEDBACK_STATUS_PENDING:
        raise InvalidStateError(f"反馈 {feedback.id} 已审核（{feedback.review_status}），不能重复审核")


def reject_feedback(db: Session, feedback_id: int, actor_id: str) -> AnswerFeedback:
    feedback = db.get(AnswerFeedback, feedback_id)
    if feedback is None:
        raise NotFoundError(f"反馈不存在: {feedback_id}")
    _require_pending(feedback)
    feedback.review_status = FEEDBACK_STATUS_REJECTED
    feedback.reviewed_by = actor_id
    feedback.reviewed_at = utcnow()
    db.add(FeedbackReviewAudit(feedback_id=feedback.id, action=REVIEW_ACTION_REJECT, actor=actor_id, detail=None))
    db.commit()
    db.refresh(feedback)
    return feedback


def convert_feedback_to_eval_case(
    db: Session,
    feedback_id: int,
    actor_id: str,
    *,
    expected_outcome: str,
    expected_document: str | None,
    category: str,
    note: str,
) -> tuple[AnswerFeedback, dict[str, Any]]:
    """把一条待审反馈转成评测用例，写入独立的带版本号数据文件（不改动固定 110 条数据集）。"""
    from eval.converted import append_new_case

    feedback = db.get(AnswerFeedback, feedback_id)
    if feedback is None:
        raise NotFoundError(f"反馈不存在: {feedback_id}")
    _require_pending(feedback)
    item = _queue_row(db, feedback)
    if not item["question"]:
        raise InvalidStateError(f"反馈 {feedback_id} 找不到对应的用户提问，无法转成评测用例")

    case: dict[str, Any] = {
        "category": category,
        "user_id": feedback.user_id,
        "input": item["question"],
        "expected_outcome": expected_outcome,
        "source": {
            "feedback_id": feedback.id,
            "message_id": feedback.message_id,
            "trace_id": item["trace_id"],
            "converted_by": actor_id,
        },
    }
    if expected_document:
        case["expected_document"] = expected_document
    if item["route"]:
        case["expected_intent"] = item["route"]

    version = append_new_case(case)
    case_id = case["case_id"]

    feedback.review_status = FEEDBACK_STATUS_CONVERTED
    feedback.reviewed_by = actor_id
    feedback.reviewed_at = utcnow()
    db.add(
        FeedbackReviewAudit(
            feedback_id=feedback.id,
            action=REVIEW_ACTION_CONVERT,
            actor=actor_id,
            detail={"case_id": case_id, "version": version, "note": note or None},
        )
    )
    db.commit()
    db.refresh(feedback)
    return feedback, case
