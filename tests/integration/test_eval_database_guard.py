"""评测库守卫（真实 PostgreSQL）：指向应用库 / 未配置时拒绝运行，且一行数据都不删。"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from sqlalchemy import text

from tests.conftest import ROOT, TEST_DATABASE_URL

pytestmark = pytest.mark.integration


def _row_counts(db_engine) -> dict[str, int]:  # noqa: ANN001
    with db_engine.connect() as conn:
        tables = conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version'")
        ).scalars()
        return {t: int(conn.execute(text(f'SELECT count(*) FROM "{t}"')).scalar() or 0) for t in tables}


def _run_eval(extra_env: dict[str, str | None]) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL, "NO_PAID_API": "true", "EMBEDDING_BACKEND": "fake"}
    for key, value in extra_env.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(
        [sys.executable, "scripts/run_eval.py"], cwd=ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=120,
    )


@pytest.mark.parametrize(
    "eval_url,reason",
    [
        (None, "未设置 EVAL_DATABASE_URL"),
        (TEST_DATABASE_URL, "同一个库"),  # 指向应用库
        (TEST_DATABASE_URL.replace("localhost", "127.0.0.1"), "同一个库"),
    ],
    ids=["unset", "app-db", "app-db-other-spelling"],
)
def test_run_eval_refuses_and_deletes_nothing(db, db_engine, eval_url: str | None, reason: str) -> None:  # noqa: ANN001
    with db_engine.begin() as conn:  # 一条业务数据：若被清空就能看出来
        conn.execute(text("INSERT INTO conversations (user_id, session_id, title, created_at, updated_at) "
                          "VALUES ('U001', 'must-survive', 'x', now(), now())"))
    before = _row_counts(db_engine)
    proc = _run_eval({"EVAL_DATABASE_URL": eval_url})
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert reason in proc.stdout
    assert _row_counts(db_engine) == before


def test_reset_environment_itself_refuses_app_database(db, db_engine, monkeypatch) -> None:  # noqa: ANN001
    """纵深防御：绕过 CLI 直接调用重置函数，也不能清空应用库。"""
    from eval.runner import reset_environment
    from eval.safety import EvalDatabaseRefused

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("EVAL_DATABASE_URL", TEST_DATABASE_URL)
    before = _row_counts(db_engine)
    with db() as s, pytest.raises(EvalDatabaseRefused):
        reset_environment(s)
    assert _row_counts(db_engine) == before


def test_reset_environment_refuses_session_not_bound_to_eval_database(db, db_engine, monkeypatch) -> None:  # noqa: ANN001
    """配置本身合法，但传进来的会话连的是别的库（应用库）：同样拒绝。"""
    from eval.runner import reset_environment
    from eval.safety import EvalDatabaseRefused

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL.rsplit("/", 1)[0] + "/agent_cs")
    monkeypatch.setenv("EVAL_DATABASE_URL", TEST_DATABASE_URL.rsplit("/", 1)[0] + "/agent_cs_eval")
    before = _row_counts(db_engine)
    with db() as s, pytest.raises(EvalDatabaseRefused, match="不是 EVAL_DATABASE_URL"):
        reset_environment(s)
    assert _row_counts(db_engine) == before
