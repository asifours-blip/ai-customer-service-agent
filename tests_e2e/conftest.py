"""真实浏览器端到端测试夹具：uvicorn 子进程跑真实应用 + 真实 PostgreSQL 测试库 + Playwright Chromium。

不在默认 testpaths 内（pyproject testpaths=["tests"]），CI 快速 job 不会收集。运行方式见 README.md「端到端测试」：
必须同时设置 DATABASE_URL 与 TEST_DATABASE_URL 且指向同一个测试库——每个用例都会清空并重建该库的数据。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_db_url = os.environ.get("DATABASE_URL")
if not _db_url or _db_url != os.environ.get("TEST_DATABASE_URL"):
    pytest.exit(
        "E2E 会清空数据库：请同时设置 DATABASE_URL 与 TEST_DATABASE_URL，且两者指向同一个测试库",
        returncode=4,
    )

import httpx  # noqa: E402

from tests.conftest import *  # noqa: F401,F403,E402  （db_engine / db：迁移 + 每用例清库重建种子）
from tests.conftest import TEST_DATABASE_URL  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def run_uvicorn(extra_env: dict[str, str]) -> Iterator[tuple[str, Path]]:
    """在已迁移的测试库上启动真实 uvicorn 子进程，返回 (地址, 日志文件)；退出时终止进程。"""
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL, **extra_env}
    log = tempfile.NamedTemporaryFile(prefix="csagent-e2e-", suffix=".log", delete=False)  # noqa: SIM115
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                raise RuntimeError(f"uvicorn 启动失败，日志见 {log.name}")
            try:
                if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(f"uvicorn 30s 内未就绪，日志见 {log.name}")
            time.sleep(0.3)
        yield base, Path(log.name)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        log.close()


@pytest.fixture(scope="session")
def live_server(db_engine) -> Iterator[str]:  # noqa: ANN001
    """会话级：在已迁移的测试库上启动真实 uvicorn（离线开关：FakeLLM + FakeEmbedding）。"""
    with run_uvicorn({"NO_PAID_API": "true", "EMBEDDING_BACKEND": "fake"}) as (base, _log):
        yield base


@pytest.fixture()
def app_url(live_server: str, db) -> str:  # noqa: ANN001
    """函数级：先清库重建种子（db 夹具），再返回服务地址。"""
    return live_server
