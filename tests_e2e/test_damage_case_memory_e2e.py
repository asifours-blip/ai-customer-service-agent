"""真实浏览器、uvicorn、PostgreSQL：人工发布、检索来源与撤回。"""

from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import Page, expect

from app.models.ticket import Ticket
from tests_e2e.helpers import ui_login


def test_reviewed_damage_case_browser_flow(page: Page, app_url: str, db, tmp_path: Path) -> None:  # noqa: ANN001
    with db() as session:
        session.add_all([
            Ticket(
                id="T90001", user_id="U002", order_id="A20001", category="OTHER",
                title="运输破损历史工单", description="客户原文不进入案例索引",
                status="RESOLVED", priority="MEDIUM", assignee_id="SUPPORT001",
            ),
            Ticket(
                id="T90002", user_id="U001", order_id="A10001", category="OTHER",
                title="当前物流破损待核", description="包装与商品需拍照核验",
                status="OPEN", priority="MEDIUM",
            ),
        ])
        session.commit()

    screenshot = Path(os.environ.get("CASE_MEMORY_SCREENSHOT", str(tmp_path / "case-memory-ui.png")))
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    ui_login(page, app_url, "support_agent", "#/support/tickets/T90002")
    expect(page.get_by_test_id("damage-cases-panel")).to_be_visible()
    page.get_by_test_id("search-damage-cases").click()
    expect(page.get_by_test_id("damage-draft-status")).to_contain_text("NO_CASES")

    page.goto(f"{app_url}/#/support/tickets/T90001")
    expect(page.get_by_test_id("approve-damage-case")).to_be_visible()
    page.get_by_test_id("approve-damage-kind").select_option("PRODUCT")
    page.get_by_test_id("approve-reviewed-path").select_option("REQUEST_EVIDENCE")
    page.get_by_test_id("approve-damage-case").click()
    expect(page.get_by_role("alert")).to_contain_text("请先核对")
    page.get_by_test_id("confirm-damage-case").check()
    page.get_by_test_id("approve-damage-case").click()
    expect(page.get_by_test_id("reviewed-damage-case")).to_contain_text("已审核")

    page.goto(f"{app_url}/#/support/tickets/T90002")
    page.get_by_test_id("damage-kind").select_option("PRODUCT")
    page.get_by_test_id("search-damage-cases").click()
    expect(page.get_by_test_id("damage-draft-status")).to_contain_text("CASE_ASSISTED")
    expect(page.get_by_test_id("damage-next-step")).to_contain_text("T90001")
    expect(page.get_by_test_id("damage-case-list")).to_contain_text("相似")
    expect(page.get_by_test_id("damage-case-list")).to_contain_text("差异/待核")
    page.screenshot(path=str(screenshot), full_page=True)

    page.get_by_role("link", name="历史工单 T90001").click()
    expect(page.get_by_test_id("reviewed-damage-case")).to_be_visible()
    page.get_by_test_id("withdraw-damage-case").click()
    expect(page.get_by_test_id("damage-cases-panel")).to_contain_text("已撤回")
    page.goto(f"{app_url}/#/support/tickets/T90002")
    page.get_by_test_id("search-damage-cases").click()
    expect(page.get_by_test_id("damage-draft-status")).to_contain_text("NO_CASES")
