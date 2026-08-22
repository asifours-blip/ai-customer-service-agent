"""权限判定：纯单测。"""

from __future__ import annotations

import pytest

from app.services.errors import PermissionDeniedError
from app.services.permission import ensure_owner, ensure_role


def test_owner_match_passes() -> None:
    ensure_owner("U001", "U001")  # 不抛


def test_owner_mismatch_denied() -> None:
    with pytest.raises(PermissionDeniedError):
        ensure_owner("U002", "U001")


def test_role_check() -> None:
    ensure_role("SUPPORT", "SUPPORT")
    with pytest.raises(PermissionDeniedError):
        ensure_role("CUSTOMER", "SUPPORT")
