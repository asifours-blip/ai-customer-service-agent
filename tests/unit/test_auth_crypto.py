"""认证加密：哈希往返 / 错误口令 / JWT 签发-校验-过期-篡改。纯单测。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.security.auth import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from app.services.errors import AuthenticationError


def test_password_roundtrip() -> None:
    stored = hash_password("demo123")
    assert stored.startswith("pbkdf2_sha256$")
    assert verify_password("demo123", stored)
    assert not verify_password("wrong", stored)


def test_password_salt_unique() -> None:
    assert hash_password("demo123") != hash_password("demo123")  # 随机盐


def test_password_malformed_hash() -> None:
    assert not verify_password("demo123", "not-a-hash")


def test_token_roundtrip() -> None:
    token = create_access_token("U001", "CUSTOMER")
    payload = decode_access_token(token)
    assert payload["sub"] == "U001"
    assert payload["role"] == "CUSTOMER"


def test_token_expired() -> None:
    from app.config import get_settings

    settings = get_settings()
    now = datetime.now(tz=UTC)
    expired = jwt.encode(
        {"sub": "U001", "role": "CUSTOMER", "exp": int((now - timedelta(minutes=1)).timestamp())},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(AuthenticationError):
        decode_access_token(expired)


def test_token_tampered() -> None:
    token = create_access_token("U001", "CUSTOMER")
    with pytest.raises(AuthenticationError):
        decode_access_token(token + "x")


def test_token_garbage() -> None:
    with pytest.raises(AuthenticationError):
        decode_access_token("garbage.token.value")


def test_token_incomplete_payload() -> None:
    from app.config import get_settings

    settings = get_settings()
    bad = jwt.encode({"sub": "U001"}, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    with pytest.raises(AuthenticationError, match="载荷不完整"):
        decode_access_token(bad)
