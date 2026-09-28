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
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402


@pytest.fixture(scope="session")
def db_engine():
    # 管理连接（建库用）从 TEST_DATABASE_URL 派生，仅替换库名——
    # 曾硬编码 localhost:5432，测试库不在该地址时集成测试全灭（CI/Linux 容器复现）
    admin_url = make_url(TEST_DATABASE_URL).set(database="postgres")
    admin = create_engine(admin_url, pool_pre_ping=True)
    with admin.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        target_db = make_url(TEST_DATABASE_URL).database or TEST_DB_NAME
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": target_db}
        ).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{target_db}"'))
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
    """函数级：清表 → 种子 → 知识库初始化（knowledge_base/ 导入为 v1 并生效，Fake）。返回测试库会话工厂。"""
    from app.kb.service import Retrieval, bootstrap_from_directory
    from app.kb.smoke import load_smoke_config
    from app.models import Base
    from app.rag import FakeEmbedding
    from scripts.seed_db import seed

    with db_engine.begin() as conn:
        # 知识库表重置自增序列：每个用例的初始版本都是 v1，断言与失败信息更易读
        conn.execute(text("TRUNCATE TABLE kb_audit_log, kb_chunks, kb_documents, kb_versions RESTART IDENTITY"))
        for table in reversed(Base.metadata.sorted_tables):
            if table.name == "ticket_events":
                # 只追加表：行级触发器拒绝 DELETE；TRUNCATE 不触发行级触发器，仅测试清库使用
                conn.execute(text("TRUNCATE TABLE ticket_events"))
            else:
                conn.execute(table.delete())

    factory = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)
    import app.services.database as database_module

    original = database_module.SessionLocal
    database_module.SessionLocal = factory
    try:
        seed.__globals__["SessionLocal"] = factory
        seed()
        retrieval = Retrieval(embedder=FakeEmbedding(), threshold=0.22, smoke=load_smoke_config())
        result, _ = bootstrap_from_directory(factory, ROOT / "knowledge_base", retrieval)
        assert result == "ACTIVATED", result
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
