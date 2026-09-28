"""评测库守卫（纯函数，离线）：评测会清空业务表，只允许指向独立的 *_eval / *_test 库。"""

from __future__ import annotations

import pytest

from eval.safety import EvalDatabaseRefused, resolve_eval_database

APP = "postgresql+psycopg://app:app@localhost:5432/agent_cs"


def refused(env: dict[str, str]) -> str:
    with pytest.raises(EvalDatabaseRefused) as exc:
        resolve_eval_database(env)
    return str(exc.value)


def test_accepts_separate_eval_database() -> None:
    url = "postgresql+psycopg://app:app@localhost:5432/agent_cs_eval"
    assert resolve_eval_database({"DATABASE_URL": APP, "EVAL_DATABASE_URL": url}) == url


def test_accepts_test_suffix_on_other_host() -> None:
    url = "postgresql+psycopg://app:app@db.internal:6543/agent_cs_test"
    assert resolve_eval_database({"DATABASE_URL": APP, "EVAL_DATABASE_URL": url}) == url


def test_refuses_when_unset() -> None:
    assert "EVAL_DATABASE_URL" in refused({"DATABASE_URL": APP})
    assert "EVAL_DATABASE_URL" in refused({"DATABASE_URL": APP, "EVAL_DATABASE_URL": "  "})


@pytest.mark.parametrize(
    "eval_url",
    [
        "postgresql+psycopg://app:app@localhost:5432/agent_cs_test",
        # 规范化：localhost≡127.0.0.1、默认端口 5432、忽略用户名
        "postgresql+psycopg://other:pw@127.0.0.1/agent_cs_test",
        "postgresql://app:app@LOCALHOST:5432/agent_cs_test",  # 驱动与主机大小写不同，仍是同一个库
    ],
)
def test_refuses_same_database_as_app(eval_url: str) -> None:
    app_url = "postgresql+psycopg://app:app@localhost:5432/agent_cs_test"
    assert "同一个库" in refused({"DATABASE_URL": app_url, "EVAL_DATABASE_URL": eval_url})


@pytest.mark.parametrize("name", ["agent_cs", "agent_cs_prod", "eval_agent_cs", "agent_cs_evaluation"])
def test_refuses_database_name_without_eval_or_test_suffix(name: str) -> None:
    url = f"postgresql+psycopg://app:app@localhost:5432/{name}"
    assert "_eval" in refused({"DATABASE_URL": APP.replace("agent_cs", "agent_cs_main"), "EVAL_DATABASE_URL": url})


def test_uses_default_app_url_when_database_url_unset() -> None:
    # 应用未显式设置 DATABASE_URL 时按配置默认值比较（默认库 agent_cs 不以 _eval/_test 结尾，同样要拦）
    url = "postgresql+psycopg://app:app@localhost:5432/agent_cs_eval"
    assert resolve_eval_database({"EVAL_DATABASE_URL": url}) == url
