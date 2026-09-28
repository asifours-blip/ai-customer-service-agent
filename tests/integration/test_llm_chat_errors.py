"""聊天接口 × 真实客户端（打本地替身）× Trace（阶段 4，先写红测试）。

真实模式与离线模式走同一套 AgentService / 图 / RagService，只替换 LLM 客户端：
- 缺配置：替身收到 0 个请求，聊天接口返回明确的「模型未配置」错误，不回退到模板或回显答案
- 各类失败：返回用户看得懂的错误，Trace 记录类别、耗时、重试次数、usage（或 unknown）
- 离线：回答带 answer_mode 标识（OFFLINE_ECHO / TEMPLATE），不冒充模型回答
- BGE 替身维度不匹配：拒绝检索，提示重建知识库版本
"""

from __future__ import annotations

import importlib.machinery
import sys
import types
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import httpx
import pytest

from app.config import Settings
from tests.conftest import auth_headers
from tests.llm_standin import LLMStandIn

pytestmark = [pytest.mark.integration]

FAKE_KEY = f"sk-chat-canary-{uuid4().hex}"
ASK_POLICY = "这个耳机支持多久保修？"


@pytest.fixture(scope="module")
def standin() -> Iterator[LLMStandIn]:
    with LLMStandIn() as s:
        yield s


def _use_real_client(monkeypatch: pytest.MonkeyPatch, base_url: str, **overrides: object) -> list[float]:
    """把聊天接口的 Agent 换成「真实 DeepseekClient → 替身」，其余装配与线上完全一致。"""
    from app.agent.service import AgentService
    from app.api import chat as chat_module
    from app.llm.deepseek import DeepseekClient
    from app.rag import FakeEmbedding
    from app.rag.answerer import RagService
    from app.tools import build_registry

    values: dict[str, object] = {
        "no_paid_api": False, "deepseek_api_key": FAKE_KEY, "deepseek_base_url": base_url,
        "model_name": "standin-model", "jwt_secret": "j" * 40, "llm_max_retries": 2,
        "llm_read_timeout_seconds": 0.5, "llm_connect_timeout_seconds": 0.3, "llm_retry_base_seconds": 0.01,
        **overrides,
    }
    sleeps: list[float] = []
    llm = DeepseekClient(
        settings=Settings(_env_file=None, **values),  # type: ignore[arg-type]
        http_client=httpx.Client(trust_env=False),
        sleep=sleeps.append,
    )
    service = AgentService(llm, RagService(FakeEmbedding(), llm, score_threshold=0.22), build_registry())
    monkeypatch.setattr(chat_module, "_agent_service", lambda: service)
    return sleeps


def _chat(client: Any, message: str, session: str | None = None) -> Any:
    return client.post(
        "/api/chat",
        json={"session_id": session or f"s-{uuid4().hex[:10]}", "message": message},
        headers=auth_headers(client, "demo_customer"),
    )


def _trace(client: Any, trace_id: str) -> dict[str, Any]:
    resp = client.get(f"/api/traces/{trace_id}", headers=auth_headers(client, "demo_customer"))
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


# ---------------- 缺配置 ----------------


def test_chat_without_key_returns_model_not_configured_and_sends_nothing(
    client: Any, standin: LLMStandIn, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_real_client(monkeypatch, standin.url("ok"), deepseek_api_key="")
    before = standin.total
    resp = _chat(client, ASK_POLICY)
    assert standin.total == before  # 一个请求都没发
    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert detail["type"] == "MODEL_NOT_CONFIGURED"
    assert detail["category"] == "NOT_CONFIGURED"
    assert "未配置" in detail["message"]
    assert FAKE_KEY not in resp.text

    trace = _trace(client, detail["trace_id"])
    assert trace["error_type"] == "LLM_NOT_CONFIGURED"
    assert trace["answer_mode"] == "ERROR"
    assert trace["llm_calls"][0]["outcome"] == "NOT_CONFIGURED"
    assert trace["llm_calls"][0]["attempts"] == 0
    # 没有悄悄回退：落库的回答就是错误提示，不是回显/模板答案
    assert trace["final_answer"] == detail["message"]


def test_tool_routes_still_work_without_model(client: Any, standin: LLMStandIn,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """订单查询不经过模型：缺 key 时照常回答，并明确标注为模板回复。"""
    _use_real_client(monkeypatch, standin.url("ok"), deepseek_api_key="")
    before = standin.total
    resp = _chat(client, "帮我查一下订单 A10001")
    assert resp.status_code == 200, resp.text
    assert resp.json()["answer_mode"] == "TEMPLATE"
    assert standin.total == before


# ---------------- 各类失败 → 用户可读提示 + Trace ----------------


def test_rate_limited_chat_records_retries_in_trace(client: Any, standin: LLMStandIn,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps = _use_real_client(monkeypatch, standin.url("rate429"))
    before = standin.count("rate429")
    resp = _chat(client, ASK_POLICY)
    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert detail["type"] == "MODEL_CALL_FAILED" and detail["category"] == "RATE_LIMITED"
    assert "稍后" in detail["message"]
    assert "429" not in detail["message"] and "standin" not in detail["message"]  # 不暴露内部细节
    assert standin.count("rate429") - before == 3 and sleeps == [3.0, 3.0]

    call = _trace(client, detail["trace_id"])["llm_calls"][0]
    assert call["outcome"] == "RATE_LIMITED"
    assert call["attempts"] == 3 and call["retries"] == 2
    assert call["status_code"] == 429
    assert call["usage"]["status"] == "none"
    assert call["latency_ms"] >= 0


def test_read_timeout_chat_billed_at_ceiling_in_trace(client: Any, standin: LLMStandIn,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    _use_real_client(monkeypatch, standin.url("readtimeout"))
    before = standin.count("readtimeout")
    resp = _chat(client, ASK_POLICY)
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["category"] == "READ_TIMEOUT"
    assert standin.count("readtimeout") - before == 1  # 不重试
    trace = _trace(client, detail["trace_id"])
    call = trace["llm_calls"][0]
    assert call["usage"]["status"] == "unknown"
    assert call["attempts"] == 1
    # 预算记账按上限计入：Trace 的 token 就是上限值（不是 0）
    assert trace["prompt_tokens"] == call["usage"]["prompt_tokens"] > 0
    assert trace["completion_tokens"] == call["usage"]["completion_tokens"] == 1500


def test_truncated_answer_is_not_shown_as_complete(client: Any, standin: LLMStandIn,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    _use_real_client(monkeypatch, standin.url("length"))
    resp = _chat(client, ASK_POLICY)
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["category"] == "TRUNCATED"
    assert "回答写到一半" not in resp.text  # 截断的半截内容不展示给用户
    trace = _trace(client, detail["trace_id"])
    assert trace["llm_calls"][0]["usage"] == {
        "status": "reported", "prompt_tokens": 40, "completion_tokens": 16, "total_tokens": 56,
    }


def test_success_without_usage_marks_unknown(client: Any, standin: LLMStandIn,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    _use_real_client(monkeypatch, standin.url("nousage"))
    resp = _chat(client, ASK_POLICY)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"] == "没有 usage 的回答"
    assert body["answer_mode"] == "MODEL"
    trace = _trace(client, body["trace_id"])
    call = trace["llm_calls"][0]
    assert call["outcome"] == "OK" and call["mode"] == "MODEL"
    assert call["usage"]["status"] == "unknown"
    assert trace["prompt_tokens"] == call["usage"]["prompt_tokens"] > 0


def test_model_answer_success_marks_model_mode(client: Any, standin: LLMStandIn,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    _use_real_client(monkeypatch, standin.url("ok"))
    resp = _chat(client, ASK_POLICY)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"] == "替身回答" and body["answer_mode"] == "MODEL"
    assert body["sources"]
    trace = _trace(client, body["trace_id"])
    assert trace["answer_mode"] == "MODEL"
    assert trace["prompt_tokens"] == 21 and trace["completion_tokens"] == 9


def test_failed_turn_history_is_marked_as_error(client: Any, standin: LLMStandIn,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """刷新后历史里的失败轮次仍标为 ERROR，不会看起来像一条正常回答。"""
    _use_real_client(monkeypatch, standin.url("server500"))
    session = f"s-{uuid4().hex[:10]}"
    assert _chat(client, ASK_POLICY, session).status_code == 503
    headers = auth_headers(client, "demo_customer")
    conv = next(c for c in client.get("/api/conversations", headers=headers).json() if c["session_id"] == session)
    messages = client.get(f"/api/conversations/{conv['id']}", headers=headers).json()["messages"]
    assert messages[-1]["role"] == "assistant" and messages[-1]["answer_mode"] == "ERROR"


# ---------------- 离线：mode 标识 ----------------


def test_offline_answers_carry_mode(client: Any) -> None:
    rag = _chat(client, ASK_POLICY).json()
    assert rag["answer_mode"] == "OFFLINE_ECHO"  # FakeLLM 回显检索原文，不是模型生成
    order = _chat(client, "帮我查一下订单 A10001").json()
    assert order["answer_mode"] == "TEMPLATE"
    trace = _trace(client, rag["trace_id"])
    assert trace["answer_mode"] == "OFFLINE_ECHO"
    assert trace["llm_calls"][0]["mode"] == "OFFLINE_ECHO" and trace["llm_calls"][0]["outcome"] == "OK"


# ---------------- 配置状态接口（仅管理员）----------------


def test_config_status_admin_only(client: Any) -> None:
    resp = client.get("/api/system/config", headers=auth_headers(client, "kb_admin"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["no_paid_api"] is True
    assert body["llm"]["api_key_set"] is False
    assert body["llm"]["answer_mode"] == "OFFLINE_ECHO"
    assert set(body["embedding"]) >= {"backend", "configured_backend", "bge"}
    for username in ("demo_customer", "support_agent"):
        assert client.get("/api/system/config", headers=auth_headers(client, username)).status_code == 403


def test_config_status_never_shows_key(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import system as system_module

    live = Settings(_env_file=None, no_paid_api=False, deepseek_api_key=FAKE_KEY,  # type: ignore[call-arg]
                    jwt_secret="j" * 40)
    monkeypatch.setattr(system_module, "get_settings", lambda: live)
    resp = client.get("/api/system/config", headers=auth_headers(client, "kb_admin"))
    assert resp.status_code == 200
    assert resp.json()["llm"]["api_key_set"] is True
    assert FAKE_KEY not in resp.text and FAKE_KEY[:14] not in resp.text


# ---------------- BGE 替身：维度不匹配拒绝检索 ----------------


class _FakeST384:
    def __init__(self, source: str, **kwargs: Any) -> None:
        self.source = source

    def get_sentence_embedding_dimension(self) -> int:
        return 384

    def encode(self, texts: list[str], normalize_embeddings: bool = True) -> list[list[float]]:
        return [[1.0] + [0.0] * 383 for _ in texts]


def test_bge_dimension_mismatch_refuses_retrieval(client: Any, db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent.service import AgentService
    from app.api import chat as chat_module
    from app.llm.client import FakeLLMClient
    from app.rag.answerer import KbRebuildRequiredError, RagService
    from app.rag.embedding import LocalBGEEmbedding
    from app.tools import build_registry

    module = types.ModuleType("sentence_transformers")
    module.__spec__ = importlib.machinery.ModuleSpec("sentence_transformers", None)
    module.SentenceTransformer = _FakeST384  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    monkeypatch.delenv("BGE_MODEL_PATH", raising=False)
    embedder = LocalBGEEmbedding("BAAI/bge-small-zh-v1.5")
    assert embedder.dim == 384

    rag = RagService(embedder, FakeLLMClient(), score_threshold=0.5)
    with db() as s, pytest.raises(KbRebuildRequiredError, match="重建"):
        rag.answer(s, ASK_POLICY)

    service = AgentService(FakeLLMClient(), rag, build_registry())
    monkeypatch.setattr(chat_module, "_agent_service", lambda: service)
    resp = _chat(client, ASK_POLICY)
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["type"] == "KB_REBUILD_REQUIRED"
    assert "384" not in detail["message"]  # 用户提示不含内部细节；维度写在 Trace / 日志
    assert _trace(client, detail["trace_id"])["error_type"] == "KB_REBUILD_REQUIRED"
