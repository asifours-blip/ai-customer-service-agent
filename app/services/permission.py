"""权限判定（纯函数，零 DB 依赖）。

核心原则（规格 §2.2）：LLM 可以决定"需要查询订单"，
但不能决定"用户是否有权限查看这个订单"——权限永远由这里（后端代码）判定。
"""

from __future__ import annotations

from app.services.errors import PermissionDeniedError

ROLE_CUSTOMER = "CUSTOMER"
ROLE_SUPPORT = "SUPPORT"


def ensure_owner(resource_owner_id: str, current_user_id: str) -> None:
    """资源级授权（防 IDOR）：不匹配即拒，不给"是否存在"的额外信息。"""
    if resource_owner_id != current_user_id:
        raise PermissionDeniedError("用户无权访问该资源")


def ensure_role(role: str, required: str) -> None:
    if role != required:
        raise PermissionDeniedError(f"该操作仅限 {required} 角色")
