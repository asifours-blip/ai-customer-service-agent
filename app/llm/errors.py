"""LLM 调用失败的统一异常层级。

    LLMError
    ├── LLMNotConfiguredError          未配置（不发请求）
    ├── LLMHTTPError                   收到了 HTTP 错误状态码
    │   ├── LLMAuthError               401 / 403
    │   ├── LLMRateLimitError          429（解析 Retry-After）
    │   ├── LLMServerError             5xx
    │   └── LLMRequestRejectedError    其他 4xx（请求本身被拒，重试无意义）
    ├── LLMTransportError              网络层
    │   ├── LLMConnectTimeoutError     连接超时（含 TLS 握手）
    │   ├── LLMConnectionError         连接失败（拒绝 / DNS / 连接被重置）
    │   ├── LLMReadTimeoutError        读超时（请求已发出，结果未知）
    │   └── LLMResponseInterruptedError 响应中断（读到一半断开，结果未知）
    └── LLMResponseError               收到 200 但内容不可用
        ├── LLMBadResponseError        非 JSON / 缺字段 / 类型不对
        ├── LLMTruncatedError          finish_reason=length
        └── LLMContentFilteredError    finish_reason=content_filter

每个异常都带可记录的上下文（状态码、请求 id、重试次数、usage），绝不包含 key：
消息由客户端自己拼写，不回显上游响应体（服务商的 401 文案常带部分 key），也不链接原始 httpx 异常。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar

from app.llm.client import LLMUsage


class LLMErrorCategory(StrEnum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    AUTH_FAILED = "AUTH_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    SERVER_ERROR = "SERVER_ERROR"
    REQUEST_REJECTED = "REQUEST_REJECTED"
    CONNECT_TIMEOUT = "CONNECT_TIMEOUT"
    CONNECTION_FAILED = "CONNECTION_FAILED"
    READ_TIMEOUT = "READ_TIMEOUT"
    RESPONSE_INTERRUPTED = "RESPONSE_INTERRUPTED"
    BAD_RESPONSE = "BAD_RESPONSE"
    TRUNCATED = "TRUNCATED"
    CONTENT_FILTERED = "CONTENT_FILTERED"


# 可自动重试：请求没有被服务端处理（限流 / 服务端错误 / 没连上），重发不会重复计费
RETRYABLE_CATEGORIES = frozenset(
    {
        LLMErrorCategory.RATE_LIMITED,
        LLMErrorCategory.SERVER_ERROR,
        LLMErrorCategory.CONNECTION_FAILED,
        LLMErrorCategory.CONNECT_TIMEOUT,
    }
)

# 用户可读提示：不含状态码、主机、请求 id 等内部细节
USER_MESSAGES: dict[LLMErrorCategory, str] = {
    LLMErrorCategory.NOT_CONFIGURED: "智能回答暂不可用：模型未配置。订单、物流、工单查询不受影响，也可以转人工客服。",
    LLMErrorCategory.AUTH_FAILED: "智能回答暂不可用，我们已记录该问题。请稍后再试，或转人工客服。",
    LLMErrorCategory.RATE_LIMITED: "当前咨询人数较多，请稍后再试。",
    LLMErrorCategory.SERVER_ERROR: "智能回答服务暂时异常，请稍后再试。",
    LLMErrorCategory.REQUEST_REJECTED: "这个问题暂时无法由智能助手处理，请换个说法或转人工客服。",
    LLMErrorCategory.CONNECT_TIMEOUT: "暂时连接不上智能回答服务，请稍后再试。",
    LLMErrorCategory.CONNECTION_FAILED: "暂时连接不上智能回答服务，请稍后再试。",
    LLMErrorCategory.READ_TIMEOUT: "智能回答超时了，请稍后重新发送。",
    LLMErrorCategory.RESPONSE_INTERRUPTED: "智能回答在返回途中中断了，请重新发送。",
    LLMErrorCategory.BAD_RESPONSE: "智能回答服务返回了无法识别的结果，请稍后再试。",
    LLMErrorCategory.TRUNCATED: "回答内容过长被截断，为避免给出不完整的信息，本次不展示。请把问题拆小一些再问。",
    LLMErrorCategory.CONTENT_FILTERED: "这个问题无法由智能助手回答，建议转人工客服。",
}


class LLMError(Exception):
    category: ClassVar[LLMErrorCategory]

    def __init__(
        self,
        message: str,
        *,
        model: str | None = None,
        status_code: int | None = None,
        request_id: str | None = None,
        retry_after: float | None = None,
        attempts: int = 1,
        latency_ms: int = 0,
        usage: LLMUsage | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.model = model
        self.status_code = status_code
        self.request_id = request_id
        self.retry_after = retry_after
        self.attempts = attempts
        self.latency_ms = latency_ms
        # 默认「确定未计费」；读超时 / 响应中断 / 200 但 usage 不可读时由客户端填上限
        self.usage = usage or LLMUsage.none()

    @property
    def retryable(self) -> bool:
        return self.category in RETRYABLE_CATEGORIES

    @property
    def user_message(self) -> str:
        return USER_MESSAGES[self.category]

    def context(self) -> dict[str, Any]:
        """可写日志 / Trace 的结构化上下文（不含 key、不含上游响应体）。"""
        return {
            "category": self.category.value,
            "message": self.message,
            "model": self.model,
            "status_code": self.status_code,
            "request_id": self.request_id,
            "retry_after": self.retry_after,
            "attempts": self.attempts,
            "retries": max(0, self.attempts - 1),
            "latency_ms": self.latency_ms,
            "usage": self.usage.to_trace(),
        }

    def __str__(self) -> str:
        parts = [f"[{self.category.value}] {self.message}"]
        if self.status_code is not None:
            parts.append(f"status={self.status_code}")
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        return " ".join(parts)


class LLMNotConfiguredError(LLMError):
    category = LLMErrorCategory.NOT_CONFIGURED

    def __init__(self, missing: list[str], *, model: str | None = None) -> None:
        super().__init__("模型未配置：" + "；".join(missing), model=model, attempts=0)
        self.missing = tuple(missing)


class LLMHTTPError(LLMError):
    """收到了 HTTP 错误状态码。"""


class LLMAuthError(LLMHTTPError):
    category = LLMErrorCategory.AUTH_FAILED


class LLMRateLimitError(LLMHTTPError):
    category = LLMErrorCategory.RATE_LIMITED


class LLMServerError(LLMHTTPError):
    category = LLMErrorCategory.SERVER_ERROR


class LLMRequestRejectedError(LLMHTTPError):
    category = LLMErrorCategory.REQUEST_REJECTED


class LLMTransportError(LLMError):
    """网络层失败。"""


class LLMConnectTimeoutError(LLMTransportError):
    category = LLMErrorCategory.CONNECT_TIMEOUT


class LLMConnectionError(LLMTransportError):
    category = LLMErrorCategory.CONNECTION_FAILED


class LLMReadTimeoutError(LLMTransportError):
    category = LLMErrorCategory.READ_TIMEOUT


class LLMResponseInterruptedError(LLMTransportError):
    category = LLMErrorCategory.RESPONSE_INTERRUPTED


class LLMResponseError(LLMError):
    """收到 200 但内容不可用。"""


class LLMBadResponseError(LLMResponseError):
    category = LLMErrorCategory.BAD_RESPONSE


class LLMTruncatedError(LLMResponseError):
    category = LLMErrorCategory.TRUNCATED


class LLMContentFilteredError(LLMResponseError):
    category = LLMErrorCategory.CONTENT_FILTERED
