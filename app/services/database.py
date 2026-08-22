"""数据库引擎与会话（SQLAlchemy 2.0 sync + psycopg3）。

测试分层（审核修订③）：业务逻辑不直接依赖本模块——
纯单测用 Mock/Fake Repository；仅集成测试与 API 层使用真实 Session。
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


def _engine_for(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True)


settings = get_settings()
engine = _engine_for(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：请求级 Session。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
