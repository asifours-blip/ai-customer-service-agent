"""系统配置状态（只读，仅 KB_ADMIN）。

模型名、base_url 主机、key 是否已设置（不显示任何字符）、NO_PAID_API、embedding 后端、BGE 是否就绪。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.config import get_settings
from app.models.user import User
from app.security.dependencies import get_kb_admin_user
from app.services.config_status import config_status

router = APIRouter()


@router.get("/config")
def get_config_status(_admin: User = Depends(get_kb_admin_user)) -> dict[str, Any]:
    return config_status(get_settings())
