"""POST /api/auth/login：用户名口令 → 简化 JWT。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.user import User
from app.schemas.api import LoginRequest, TokenResponse
from app.security.auth import create_access_token, verify_password
from app.services.database import get_db
from app.services.errors import AuthenticationError

router = APIRouter()


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.scalar(select(User).where(User.username == body.username))
    if user is None or not verify_password(body.password, user.password_hash):
        # 统一错误信息，不区分"用户不存在/口令错误"
        raise AuthenticationError("用户名或口令错误")
    return TokenResponse(
        access_token=create_access_token(user.id, user.role),
        user_id=user.id,
        role=user.role,
    )
