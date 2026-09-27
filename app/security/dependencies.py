"""FastAPI 认证依赖：Bearer Token → 当前用户；角色依赖在每个接口独立生效（前端跳转只是体验层）。"""

from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.models.user import User
from app.security.auth import decode_access_token
from app.services.database import get_db
from app.services.errors import AuthenticationError, PermissionDeniedError
from app.services.permission import ROLE_CUSTOMER, ROLE_SUPPORT


def _extract_bearer(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthenticationError("缺少 Bearer 访问令牌")
    return token


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    payload = decode_access_token(_extract_bearer(request))
    user = db.get(User, payload["sub"])
    if user is None:
        raise AuthenticationError("用户不存在或已失效")
    return user


def get_support_user(user: User = Depends(get_current_user)) -> User:
    if user.role != ROLE_SUPPORT:
        raise PermissionDeniedError("该操作仅限 SUPPORT 角色")
    return user


def get_customer_user(user: User = Depends(get_current_user)) -> User:
    """客户工单接口仅限 CUSTOMER：客服不能冒充客户回复、提交反馈或建单。"""
    if user.role != ROLE_CUSTOMER:
        raise PermissionDeniedError("该操作仅限 CUSTOMER 角色")
    return user
