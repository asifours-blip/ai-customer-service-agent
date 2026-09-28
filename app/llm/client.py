"""LLM 客户端抽象（OpenAI-compatible 统一接口，规格 §3）。

- LLMClient 协议：业务代码只依赖它；离线 FakeLLMClient 与真实 DeepseekClient 走同一套调用路径
- LLMUsage：用量带状态——reported（接口返回）/ unknown（没返回或结果未知，数字是按上限估的计费值）/ none（确定未计费）
- 调用记录：每次 complete() 都登记一条 LLMCallRecord（类别、耗时、重试次数、usage），由 AgentService 写进 Trace
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol

USAGE_REPORTED = "reported"
USAGE_UNKNOWN = "unknown"
USAGE_NONE = "none"

# 回答方式（前端与 Trace 展示）：模型生成 / 离线回显（未调用模型）/ 确定性模板 / 出错提示
ANSWER_MODE_MODEL = "MODEL"
ANSWER_MODE_OFFLINE_ECHO = "OFFLINE_ECHO"
ANSWER_MODE_TEMPLATE = "TEMPLATE"
ANSWER_MODE_ERROR = "ERROR"

# 聊天模板每条消息的角色标记等固定开销（token），取宽裕值保证是上限
PROMPT_OVERHEAD_TOKENS = 32


@dataclass(frozen=True)
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    status: str = USAGE_REPORTED

    @classmethod
    def ceiling(cls, system: str, user: str, max_tokens: int) -> LLMUsage:
        """usage 未知时的计费上限。

        输入：字节级 BPE 的 token 数不超过 UTF-8 字节数，再加模板开销；输出：不超过请求的 max_tokens。
        """
        prompt = len(system.encode("utf-8")) + len(user.encode("utf-8")) + PROMPT_OVERHEAD_TOKENS
        return cls(prompt, max_tokens, prompt + max_tokens, USAGE_UNKNOWN)

    @classmethod
    def none(cls) -> LLMUsage:
        """请求确定没有被处理（未发出 / 连接失败 / 服务端明确拒绝）：不计费。"""
        return cls(0, 0, 0, USAGE_NONE)

    def to_trace(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True)
class LLMResponse:
    content: str
    model: str
    usage: LLMUsage
    finish_reason: str | None = None
    attempts: int = 1
    request_id: str | None = None


class LLMClient(Protocol):
    """所有 LLM 调用走此接口；业务代码不与供应商绑定。"""

    model_name: str
    answer_mode: str

    def complete(
        self, system: str, user: str, *, max_tokens: int = 1500, json_mode: bool = False
    ) -> LLMResponse:
        ...


def answer_mode_of(llm: object) -> str:
    """客户端声明的回答方式；未声明的实现按真实模型处理（宁可标为模型，也不把模型回答标成模板）。"""
    return str(getattr(llm, "answer_mode", ANSWER_MODE_MODEL))


# ---------------------------------------------------------------- 调用记录


@dataclass(frozen=True)
class LLMCallRecord:
    model: str
    mode: str
    outcome: str  # OK 或 LLMErrorCategory 的值
    latency_ms: int
    attempts: int  # 实际发出的 HTTP 请求数（未配置时为 0）
    usage: LLMUsage
    finish_reason: str | None = None
    status_code: int | None = None
    request_id: str | None = None

    @property
    def retries(self) -> int:
        return max(0, self.attempts - 1)

    def to_trace(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "mode": self.mode,
            "outcome": self.outcome,
            "latency_ms": self.latency_ms,
            "attempts": self.attempts,
            "retries": self.retries,
            "usage": self.usage.to_trace(),
            "finish_reason": self.finish_reason,
            "status_code": self.status_code,
            "request_id": self.request_id,
        }


_CALLS: ContextVar[list[LLMCallRecord] | None] = ContextVar("llm_calls", default=None)


@contextmanager
def recording_llm_calls() -> Iterator[list[LLMCallRecord]]:
    """在当前上下文收集本轮所有 LLM 调用记录（成功与失败都记）。"""
    calls: list[LLMCallRecord] = []
    token = _CALLS.set(calls)
    try:
        yield calls
    finally:
        _CALLS.reset(token)


def record_llm_call(record: LLMCallRecord) -> None:
    calls = _CALLS.get()
    if calls is not None:
        calls.append(record)


class FakeLLMClient:
    """确定性伪 LLM：直接返回 user 内容摘要（离线测试/CI 用，零 token）。

    它不生成回答，只回显输入（RAG 路径下即检索到的知识库原文），所以回答方式标为 OFFLINE_ECHO。
    """

    answer_mode = ANSWER_MODE_OFFLINE_ECHO

    def __init__(self, model_name: str = "fake-llm") -> None:
        self.model_name = model_name

    def complete(
        self, system: str, user: str, *, max_tokens: int = 1500, json_mode: bool = False
    ) -> LLMResponse:
        _ = system, max_tokens, json_mode
        started = time.monotonic()
        content = user if len(user) <= 600 else user[:600] + "…"
        usage = LLMUsage(prompt_tokens=len(user), completion_tokens=len(content), total_tokens=len(user) + len(content))
        resp = LLMResponse(content=content, model=self.model_name, usage=usage, finish_reason="stop")
        record_llm_call(
            LLMCallRecord(
                model=self.model_name,
                mode=self.answer_mode,
                outcome="OK",
                latency_ms=int((time.monotonic() - started) * 1000),
                attempts=1,
                usage=usage,
                finish_reason="stop",
            )
        )
        return resp
