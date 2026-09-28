"""集中配置：所有可调参数与护栏常量的唯一来源。

红线：DEEPSEEK_API_KEY 只从环境变量/.env 注入，绝不打印、绝不写入日志或报告
（类型为 SecretStr：配置对象被打印、被 repr 时也只显示 **********）。
"""

from __future__ import annotations

from functools import lru_cache

import httpx
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_JWT_SECRET = "dev-only-secret-change-me-0123456789abcdef"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- 数据库 ---
    database_url: str = "postgresql+psycopg://app:app@localhost:5432/agent_cs"

    # --- LLM（OpenAI-compatible；真实调用仅 Phase 7 live）---
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_base_url: str = "https://api.deepseek.com"
    model_name: str = "deepseek-v4-flash"
    judge_model_name: str = "deepseek-v4-pro"

    # --- LLM 调用的超时与重试（只重试 429 / 5xx / 连接失败 / 连接超时；读超时与响应中断不重试）---
    llm_connect_timeout_seconds: float = 10.0
    llm_read_timeout_seconds: float = 60.0
    llm_max_retries: int = 2  # 首次之外最多再试几次
    llm_retry_base_seconds: float = 0.5  # 指数退避：base * 2^(n-1)，封顶 llm_retry_max_seconds
    llm_retry_max_seconds: float = 8.0
    llm_retry_after_max_seconds: float = 20.0  # Retry-After 超过它就不在请求线程里等，直接失败

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
    # 本地模型目录（设置后只从该目录加载，不联网）；HF_ENDPOINT 只在显式配置时写入进程环境，默认官方源
    bge_model_path: str = ""
    hf_endpoint: str = ""
    hf_hub_offline: bool = False
    # 拒答阈值：fake 用停用字过滤后的 bigram 余弦（实测可答≥0.24 / 无关≤0.23，阈值 0.22）
    # bge 用真实语义余弦，分布不同，单独设阈值
    retrieval_score_threshold_fake: float = 0.22
    retrieval_score_threshold_bge: float = 0.52

    # --- 知识库版本（D-021）：启动恢复宽限期 ---
    # 只有创建早于该时长、且无存活连接持有导入锁的 DRAFT/INGESTING 版本才会被标为 FAILED：
    # 避免多实例时把别的实例刚上传、后台任务尚未取锁的草稿误判为「中断」
    kb_recover_grace_minutes: int = 10

    # --- Cost Guard ---
    max_single_live_eval_cost_usd: float = 1.00  # preflight 预检，--force 可越
    hard_eval_cost_limit_usd: float = 2.00  # 硬闸，任何情况不可越

    # --- 简化 JWT ---
    jwt_secret: str = DEFAULT_JWT_SECRET  # ≥32 字节（RFC 7518）
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 720


def base_url_problem(base_url: str) -> str | None:
    """base_url 必须是带主机名的 http(s) 地址；明文 http 只允许本机（替身 / 本地网关），否则 key 会明文上网。"""
    try:
        url = httpx.URL(base_url)
    except (httpx.InvalidURL, TypeError):
        return "DEEPSEEK_BASE_URL 不是合法的 URL"
    if url.scheme not in ("http", "https") or not url.host:
        return "DEEPSEEK_BASE_URL 必须是 http(s)://主机名 形式"
    if url.scheme == "http" and url.host not in LOOPBACK_HOSTS:
        return "DEEPSEEK_BASE_URL 必须使用 https（明文 http 会在网络上暴露 key；仅本机地址例外）"
    return None


def live_config_problems(settings: Settings) -> list[str]:
    """live 模式（NO_PAID_API=false）必需的配置缺口；离线模式恒为空。"""
    if settings.no_paid_api:
        return []
    problems: list[str] = []
    if not settings.deepseek_api_key.get_secret_value().strip():
        problems.append("缺少 DEEPSEEK_API_KEY")
    url_problem = base_url_problem(settings.deepseek_base_url)
    if url_problem:
        problems.append(url_problem)
    if not settings.model_name.strip():
        problems.append("缺少 MODEL_NAME")
    # P1 守卫：公开可猜的 HS256 默认密钥 = 令牌可伪造
    if settings.jwt_secret == DEFAULT_JWT_SECRET:
        problems.append("禁止使用默认 JWT_SECRET：请设置 >=32 字节的随机密钥")
    return problems


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    # live 模式启动即校验（离线/CI 不受影响）：缺任何一项都拒绝启动，一次列出全部缺口
    problems = live_config_problems(settings)
    if problems:
        raise RuntimeError(
            "live 模式（NO_PAID_API=false）配置不完整，拒绝启动：" + "；".join(problems)
        )
    return settings
