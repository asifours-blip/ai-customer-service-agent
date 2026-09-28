"""回答方式标识与模型错误提示（阶段 4）端到端：真实浏览器 → 真实 uvicorn → 真实 PostgreSQL。

- 离线服务（NO_PAID_API=true）：回答带「离线演示 / 模板回复」标识，不冒充模型回答
- 真实模式服务（NO_PAID_API=false）：同一套应用代码，只把 base_url 指向进程内的本地 LLM 替身；
  替身持续返回 500 → 页面给出用户看得懂的提示，不暴露状态码；刷新后历史仍标为失败；服务日志里没有 key
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from playwright.sync_api import Page, expect

from tests.llm_standin import LLMStandIn
from tests_e2e.conftest import run_uvicorn
from tests_e2e.helpers import ui_login

ASK_POLICY = "这个耳机支持多久保修？"
FAKE_KEY = f"sk-e2e-canary-{uuid4().hex}"


def _ask(page: Page, text: str) -> None:
    page.fill("#chat-input", text)
    page.get_by_test_id("send").click()


def test_offline_answers_are_labeled(page: Page, app_url: str) -> None:
    ui_login(page, app_url, "demo_customer", "#/chat")
    page.wait_for_url(re.compile(r"#/chat/[^/]+$"))

    before = page.get_by_test_id("msg-assistant").count()
    _ask(page, ASK_POLICY)
    expect(page.get_by_test_id("msg-assistant")).to_have_count(before + 1)
    rag = page.get_by_test_id("msg-assistant").last
    expect(rag.get_by_test_id("answer-mode")).to_have_attribute("data-mode", "OFFLINE_ECHO")
    expect(rag.get_by_test_id("answer-mode")).to_contain_text("未调用模型")
    expect(page.get_by_test_id("basis-answer-mode")).to_contain_text("离线演示")

    _ask(page, "帮我查一下订单 A10001")
    expect(page.get_by_test_id("msg-assistant")).to_have_count(before + 2)
    order = page.get_by_test_id("msg-assistant").last
    expect(order.get_by_test_id("answer-mode")).to_have_attribute("data-mode", "TEMPLATE")

    page.reload()  # 刷新后历史里的标识仍在（取自 Trace）
    expect(page.get_by_test_id("answer-mode").first).to_be_visible()
    expect(page.locator("[data-testid=answer-mode][data-mode=OFFLINE_ECHO]")).to_have_count(1)


@pytest.fixture(scope="module")
def standin() -> Iterator[LLMStandIn]:
    with LLMStandIn() as s:
        yield s


@pytest.fixture(scope="module")
def live_mode_server(db_engine, standin: LLMStandIn) -> Iterator[tuple[str, Path]]:  # noqa: ANN001
    env = {
        "NO_PAID_API": "false",
        "EMBEDDING_BACKEND": "fake",
        "DEEPSEEK_API_KEY": FAKE_KEY,
        "DEEPSEEK_BASE_URL": standin.url("server500"),
        "MODEL_NAME": "standin-model",
        "JWT_SECRET": "e" * 48,
        "LLM_MAX_RETRIES": "2",
        "LLM_RETRY_BASE_SECONDS": "0.01",
        "PYTHONIOENCODING": "utf-8",  # 日志文件按 UTF-8 写，便于断言中文日志内容
    }
    with run_uvicorn(env) as server:
        yield server


def test_model_failure_shows_friendly_error(page: Page, db, standin: LLMStandIn,  # noqa: ANN001
                                            live_mode_server: tuple[str, Path]) -> None:
    base, log_path = live_mode_server
    ui_login(page, base, "demo_customer", "#/chat")
    page.wait_for_url(re.compile(r"#/chat/[^/]+$"))

    before = standin.count("server500")
    _ask(page, ASK_POLICY)
    error = page.get_by_test_id("chat-error")
    expect(error).to_be_visible()
    expect(error).to_have_attribute("data-category", "SERVER_ERROR")
    expect(error).to_contain_text("智能回答服务暂时异常")
    expect(error).not_to_contain_text("500")
    assert standin.count("server500") - before == 3  # 首次 + 2 次重试，全部打在本地替身上

    page.reload()
    failed = page.locator(".msg-failed")
    expect(failed).to_have_count(1)
    expect(failed.get_by_test_id("answer-mode")).to_have_attribute("data-mode", "ERROR")

    # 订单查询不依赖模型：真实模式下照常回答，并标为模板回复
    _ask(page, "帮我查一下订单 A10001")
    expect(page.locator("[data-testid=answer-mode][data-mode=TEMPLATE]")).to_have_count(1)

    log = log_path.read_text(encoding="utf-8", errors="replace")
    assert "启动配置状态" in log and '"api_key_set": true' in log
    assert "LLM 调用失败" in log and "SERVER_ERROR" in log
    assert FAKE_KEY not in log and FAKE_KEY[-12:] not in log
