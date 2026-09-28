"""DeepSeek OpenAI-compatible 客户端（真实调用）：离线与真实模式共用同一套上层代码，只替换这个客户端。

红线（审核修订⑦）：
- Key 只从 DEEPSEEK_API_KEY 读取；不打印、不写日志、不进异常与 Trace（SecretStr 保存，repr 只显示 ***）
- NO_PAID_API=true 或缺配置时不发任何请求，抛 LLMNotConfiguredError，由上层返回明确的「模型未配置」

失败分类见 app/llm/errors.py；重试策略：
- 只重试 429 / 5xx / 连接失败 / 连接超时：这些情况下请求没有被处理，重发不会重复计费
- 指数退避 base * 2^(n-1)（封顶 max），有 Retry-After 时至少等 Retry-After；Retry-After 超过上限直接失败
- 读超时与响应中断绝不重试：请求可能已被处理并计费；usage 记为 unknown 并按上限计入
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from pydantic import SecretStr

from app.config import Settings, base_url_problem, get_settings
from app.llm.client import (
    ANSWER_MODE_MODEL,
    USAGE_REPORTED,
    LLMCallRecord,
    LLMResponse,
    LLMUsage,
    record_llm_call,
)
from app.llm.errors import (
    LLMAuthError,
    LLMBadResponseError,
    LLMConnectionError,
    LLMConnectTimeoutError,
    LLMContentFilteredError,
    LLMError,
    LLMNotConfiguredError,
    LLMRateLimitError,
    LLMReadTimeoutError,
    LLMRequestRejectedError,
    LLMResponseInterruptedError,
    LLMServerError,
    LLMTruncatedError,
)

logger = logging.getLogger(__name__)

_REQUEST_ID_HEADERS = ("x-request-id", "request-id", "x-ds-request-id")


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After：秒数或 HTTP 日期；过去的日期 → 0；无法解析 / 负数 → None（按普通退避处理）。"""
    if value is None:
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - datetime.now(tz=UTC)).total_seconds())
    return seconds if seconds >= 0 else None


class DeepseekClient:
    """最小 OpenAI-compatible /chat/completions 封装（非流式）。model_name / answer_mode 满足 LLMClient 协议。"""

    answer_mode = ANSWER_MODE_MODEL

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        *,
        settings: Settings | None = None,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        s = settings or get_settings()
        self._api_key = SecretStr(api_key) if api_key is not None else s.deepseek_api_key
        self.base_url = (base_url or s.deepseek_base_url).rstrip("/")
        self.model_name = model or s.model_name
        self._paid_api_enabled = not s.no_paid_api
        self.max_retries = max(0, s.llm_max_retries)
        self.retry_base_seconds = s.llm_retry_base_seconds
        self.retry_max_seconds = s.llm_retry_max_seconds
        self.retry_after_max_seconds = s.llm_retry_after_max_seconds
        self._timeout = httpx.Timeout(
            s.llm_read_timeout_seconds,
            connect=s.llm_connect_timeout_seconds,
            pool=s.llm_connect_timeout_seconds,
        )
        self._http = http_client or httpx.Client()
        self._sleep = sleep

    def __repr__(self) -> str:
        return f"DeepseekClient(model={self.model_name!r}, host={self.host!r}, api_key_set={self.api_key_set})"

    @property
    def host(self) -> str:
        try:
            return httpx.URL(self.base_url).host
        except httpx.InvalidURL:
            return ""

    @property
    def api_key_set(self) -> bool:
        return bool(self._api_key.get_secret_value().strip())

    # ---------------------------------------------------------------- 对外

    def complete(self, system: str, user: str, *, max_tokens: int = 1500, json_mode: bool = False) -> LLMResponse:
        started = time.monotonic()
        try:
            resp = self._complete(system, user, max_tokens=max_tokens, json_mode=json_mode)
        except LLMError as exc:
            exc.latency_ms = int((time.monotonic() - started) * 1000)
            exc.model = exc.model or self.model_name
            self._record(exc.category.value, exc.latency_ms, exc.attempts, exc.usage,
                         status_code=exc.status_code, request_id=exc.request_id)
            logger.warning("LLM 调用失败 host=%s %s", self.host, json.dumps(exc.context(), ensure_ascii=False))
            raise
        latency_ms = int((time.monotonic() - started) * 1000)
        self._record("OK", latency_ms, resp.attempts, resp.usage,
                     finish_reason=resp.finish_reason, request_id=resp.request_id)
        logger.info(
            "LLM 调用成功 host=%s model=%s attempts=%d latency_ms=%d usage=%s request_id=%s",
            self.host, resp.model, resp.attempts, latency_ms, resp.usage.status, resp.request_id,
        )
        return resp

    # ---------------------------------------------------------------- 内部

    def _record(self, outcome: str, latency_ms: int, attempts: int, usage: LLMUsage, *,
                finish_reason: str | None = None, status_code: int | None = None,
                request_id: str | None = None) -> None:
        record_llm_call(
            LLMCallRecord(
                model=self.model_name, mode=self.answer_mode, outcome=outcome, latency_ms=latency_ms,
                attempts=attempts, usage=usage, finish_reason=finish_reason, status_code=status_code,
                request_id=request_id,
            )
        )

    def _missing_config(self) -> list[str]:
        missing: list[str] = []
        if not self._paid_api_enabled:
            missing.append("NO_PAID_API=true，真实模型调用被策略关闭")
        if not self.api_key_set:
            missing.append("缺少 DEEPSEEK_API_KEY")
        problem = base_url_problem(self.base_url)
        if problem:
            missing.append(problem)
        if not self.model_name.strip():
            missing.append("缺少 MODEL_NAME")
        return missing

    def _complete(self, system: str, user: str, *, max_tokens: int, json_mode: bool) -> LLMResponse:
        missing = self._missing_config()
        if missing:
            raise LLMNotConfiguredError(missing, model=self.model_name)
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
        ceiling = LLMUsage.ceiling(system, user, max_tokens)

        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._attempt(body, ceiling)
            except LLMError as exc:
                exc.attempts = attempt
                delay = self._retry_delay(exc, attempt)
                if delay is None:
                    raise
                logger.warning(
                    "LLM 第 %d 次请求失败（%s status=%s request_id=%s），%.2fs 后重试",
                    attempt, exc.category.value, exc.status_code, exc.request_id, delay,
                )
                self._sleep(delay)
                continue
            return LLMResponse(
                content=resp.content, model=resp.model, usage=resp.usage, finish_reason=resp.finish_reason,
                attempts=attempt, request_id=resp.request_id,
            )

    def _retry_delay(self, exc: LLMError, attempt: int) -> float | None:
        """返回下一次重试前的等待秒数；None = 不再重试。"""
        if not exc.retryable or attempt > self.max_retries:
            return None
        backoff = min(self.retry_max_seconds, self.retry_base_seconds * float(2 ** (attempt - 1)))
        if exc.retry_after is None:
            return backoff
        if exc.retry_after > self.retry_after_max_seconds:
            return None  # 服务端要求等很久：不在请求线程里阻塞，直接把限流告诉用户
        return max(backoff, exc.retry_after)

    def _attempt(self, body: dict[str, object], ceiling: LLMUsage) -> LLMResponse:
        request = self._http.build_request(
            "POST",
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key.get_secret_value()}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=self._timeout,
        )
        # 所有异常都 from None：不把 httpx 原始异常链带进日志；消息只写类别与可公开的上下文
        try:
            resp = self._http.send(request, stream=True)
        except httpx.ConnectTimeout:
            raise LLMConnectTimeoutError("连接超时") from None
        except httpx.PoolTimeout:
            raise LLMConnectTimeoutError("等待连接池超时") from None
        except httpx.ConnectError:
            raise LLMConnectionError("连接失败") from None
        except httpx.ReadTimeout:
            raise LLMReadTimeoutError("等待响应超时：请求已发出，结果未知", usage=ceiling) from None
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError, httpx.WriteTimeout):
            # 请求（可能已完整）发出后连接断开：服务端可能已处理并计费，按结果未知处理
            raise LLMResponseInterruptedError("响应中断：连接在返回前断开，结果未知", usage=ceiling) from None
        except httpx.TransportError:
            raise LLMConnectionError("网络传输失败") from None

        status = resp.status_code
        request_id = _request_id(resp.headers)
        try:
            raw = resp.read()
        except httpx.ReadTimeout:
            raise LLMReadTimeoutError("读取响应超时：结果未知", status_code=status, request_id=request_id,
                                      usage=ceiling) from None
        except (httpx.RemoteProtocolError, httpx.ReadError):
            raise LLMResponseInterruptedError("响应读到一半中断", status_code=status, request_id=request_id,
                                              usage=ceiling) from None
        finally:
            resp.close()

        common: dict[str, Any] = {"status_code": status, "request_id": request_id}
        if status in (401, 403):
            raise LLMAuthError(f"鉴权失败（HTTP {status}）", **common)
        if status == 429:
            raise LLMRateLimitError("请求被限流", retry_after=parse_retry_after(resp.headers.get("retry-after")),
                                    **common)
        if status >= 500:
            raise LLMServerError(f"服务端错误（HTTP {status}）",
                                 retry_after=parse_retry_after(resp.headers.get("retry-after")), **common)
        if status >= 400:
            raise LLMRequestRejectedError(f"请求被拒绝（HTTP {status}）", **common)
        if status != 200:
            raise LLMBadResponseError(f"意外的状态码 HTTP {status}", usage=ceiling, **common)
        return self._parse(raw, ceiling, resp.headers.get("content-type", ""), common)

    def _parse(self, raw: bytes, ceiling: LLMUsage, content_type: str, common: dict[str, Any]) -> LLMResponse:
        """解析 200 响应。只描述「哪里不对」，不回显响应体。"""
        try:
            data = json.loads(raw)
        except ValueError:
            raise LLMBadResponseError(
                f"响应不是 JSON（content-type={content_type[:60]!r}，{len(raw)} 字节）", usage=ceiling, **common
            ) from None
        usage = _parse_usage(data, ceiling)
        if not isinstance(data, dict):
            raise LLMBadResponseError("响应 JSON 顶层不是对象", usage=usage, **common)
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMBadResponseError("响应缺少 choices", usage=usage, **common)
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            raise LLMBadResponseError("choices[0] 缺少 message 对象", usage=usage, **common)
        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            raise LLMTruncatedError("输出达到 max_tokens 被截断（finish_reason=length）", usage=usage, **common)
        if finish_reason == "content_filter":
            raise LLMContentFilteredError("内容被服务端过滤（finish_reason=content_filter）", usage=usage, **common)
        if finish_reason not in (None, "stop"):
            raise LLMBadResponseError(f"未预期的 finish_reason={str(finish_reason)[:40]!r}", usage=usage, **common)
        content = message.get("content")
        if not isinstance(content, str):
            raise LLMBadResponseError("message.content 不是字符串", usage=usage, **common)
        model = data.get("model")
        return LLMResponse(
            content=content,
            model=model if isinstance(model, str) and model else self.model_name,
            usage=usage,
            finish_reason=finish_reason,
            request_id=common["request_id"],
        )


def _parse_usage(data: object, ceiling: LLMUsage) -> LLMUsage:
    """usage 完整才算 reported；缺失、缺字段、类型不对一律 unknown，按上限计。"""
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return ceiling
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (prompt, completion)):
        return ceiling
    assert isinstance(prompt, int) and isinstance(completion, int)
    total = usage.get("total_tokens")
    if not isinstance(total, int) or isinstance(total, bool):
        total = prompt + completion
    return LLMUsage(prompt, completion, total, USAGE_REPORTED)


def _request_id(headers: httpx.Headers) -> str | None:
    for name in _REQUEST_ID_HEADERS:
        value = str(headers.get(name) or "")
        if value:
            return value[:128]
    return None
