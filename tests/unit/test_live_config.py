"""真实调用开关与配置状态（阶段 4，先写红测试）。

- NO_PAID_API=false 时启动即校验 key / base_url / 模型名 / JWT_SECRET，缺任何一项拒绝启动
- 配置状态只报「是否已设置」，绝不出现 key 的任何字符
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
FAKE_KEY = f"sk-config-canary-{uuid4().hex}"
GOOD_JWT = "k" * 40


def _live_env(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    """隔离 .env，只用环境变量驱动 Settings；进出都清 get_settings 缓存。"""
    import app.config as config

    config.get_settings.cache_clear()
    monkeypatch.setattr(config.Settings, "model_config", {"env_file": None, "extra": "ignore"})
    base = {"NO_PAID_API": "false", "JWT_SECRET": GOOD_JWT, "DEEPSEEK_API_KEY": FAKE_KEY,
            "DEEPSEEK_BASE_URL": "https://api.deepseek.com", "MODEL_NAME": "deepseek-v4-flash"}
    for key, value in {**base, **env}.items():
        monkeypatch.setenv(key, value)


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> object:
    import app.config as config

    yield
    config.get_settings.cache_clear()


@pytest.mark.parametrize(
    ("env", "needle"),
    [
        ({"DEEPSEEK_API_KEY": ""}, "DEEPSEEK_API_KEY"),
        ({"DEEPSEEK_API_KEY": "  "}, "DEEPSEEK_API_KEY"),
        ({"DEEPSEEK_BASE_URL": "not a url"}, "DEEPSEEK_BASE_URL"),
        ({"DEEPSEEK_BASE_URL": "ftp://api.deepseek.com"}, "DEEPSEEK_BASE_URL"),
        ({"DEEPSEEK_BASE_URL": "http://api.deepseek.com"}, "https"),
        ({"MODEL_NAME": ""}, "MODEL_NAME"),
        ({"JWT_SECRET": "dev-only-secret-change-me-0123456789abcdef"}, "JWT_SECRET"),
    ],
    ids=["no-key", "blank-key", "bad-url", "bad-scheme", "plain-http", "no-model", "default-jwt"],
)
def test_live_mode_missing_config_refuses_settings(monkeypatch: pytest.MonkeyPatch, env: dict[str, str],
                                                   needle: str) -> None:
    import app.config as config

    _live_env(monkeypatch, **env)
    with pytest.raises(RuntimeError, match=needle) as info:
        config.get_settings()
    assert FAKE_KEY not in str(info.value)


def test_live_mode_lists_every_problem_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.config as config

    _live_env(monkeypatch, DEEPSEEK_API_KEY="", MODEL_NAME="",
              JWT_SECRET="dev-only-secret-change-me-0123456789abcdef")
    with pytest.raises(RuntimeError) as info:
        config.get_settings()
    msg = str(info.value)
    assert "DEEPSEEK_API_KEY" in msg and "MODEL_NAME" in msg and "JWT_SECRET" in msg


def test_live_mode_loopback_http_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """本机替身 / 本地网关可以用 http；公网地址必须 https。"""
    import app.config as config

    _live_env(monkeypatch, DEEPSEEK_BASE_URL="http://127.0.0.1:9/v1")
    assert config.get_settings().deepseek_base_url == "http://127.0.0.1:9/v1"


def test_live_mode_complete_config_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.config as config

    _live_env(monkeypatch)
    settings = config.get_settings()
    assert settings.deepseek_api_key.get_secret_value() == FAKE_KEY
    assert FAKE_KEY not in repr(settings)  # 配置对象被打印也不泄露 key


def test_offline_mode_needs_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.config as config

    _live_env(monkeypatch, NO_PAID_API="true", DEEPSEEK_API_KEY="",
              JWT_SECRET="dev-only-secret-change-me-0123456789abcdef")
    assert config.get_settings().no_paid_api is True


def test_app_refuses_to_start_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """应用生命周期启动时校验配置：缺 key 直接启动失败（在连接数据库之前）。"""
    from app.main import create_app

    _live_env(monkeypatch, DEEPSEEK_API_KEY="")
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"), TestClient(create_app()):
        pass


def test_uvicorn_process_exits_without_key() -> None:
    """真实进程：uvicorn 启动失败退出（导入应用时数据库模块读取配置即失败），输出给出清晰原因。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DEEPSEEK_", "MODEL_NAME"))}
    env.update({"NO_PAID_API": "false", "JWT_SECRET": GOOD_JWT, "DEEPSEEK_API_KEY": "",
                "PYTHONIOENCODING": "utf-8"})
    proc = subprocess.run(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "0"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        timeout=60,
    )
    out = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    assert proc.returncode != 0, out
    assert "拒绝启动" in out and "DEEPSEEK_API_KEY" in out
    assert "Application startup complete" not in out and "Uvicorn running" not in out


# ---------------- 配置状态 ----------------


def test_config_status_reports_presence_not_value(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    import app.config as config
    from app.services.config_status import config_status

    _live_env(monkeypatch, DEEPSEEK_BASE_URL="https://user:pw@llm.example.com:8443/v1")
    status = config_status(config.get_settings())
    text = json.dumps(status, ensure_ascii=False)
    assert FAKE_KEY not in text and FAKE_KEY[:12] not in text
    assert "pw" not in status["llm"]["base_url_host"]
    assert status["no_paid_api"] is False
    assert status["llm"]["api_key_set"] is True
    assert status["llm"]["base_url_host"] == "llm.example.com"
    assert status["llm"]["model"] == "deepseek-v4-flash"
    assert status["llm"]["answer_mode"] == "MODEL"
    assert status["llm"]["problems"] == []
    assert status["embedding"]["backend"] == "fake"
    assert "ready" in status["embedding"]["bge"]


def test_config_status_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.config as config
    from app.services.config_status import config_status

    _live_env(monkeypatch, NO_PAID_API="true", DEEPSEEK_API_KEY="", EMBEDDING_BACKEND="bge")
    status = config_status(config.get_settings())
    assert status["no_paid_api"] is True
    assert status["llm"]["api_key_set"] is False
    assert status["llm"]["answer_mode"] == "OFFLINE_ECHO"
    # 离线开关固定 FakeEmbedding：报告实际生效的后端，也保留配置值
    assert status["embedding"]["backend"] == "fake"
    assert status["embedding"]["configured_backend"] == "bge"


def test_startup_log_has_status_without_key(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    import app.config as config
    from app.services.config_status import log_startup_status

    _live_env(monkeypatch)
    caplog.set_level(logging.INFO)
    log_startup_status(config.get_settings())
    assert "api_key_set" in caplog.text and "true" in caplog.text.lower()
    assert FAKE_KEY not in caplog.text
