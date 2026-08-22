"""LLM 客户端抽象（OpenAI-compatible 统一接口，规格 §3）。

Phase 2 仅提供协议 + Fake 实现（离线零付费）；
DeepSeek 真实实现（OpenAI 兼容 /chat/completions）在 Phase 4 接入，且遵守 NO_PAID_API。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class LLMResponse:
    content: str
    model: str
    usage: LLMUsage


class LLMClient(Protocol):
    """所有 LLM 调用走此接口；业务代码不与供应商绑定。"""

    model_name: str

    def complete(
        self, system: str, user: str, *, max_tokens: int = 1500, json_mode: bool = False
    ) -> LLMResponse:
        ...


class FakeLLMClient:
    """确定性伪 LLM：直接返回 user 内容摘要（离线测试/CI 用，零 token）。"""

    def __init__(self, model_name: str = "fake-llm") -> None:
        self.model_name = model_name

    def complete(
        self, system: str, user: str, *, max_tokens: int = 1500, json_mode: bool = False
    ) -> LLMResponse:
        _ = system, max_tokens, json_mode
        content = user if len(user) <= 600 else user[:600] + "…"
        usage = LLMUsage(prompt_tokens=len(user), completion_tokens=len(content), total_tokens=len(user) + len(content))
        return LLMResponse(content=content, model=self.model_name, usage=usage)
