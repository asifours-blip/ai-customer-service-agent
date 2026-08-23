"""P1 守卫验证（test-first，先于实现）。

P1-A：live 模式（NO_PAID_API=false）使用默认 JWT 密钥必须拒绝启动。
P1-B：BGE embedding 客户端必须跨调用复用（避免每请求重载 SentenceTransformer）。
对应审计 16 号：config.py:48 / chat.py:29→embedding.py:93。
"""

from __future__ import annotations

import pytest

BGE_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_JWT_SECRET = "dev-only-secret-change-me-0123456789abcdef"


def _settings_env(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    """隔离 .env，仅用环境变量驱动 Settings；进出都清 get_settings 的 lru 缓存。"""
    import app.config as config

    config.get_settings.cache_clear()
    monkeypatch.setattr(config.Settings, "model_config", {"env_file": None, "extra": "ignore"})
    for key, value in env.items():
        monkeypatch.setenv(key.upper(), value)


# ---------- P1-A：默认 JWT 密钥守卫 ----------

def test_p1a_live_mode_with_default_jwt_secret_must_fail_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """NO_PAID_API=false 且 jwt_secret 为默认值 → 启动即抛错（修复前应 FAIL：当前无守卫）。"""
    import app.config as config

    monkeypatch.setenv("NO_PAID_API", "false")
    monkeypatch.setenv("JWT_SECRET", DEFAULT_JWT_SECRET)
    _settings_env(monkeypatch)
    try:
        with pytest.raises(RuntimeError, match="JWT_SECRET"):
            config.get_settings()
    finally:
        config.get_settings.cache_clear()


def test_p1a_live_mode_with_custom_secret_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.config as config

    monkeypatch.setenv("NO_PAID_API", "false")
    monkeypatch.setenv("JWT_SECRET", "a" * 32)
    _settings_env(monkeypatch)
    try:
        assert config.get_settings().jwt_secret == "a" * 32
    finally:
        config.get_settings.cache_clear()


def test_p1a_offline_mode_default_secret_still_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """离线/CI（NO_PAID_API=true）维持现状可用——守卫只针对 live 模式。"""
    import app.config as config

    monkeypatch.setenv("NO_PAID_API", "true")
    monkeypatch.setenv("JWT_SECRET", DEFAULT_JWT_SECRET)
    _settings_env(monkeypatch)
    try:
        assert config.get_settings() is not None
    finally:
        config.get_settings.cache_clear()


# ---------- P1-B：embedding 客户端缓存 ----------

def test_p1b_bge_client_created_once_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一 (backend, model) 两次 get_embedding_client 必须返回同一实例（修复前应 FAIL：每次新建）。"""
    from app.rag import embedding as emb

    calls = {"n": 0}

    def counting_init(self: object, model_name: str = BGE_MODEL) -> None:
        # 故意不调用真 __init__：本测试只验证实例化次数与复用，不加载 torch 模型
        calls["n"] += 1
        self._model_name = model_name

    monkeypatch.setattr(emb.LocalBGEEmbedding, "__init__", counting_init)
    if hasattr(emb.get_embedding_client, "cache_clear"):
        emb.get_embedding_client.cache_clear()
    try:
        a = emb.get_embedding_client("bge", BGE_MODEL)
        b = emb.get_embedding_client("bge", BGE_MODEL)
        assert calls["n"] == 1, f"LocalBGEEmbedding 被实例化了 {calls['n']} 次，模型会被重复加载"
        assert a is b
    finally:
        if hasattr(emb.get_embedding_client, "cache_clear"):
            emb.get_embedding_client.cache_clear()
