"""在指定 Docker 测试库上重建种子并采集真实浏览器演示截图。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from playwright.sync_api import Page, expect, sync_playwright
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ASSETS = ROOT / "docs" / "assets"
TEST_URL = "postgresql+psycopg://app:app@localhost:55432/agent_cs_test"
ASK_REFUND = "A10001 买的耳机用了三天坏了，可以退款吗"


def guard_test_database() -> None:
    url = os.environ.get("DATABASE_URL")
    if url != TEST_URL or os.environ.get("TEST_DATABASE_URL") != TEST_URL:
        raise RuntimeError("截图只允许 DATABASE_URL 与 TEST_DATABASE_URL 同时指向 localhost:55432/agent_cs_test")
    if os.environ.get("NO_PAID_API", "true").lower() != "true" or os.environ.get("EMBEDDING_BACKEND", "fake") != "fake":
        raise RuntimeError("截图必须使用 NO_PAID_API=true 与 EMBEDDING_BACKEND=fake")
    parsed = make_url(url)
    if (parsed.username, parsed.password, parsed.host, parsed.port, parsed.database) != (
        "app", "app", "localhost", 55432, "agent_cs_test"
    ):
        raise RuntimeError("测试库连接参数不符合指定容器")
    running = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", "csagent-pg-test"],
        capture_output=True, text=True, check=True,
    )
    if running.stdout.strip() != "true":
        raise RuntimeError("csagent-pg-test 未运行")
    ports = subprocess.run(
        ["docker", "port", "csagent-pg-test", "5432/tcp"],
        capture_output=True, text=True, check=True,
    )
    if not any(line.endswith(":55432") for line in ports.stdout.splitlines()):
        raise RuntimeError("csagent-pg-test 没有映射到主机 55432，拒绝重置")


def reset_test_database() -> None:
    """仅清空 agent_cs_test，重建迁移、业务种子和 fake 知识库初版。"""
    guard_test_database()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", TEST_URL)
    command.upgrade(cfg, "head")

    from app.kb.service import Retrieval, bootstrap_from_directory
    from app.kb.smoke import load_smoke_config
    from app.models import Base
    from app.rag import FakeEmbedding
    from app.services.database import SessionLocal
    from scripts.seed_db import seed

    engine = create_engine(TEST_URL, pool_pre_ping=True)
    try:
        with engine.begin() as conn:
            if conn.execute(text("SELECT current_database()")).scalar_one() != "agent_cs_test":
                raise RuntimeError("实际连接的库不是 agent_cs_test，拒绝重置")
            tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
            conn.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
        seed()
        # 迁移创建的独立工单序列不归属于 tickets 表，TRUNCATE 不会复位。
        with engine.begin() as conn:
            conn.execute(text("ALTER SEQUENCE ticket_id_sequence RESTART WITH 10003"))
        retrieval = Retrieval(embedder=FakeEmbedding(), threshold=0.22, smoke=load_smoke_config())
        result, version = bootstrap_from_directory(
            SessionLocal,
            ROOT / "knowledge_base", retrieval,
        )
        if result != "ACTIVATED" or version.status != "ACTIVE":
            raise RuntimeError(f"知识库种子未成功生效：{result}")
    finally:
        engine.dispose()


def shot(page: Page, name: str) -> None:
    path = ASSETS / f"{name}.png"
    page.screenshot(path=str(path), full_page=True, animations="disabled")
    print(path)


def capture(base: str) -> None:
    from tests_e2e.helpers import ui_login

    ASSETS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:
            if "Executable doesn't exist" not in str(exc):
                raise
            # 本机 Chromium 缓存缺失时仍由 Playwright 驱动已安装的 Chrome。
            browser = playwright.chromium.launch(channel="chrome", headless=True)
        try:
            customer = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
            page = customer.new_page()
            page.goto(f"{base}/#/login")
            expect(page.locator("form.login-card")).to_be_visible()
            shot(page, "login")

            ui_login(page, base, "demo_customer", "#/chat")
            expect(page.get_by_test_id("current-user")).to_contain_text("demo_customer")
            page.get_by_role("button", name="这个耳机支持多久保修？").click()
            expect(page.get_by_test_id("msg-assistant").last.get_by_test_id("sources")).to_contain_text("《")
            expect(page.get_by_test_id("basis-answer-mode")).not_to_be_empty()
            shot(page, "chat_citation")

            page.get_by_role("button", name=ASK_REFUND).click()
            expect(page.get_by_test_id("msg-assistant").last.get_by_test_id("eligibility")).to_contain_text("符合")
            expect(page.get_by_test_id("confirm-card")).to_be_visible()
            expect(page.get_by_test_id("basis-answer-mode")).not_to_be_empty()
            shot(page, "chat_eligibility")
            page.get_by_test_id("confirm-card").screenshot(
                path=str(ASSETS / "pending_confirmation.png"), animations="disabled"
            )
            print(ASSETS / "pending_confirmation.png")

            page.get_by_test_id("confirm-yes").click()
            ticket_link = page.get_by_test_id("msg-assistant").last.get_by_test_id("ticket-link")
            expect(ticket_link).to_be_visible()
            ticket_id = ticket_link.inner_text()
            ticket_link.click()
            expect(page.get_by_test_id("ticket-detail")).to_be_visible()
            expect(page.locator("[data-testid=timeline-item][data-event=CREATED]")).to_have_count(1)
            shot(page, "customer_ticket_timeline")

            support = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
            support_page = support.new_page()
            ui_login(support_page, base, "support_agent", "#/support/tickets?scope=unassigned")
            row = support_page.locator(f"[data-testid=queue-row][data-ticket-id='{ticket_id}']")
            expect(row).to_be_visible()
            shot(support_page, "support_queue")
            row.get_by_test_id("claim").click()
            expect(support_page.get_by_test_id("assignee")).to_contain_text("我")
            support_page.get_by_test_id("support-reply").locator("textarea").fill("您好，已安排检测，请寄回耳机。")
            support_page.get_by_test_id("support-reply-submit").click()
            expect(support_page.get_by_test_id("reply-content").last).to_contain_text("已安排检测")
            support_page.get_by_test_id("advance").click()
            expect(support_page.get_by_test_id("advance")).to_have_attribute("data-target", "RESOLVED")
            support_page.get_by_test_id("advance").click()
            expect(support_page.get_by_test_id("status-pill").first).to_have_attribute("data-status", "RESOLVED")
            shot(support_page, "support_ticket_resolved")

            kb = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
            kb_page = kb.new_page()
            ui_login(kb_page, base, "kb_admin", "#/kb")
            expect(kb_page.get_by_test_id("kb-row").first).to_be_visible()
            expect(kb_page.get_by_test_id("kb-status").first).to_be_visible()
            shot(kb_page, "kb_versions")

            with page.expect_response(lambda response: response.url.endswith("/api/tickets/T10002")) as denied:
                page.goto(f"{base}/#/tickets/T10002")
            if denied.value.status != 403:
                raise RuntimeError(f"越权接口应返回 403，实际 {denied.value.status}")
            expect(page.get_by_test_id("access-denied")).to_be_visible()
            expect(page.locator("main")).not_to_contain_text("超期退款申请咨询")
            shot(page, "customer_idor_denied")
        finally:
            browser.close()


def main() -> int:
    guard_test_database()
    reset_test_database()
    from tests_e2e.conftest import run_uvicorn

    with run_uvicorn({"NO_PAID_API": "true", "EMBEDDING_BACKEND": "fake"}) as (base, log):
        try:
            capture(base)
        finally:
            print(f"uvicorn 日志：{log}")
    print("uvicorn 已关闭")
    return 0


if __name__ == "__main__":
    sys.exit(main())
