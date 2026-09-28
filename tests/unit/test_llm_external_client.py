"""真实 LLM 客户端（DeepseekClient）对各类失败的分类、重试与密钥保密（阶段 4，先写红测试）。

全部请求只打到本地替身（tests/llm_standin.py），不发任何外部请求、不使用真实 key。
每个场景断言三件事：分类正确；请求次数符合重试策略（替身 / 客户端传输层计数为证）；
异常与日志里都找不到那个唯一的假 key。
"""

from __future__ import annotations

import json
import logging
import traceback
from collections.abc import Iterator
from uuid import uuid4

import httpx
import pytest

from app.config import Settings
from app.llm.client import USAGE_NONE, USAGE_REPORTED, USAGE_UNKNOWN, LLMUsage, recording_llm_calls
from app.llm.deepseek import DeepseekClient, parse_retry_after
from app.llm.errors import (
    LLMAuthError,
    LLMBadResponseError,
    LLMConnectionError,
    LLMConnectTimeoutError,
    LLMContentFilteredError,
    LLMError,
    LLMErrorCategory,
    LLMNotConfiguredError,
    LLMRateLimitError,
    LLMReadTimeoutError,
    LLMResponseInterruptedError,
    LLMServerError,
    LLMTruncatedError,
)
from tests.llm_standin import CountingTransport, LLMStandIn, SilentTLSServer, closed_port

FAKE_KEY = f"sk-standin-canary-{uuid4().hex}"
MAX_RETRIES = 2
SYSTEM = "你是测试助手。"
USER = "请回答：耳机保修多久？"
MAX_TOKENS = 64


@pytest.fixture(scope="module")
def standin() -> Iterator[LLMStandIn]:
    with LLMStandIn() as s:
        yield s


def _settings(base_url: str, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "no_paid_api": False,
        "deepseek_api_key": FAKE_KEY,
        "deepseek_base_url": base_url,
        "model_name": "standin-model",
        "jwt_secret": "j" * 40,
        "llm_max_retries": MAX_RETRIES,
        "llm_connect_timeout_seconds": 0.3,
        "llm_read_timeout_seconds": 0.5,
        "llm_retry_base_seconds": 0.01,
        "llm_retry_max_seconds": 1.0,
        "llm_retry_after_max_seconds": 10.0,
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def _client(base_url: str, **overrides: object) -> tuple[DeepseekClient, CountingTransport, list[float]]:
    transport = CountingTransport()
    sleeps: list[float] = []
    client = DeepseekClient(
        settings=_settings(base_url, **overrides),
        http_client=httpx.Client(transport=transport, trust_env=False),
        sleep=sleeps.append,
    )
    return client, transport, sleeps


def _assert_no_key(exc: BaseException, logs: str) -> None:
    rendered = [
        str(exc),
        repr(exc),
        "".join(traceback.format_exception(exc)),
        logs,
    ]
    if isinstance(exc, LLMError):
        rendered.append(json.dumps(exc.context(), ensure_ascii=False))
    for text in rendered:
        assert FAKE_KEY not in text
        assert FAKE_KEY[-12:] not in text  # 连 key 的片段都不能出现


# (场景, 期望异常类型, 期望类别, 期望请求次数, 期望 HTTP 状态码)
HTTP_FAILURES = [
    ("auth401", LLMAuthError, LLMErrorCategory.AUTH_FAILED, 1, 401),
    ("auth403", LLMAuthError, LLMErrorCategory.AUTH_FAILED, 1, 403),
    ("rate429", LLMRateLimitError, LLMErrorCategory.RATE_LIMITED, 1 + MAX_RETRIES, 429),
    ("rate429long", LLMRateLimitError, LLMErrorCategory.RATE_LIMITED, 1, 429),
    ("server500", LLMServerError, LLMErrorCategory.SERVER_ERROR, 1 + MAX_RETRIES, 500),
    ("readtimeout", LLMReadTimeoutError, LLMErrorCategory.READ_TIMEOUT, 1, None),
    ("interrupted", LLMResponseInterruptedError, LLMErrorCategory.RESPONSE_INTERRUPTED, 1, 200),
    ("notjson", LLMBadResponseError, LLMErrorCategory.BAD_RESPONSE, 1, 200),
    ("nochoices", LLMBadResponseError, LLMErrorCategory.BAD_RESPONSE, 1, 200),
    ("badtypes", LLMBadResponseError, LLMErrorCategory.BAD_RESPONSE, 1, 200),
    ("length", LLMTruncatedError, LLMErrorCategory.TRUNCATED, 1, 200),
    ("contentfilter", LLMContentFilteredError, LLMErrorCategory.CONTENT_FILTERED, 1, 200),
]


@pytest.mark.parametrize(("scenario", "exc_type", "category", "requests", "status"), HTTP_FAILURES,
                         ids=[c[0] for c in HTTP_FAILURES])
def test_http_failure_is_classified_retried_per_policy_and_never_leaks_key(
    standin: LLMStandIn, caplog: pytest.LogCaptureFixture,
    scenario: str, exc_type: type[LLMError], category: LLMErrorCategory, requests: int, status: int | None,
) -> None:
    caplog.set_level(logging.DEBUG)
    client, transport, sleeps = _client(standin.url(scenario))
    before = standin.count(scenario)
    with recording_llm_calls() as calls, pytest.raises(exc_type) as info:
        client.complete(SYSTEM, USER, max_tokens=MAX_TOKENS)
    exc = info.value
    assert exc.category is category
    assert standin.count(scenario) - before == requests  # 替身计数
    assert transport.calls == requests
    assert exc.attempts == requests
    assert exc.status_code == status
    assert len(sleeps) == requests - 1
    # 密钥确实发给了替身（测试不是空转），但异常和日志里没有
    assert f"Bearer {FAKE_KEY}" in standin.auth_seen
    _assert_no_key(exc, caplog.text)
    # 每次调用都留下调用记录（类别、耗时、重试次数、usage）
    assert len(calls) == 1
    assert calls[0].outcome == category.value
    assert calls[0].attempts == requests and calls[0].retries == requests - 1


def test_rate_limit_waits_at_least_retry_after(standin: LLMStandIn) -> None:
    client, _, sleeps = _client(standin.url("rate429"))
    with pytest.raises(LLMRateLimitError) as info:
        client.complete(SYSTEM, USER, max_tokens=MAX_TOKENS)
    assert info.value.retry_after == 3.0
    assert sleeps == [3.0, 3.0]  # 退避 0.01/0.02 < Retry-After：以 Retry-After 为准


def test_retry_after_beyond_cap_fails_fast(standin: LLMStandIn) -> None:
    client, _, sleeps = _client(standin.url("rate429long"))
    with pytest.raises(LLMRateLimitError) as info:
        client.complete(SYSTEM, USER, max_tokens=MAX_TOKENS)
    assert info.value.retry_after == 3600.0
    assert sleeps == []  # 要等一小时：不在请求线程里阻塞，直接失败


def test_server_error_uses_exponential_backoff(standin: LLMStandIn) -> None:
    client, _, sleeps = _client(standin.url("server500"))
    with pytest.raises(LLMServerError):
        client.complete(SYSTEM, USER, max_tokens=MAX_TOKENS)
    assert sleeps == pytest.approx([0.01, 0.02])


def test_retry_count_is_configurable(standin: LLMStandIn) -> None:
    before = standin.count("server500")
    client, _, _ = _client(standin.url("server500"), llm_max_retries=0)
    with pytest.raises(LLMServerError):
        client.complete(SYSTEM, USER, max_tokens=MAX_TOKENS)
    assert standin.count("server500") - before == 1


def test_503_then_success(standin: LLMStandIn, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    client, transport, sleeps = _client(standin.url("flaky503"))
    with recording_llm_calls() as calls:
        resp = client.complete(SYSTEM, USER, max_tokens=MAX_TOKENS)
    assert resp.content == "重试后的回答"
    assert standin.count("flaky503") == 2 and transport.calls == 2
    assert resp.attempts == 2 and len(sleeps) == 1
    assert resp.usage.status == USAGE_REPORTED
    assert calls[0].outcome == "OK" and calls[0].retries == 1
    assert FAKE_KEY not in caplog.text


def test_read_timeout_is_not_retried_and_billed_at_ceiling(standin: LLMStandIn) -> None:
    """读超时：请求可能已被处理并计费 → 不重试，usage 标记 unknown 并按上限计。"""
    client, _, _ = _client(standin.url("readtimeout"))
    with recording_llm_calls() as calls, pytest.raises(LLMReadTimeoutError) as info:
        client.complete(SYSTEM, USER, max_tokens=MAX_TOKENS)
    ceiling = LLMUsage.ceiling(SYSTEM, USER, MAX_TOKENS)
    assert info.value.usage == ceiling
    assert ceiling.status == USAGE_UNKNOWN and ceiling.completion_tokens == MAX_TOKENS
    assert ceiling.prompt_tokens >= len((SYSTEM + USER).encode("utf-8"))
    assert calls[0].usage == ceiling


def test_interrupted_response_is_not_retried_and_billed_at_ceiling(standin: LLMStandIn) -> None:
    client, _, _ = _client(standin.url("interrupted"))
    with pytest.raises(LLMResponseInterruptedError) as info:
        client.complete(SYSTEM, USER, max_tokens=MAX_TOKENS)
    assert info.value.usage.status == USAGE_UNKNOWN
    assert info.value.request_id == f"req-interrupted-{standin.count('interrupted')}"


def test_http_error_before_processing_is_not_billed(standin: LLMStandIn) -> None:
    client, _, _ = _client(standin.url("auth401"))
    with pytest.raises(LLMAuthError) as info:
        client.complete(SYSTEM, USER, max_tokens=MAX_TOKENS)
    assert info.value.usage.status == USAGE_NONE and info.value.usage.prompt_tokens == 0
    assert info.value.request_id and info.value.request_id.startswith("req-auth401-")


def test_truncated_keeps_reported_usage(standin: LLMStandIn) -> None:
    client, _, _ = _client(standin.url("length"))
    with pytest.raises(LLMTruncatedError) as info:
        client.complete(SYSTEM, USER, max_tokens=MAX_TOKENS)
    assert info.value.usage == LLMUsage(40, 16, 56, USAGE_REPORTED)


def test_missing_usage_is_unknown_and_counted_at_ceiling(standin: LLMStandIn) -> None:
    client, _, _ = _client(standin.url("nousage"))
    with recording_llm_calls() as calls:
        resp = client.complete(SYSTEM, USER, max_tokens=MAX_TOKENS)
    assert resp.content == "没有 usage 的回答"
    assert standin.count("nousage") >= 1
    assert resp.usage == LLMUsage.ceiling(SYSTEM, USER, MAX_TOKENS)  # 不再按 0 少计
    assert calls[0].usage.status == USAGE_UNKNOWN
    assert calls[0].to_trace()["usage"]["status"] == USAGE_UNKNOWN


def test_success_records_reported_usage(standin: LLMStandIn) -> None:
    client, _, _ = _client(standin.url("ok"))
    with recording_llm_calls() as calls:
        resp = client.complete(SYSTEM, USER, max_tokens=MAX_TOKENS)
    assert resp.usage == LLMUsage(21, 9, 30, USAGE_REPORTED)
    assert resp.finish_reason == "stop"
    record = calls[0].to_trace()
    assert record["outcome"] == "OK" and record["attempts"] == 1 and record["retries"] == 0
    assert record["latency_ms"] >= 0 and record["mode"] == "MODEL"
    assert record["request_id"] == f"req-ok-{standin.count('ok')}"


# ---------------- 传输层：连接被拒 / 连接超时 ----------------


def test_connection_refused_is_retried(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    # Windows 对本机关闭端口会重发 SYN 约 2 秒才报「拒绝」：连接超时放宽，确保测到的是连接失败而非超时
    client, transport, sleeps = _client(f"http://127.0.0.1:{closed_port()}/v1", llm_connect_timeout_seconds=5.0)
    with pytest.raises(LLMConnectionError) as info:
        client.complete(SYSTEM, USER, max_tokens=MAX_TOKENS)
    assert info.value.category is LLMErrorCategory.CONNECTION_FAILED
    assert transport.calls == 1 + MAX_RETRIES and info.value.attempts == 1 + MAX_RETRIES
    assert len(sleeps) == MAX_RETRIES
    assert info.value.usage.status == USAGE_NONE
    _assert_no_key(info.value, caplog.text)


def test_connect_timeout_is_retried(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    with SilentTLSServer() as silent:
        client, transport, sleeps = _client(silent.base_url)
        with pytest.raises(LLMConnectTimeoutError) as info:
            client.complete(SYSTEM, USER, max_tokens=MAX_TOKENS)
        accepted = silent.accepted
    assert info.value.category is LLMErrorCategory.CONNECT_TIMEOUT
    assert accepted == 1 + MAX_RETRIES  # 替身侧接受的连接数
    assert transport.calls == 1 + MAX_RETRIES and len(sleeps) == MAX_RETRIES
    _assert_no_key(info.value, caplog.text)


# ---------------- 缺配置：一个请求都不发 ----------------


@pytest.mark.parametrize(
    ("overrides", "missing"),
    [
        ({"deepseek_api_key": ""}, "DEEPSEEK_API_KEY"),
        ({"deepseek_api_key": "   "}, "DEEPSEEK_API_KEY"),
        ({"no_paid_api": True}, "NO_PAID_API"),
        ({"model_name": ""}, "MODEL_NAME"),
    ],
    ids=["no-key", "blank-key", "paid-api-off", "no-model"],
)
def test_not_configured_sends_zero_requests(
    standin: LLMStandIn, overrides: dict[str, object], missing: str
) -> None:
    before = standin.total
    client, transport, _ = _client(standin.url("ok"), **overrides)
    with recording_llm_calls() as calls, pytest.raises(LLMNotConfiguredError) as info:
        client.complete(SYSTEM, USER, max_tokens=MAX_TOKENS)
    assert standin.total == before and transport.calls == 0
    assert info.value.category is LLMErrorCategory.NOT_CONFIGURED
    assert info.value.attempts == 0
    assert missing in " ".join(info.value.missing)
    assert calls[0].outcome == "NOT_CONFIGURED" and calls[0].attempts == 0


def test_client_repr_hides_key(standin: LLMStandIn) -> None:
    client, _, _ = _client(standin.url("ok"))
    assert FAKE_KEY not in repr(client)
    assert FAKE_KEY not in repr(client.__dict__)


def test_parse_retry_after() -> None:
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after("1.5") == 1.5
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0  # 过去的时间 → 立即
    assert parse_retry_after("soon") is None
    assert parse_retry_after(None) is None
    assert parse_retry_after("-3") is None
