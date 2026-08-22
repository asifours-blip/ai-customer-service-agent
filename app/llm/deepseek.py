"""DeepSeek OpenAI-compatible 客户端（真实调用，仅 live/Phase 7）。

红线（审核修订⑦）：
- Key 只从 DEEPSEEK_API_KEY 环境变量读取；不打印、不写日志、不进报告正文
- NO_PAID_API=true（Phase 0~6 默认）时拒绝真实调用，防止意外烧钱
"""

from __future__ import annotations

import httpx

from app.config import get_settings
from app.llm.client import LLMResponse, LLMUsage
from app.services.errors import ValidationFailedError

_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class DeepseekClient:
    """最小 OpenAI-compatible /chat/completions 封装（非流式）。model_name 属性满足 LLMClient 协议。"""

    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self.api_key = api_key or settings.deepseek_api_key
        self.base_url = (base_url or settings.deepseek_base_url).rstrip("/")
        self.model_name = model or settings.model_name

    def complete(self, system: str, user: str, *, max_tokens: int = 1500, json_mode: bool = False) -> LLMResponse:
        settings = get_settings()
        if settings.no_paid_api:
            raise ValidationFailedError("NO_PAID_API=true：真实 LLM 调用被策略拦截（live 评测需显式关闭）")
        if not self.api_key:
            raise ValidationFailedError("缺少 DEEPSEEK_API_KEY 环境变量")
        body: dict[str, object] = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        resp = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        return LLMResponse(
            content=content,
            model=str(data.get("model", self.model_name)),
            usage=LLMUsage(
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                total_tokens=int(usage.get("total_tokens", 0)),
            ),
        )
