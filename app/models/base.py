from datetime import UTC, datetime

from sqlalchemy.orm import DeclarativeBase


def utcnow() -> datetime:
    """统一 UTC 时间戳来源。"""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """全局声明基类。"""
