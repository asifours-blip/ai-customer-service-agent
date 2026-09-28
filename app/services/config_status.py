"""配置状态（只读）：管理员接口与启动日志共用。

只报「是否已设置」与主机名，绝不输出 key 的任何字符，也不输出 base_url 里可能夹带的用户名密码与路径。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.config import Settings, live_config_problems
from app.llm.client import ANSWER_MODE_MODEL, ANSWER_MODE_OFFLINE_ECHO
from app.rag.embedding import bge_status

# 挂在 uvicorn.error 下：uvicorn 只给自己的 logger 配置了 INFO 输出，应用 logger 的 INFO 会被丢弃；
# 这样启动状态与「Application startup complete」出现在同一输出里
logger = logging.getLogger("uvicorn.error.csagent")


def _host(url: str) -> str:
    try:
        return httpx.URL(url).host
    except httpx.InvalidURL:
        return ""


def config_status(settings: Settings) -> dict[str, Any]:
    live = not settings.no_paid_api
    return {
        "no_paid_api": settings.no_paid_api,
        "llm": {
            "provider": "deepseek" if live else "fake",
            # 离线回答是 FakeLLM 回显检索原文，不是模型生成
            "answer_mode": ANSWER_MODE_MODEL if live else ANSWER_MODE_OFFLINE_ECHO,
            "model": settings.model_name,
            "base_url_host": _host(settings.deepseek_base_url),
            "api_key_set": bool(settings.deepseek_api_key.get_secret_value().strip()),
            "problems": live_config_problems(settings),
            "connect_timeout_seconds": settings.llm_connect_timeout_seconds,
            "read_timeout_seconds": settings.llm_read_timeout_seconds,
            "max_retries": settings.llm_max_retries,
        },
        "embedding": {
            # 离线开关固定 FakeEmbedding（serving_retrieval）；configured_backend 是 EMBEDDING_BACKEND 的配置值
            "backend": "fake" if settings.no_paid_api else settings.embedding_backend,
            "configured_backend": settings.embedding_backend,
            "bge": bge_status(settings),
        },
    }


def log_startup_status(settings: Settings) -> None:
    logger.info("启动配置状态：%s", json.dumps(config_status(settings), ensure_ascii=False))
