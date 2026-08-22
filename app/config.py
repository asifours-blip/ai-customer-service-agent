"""集中配置：所有可调参数与护栏常量的唯一来源。

红线：DEEPSEEK_API_KEY 只从环境变量/.env 注入，绝不打印、绝不写入日志或报告。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- 数据库 ---
    database_url: str = "postgresql+psycopg://app:app@localhost:5432/agent_cs"

    # --- LLM（OpenAI-compatible；真实调用仅 Phase 7 live）---
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    model_name: str = "deepseek-v4-flash"
    judge_model_name: str = "deepseek-v4-pro"

    # --- 离线优先开关：Phase 0~6 必须保持 true ---
    no_paid_api: bool = True

    # --- Agent / 工具护栏（补丁 §9 / 规格 §14/§31）---
    max_agent_steps: int = 8
    max_tool_retries: int = 2  # 仅 READ_ONLY_TOOL 允许自动重试；SIDE_EFFECT_TOOL 走幂等
    tool_timeout_seconds: float = 10.0
    max_output_tokens: int = 1500
    max_context_tokens: int = 12000

    # --- Embedding：fake（CI/离线）| bge（本地 BAAI/bge-small-zh-v1.5）---
    embedding_backend: str = "fake"
    bge_model_name: str = "BAAI/bge-small-zh-v1.5"

    # --- Cost Guard ---
    max_single_live_eval_cost_usd: float = 1.00  # preflight 预检，--force 可越
    hard_eval_cost_limit_usd: float = 2.00  # 硬闸，任何情况不可越

    # --- 简化 JWT ---
    jwt_secret: str = "dev-only-secret-change-me-0123456789abcdef"  # ≥32 字节（RFC 7518）
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 720


@lru_cache
def get_settings() -> Settings:
    return Settings()
