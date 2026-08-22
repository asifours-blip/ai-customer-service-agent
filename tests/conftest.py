"""共享测试夹具。

顶部：在任何 app 导入前固定环境（测试库 URL + 离线开关）。
集成夹具（db/client，真实 Docker PostgreSQL）只被显式请求时才连接——
CI 离线 job 选择 `-m "not integration"` 不会触发任何数据库连接。

前置（仅集成测试）：docker compose up -d db
- 会话级：建库（若缺）→ alembic upgrade head
- 函数级：清空全部表 → 种子 + 知识库摄取（FakeEmbedding，确定性）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

TEST_DB_NAME = os.environ.get("AGENT_CS_TEST_DB", "agent_cs_test")
os.environ.setdefault("DATABASE_URL", f"postgresql+psycopg://app:app@localhost:5432/{TEST_DB_NAME}")
os.environ.setdefault("NO_PAID_API", "true")
os.environ.setdefault("EMBEDDING_BACKEND", "fake")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", f"postgresql+psycopg://app:app@localhost:5432/{TEST_DB_NAME}"
)

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402


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
    """函数级：清表 → 种子 → 知识库摄取（Fake）。返回测试库会话工厂。"""
    from app.models import Base
    from app.rag import FakeEmbedding, chunk_corpus, load_corpus, rebuild_index
    from scripts.seed_db import seed

    with db_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())

    factory = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)
    import app.services.database as database_module

    original = database_module.SessionLocal
    database_module.SessionLocal = factory
    try:
        with factory() as s:
            seed.__globals__["SessionLocal"] = factory
            seed()
            corpus = load_corpus(ROOT / "knowledge_base")
            rebuild_index(s, chunk_corpus(corpus), FakeEmbedding())
        yield factory
    finally:
        database_module.SessionLocal = original


@pytest.fixture()
def client(db):
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
