"""集成测试夹具：真实 Docker PostgreSQL（审核修订③：SQLite 不做 PG 替身）。

前置：docker compose up -d db
- 会话级：建库（若缺）→ alembic upgrade head
- 函数级：清空全部表 → 重跑种子（每个测试拿到确定性初始数据）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

TEST_DB_NAME = __import__("os").environ.get("AGENT_CS_TEST_DB", "agent_cs_test")
TEST_DATABASE_URL = __import__("os").environ.get(
    "TEST_DATABASE_URL", f"postgresql+psycopg://app:app@localhost:5432/{TEST_DB_NAME}"
)

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def db_engine():
    admin = create_engine("postgresql+psycopg://app:app@localhost:5432/postgres", pool_pre_ping=True)
    with admin.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": TEST_DB_NAME}
        ).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    admin.dispose()

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(cfg, "head")

    engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.fixture()
def db(db_engine):
    """函数级：清表 + 种子（确定性初始数据）。返回测试库会话工厂。"""
    from app.models import Base
    from scripts.seed_db import seed

    with db_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())

    factory = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)
    import app.services.database as database_module

    original = database_module.SessionLocal
    database_module.SessionLocal = factory
    try:
        with factory():
            seed.__globals__["SessionLocal"] = factory
            # seed 内部使用 SessionLocal；已替换为测试工厂
            seed()
        yield factory
    finally:
        database_module.SessionLocal = original


@pytest.fixture()
def client(db):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def login(client: TestClient, username: str, password: str = "demo123") -> str:
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def auth_headers(client: TestClient, username: str, password: str = "demo123") -> dict[str, str]:
    token = login(client, username, password)
    return {"Authorization": f"Bearer {token}"}
