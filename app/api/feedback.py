"""回答反馈 API。

- POST /api/feedback/{message_id}            客户对一条 assistant 回答提交反馈（有帮助/没帮助）
- GET  /api/feedback/admin/queue             管理员审核队列（问题/回答/引用/知识库版本/trace）
- POST /api/feedback/admin/{id}/convert      审核通过：转成评测用例（写独立版本数据文件）
- POST /api/feedback/admin/{id}/reject       审核驳回
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.models.feedback import FEEDBACK_STATUS_PENDING
from app.models.user import User
from app.schemas.api import (
    AnswerFeedbackCreate,
    AnswerFeedbackOut,
    FeedbackConvertRequest,
    FeedbackQueueItemOut,
)
from app.security.dependencies import get_customer_user, get_support_user
from app.services import feedback as feedback_service
from app.services.database import get_db

router = APIRouter()


@router.post("/{message_id}", response_model=AnswerFeedbackOut)
def create_feedback(
    message_id: int,
    body: AnswerFeedbackCreate,
    user: User = Depends(get_customer_user),
    db: Session = Depends(get_db),
) -> AnswerFeedbackOut:
    fb = feedback_service.submit_feedback(db, message_id, user.id, body.helpful, body.note)
    return AnswerFeedbackOut.model_validate(fb)


@router.get("/admin/queue", response_model=list[FeedbackQueueItemOut])
def review_queue(
    status: str = FEEDBACK_STATUS_PENDING,
    support: User = Depends(get_support_user),
    db: Session = Depends(get_db),
) -> list[FeedbackQueueItemOut]:
    rows = feedback_service.list_review_queue(db, status=status)
    return [FeedbackQueueItemOut(**row) for row in rows]


@router.post("/admin/{feedback_id}/convert", response_model=AnswerFeedbackOut)
def convert_to_eval_case(
    feedback_id: int,
    body: FeedbackConvertRequest,
    support: User = Depends(get_support_user),
    db: Session = Depends(get_db),
) -> AnswerFeedbackOut:
    fb, _case = feedback_service.convert_feedback_to_eval_case(
        db,
        feedback_id,
        support.id,
        expected_outcome=body.expected_outcome,
        expected_document=body.expected_document,
        category=body.category,
        note=body.note,
    )
    return AnswerFeedbackOut.model_validate(fb)


@router.post("/admin/{feedback_id}/reject", response_model=AnswerFeedbackOut)
def reject(
    feedback_id: int,
    support: User = Depends(get_support_user),
    db: Session = Depends(get_db),
) -> AnswerFeedbackOut:
    fb = feedback_service.reject_feedback(db, feedback_id, support.id)
    return AnswerFeedbackOut.model_validate(fb)
