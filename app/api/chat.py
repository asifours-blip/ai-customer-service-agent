"""POST /api/chat：Phase 1 占位（Phase 4 由 LangGraph Agent 实装）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.models.user import User
from app.schemas.api import ChatRequest
from app.security.dependencies import get_current_user

router = APIRouter()

PLACEHOLDER = "chat 接口将在 Phase 4（Agent 工作流）实装"


@router.post("", status_code=501)
def chat(
    body: ChatRequest,
    user: User = Depends(get_current_user),
) -> dict[str, str]:
    _ = (body, user)
    return {"detail": PLACEHOLDER}
