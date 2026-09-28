"""知识库版本管理端到端：真实浏览器（Chromium）→ 真实 uvicorn（后台导入）→ 真实 PostgreSQL。

闭环：管理员上传 → 页面轮询到 READY → 发布 → 客户提问命中新内容（引用标注版本）→ 回滚 → 不再命中，
且旧回答的「查看引用原文」仍能取回已退役版本里的原文。
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
from playwright.sync_api import Browser, Page, expect

from tests.integration.test_kb_versions import TRADEIN_MD, TRADEIN_QUERY
from tests_e2e.helpers import api_headers, ui_login


def _row(page: Page, vid: int):  # noqa: ANN202
    return page.locator(f"[data-testid=kb-row][data-version-id='{vid}']")


def _ask(page: Page, text: str):  # noqa: ANN202
    before = page.get_by_test_id("msg-assistant").count()
    page.fill("#chat-input", text)
    page.get_by_test_id("send").click()
    expect(page.get_by_test_id("msg-assistant")).to_have_count(before + 1)
    return page.get_by_test_id("msg-assistant").last


def test_admin_upload_publish_customer_hits_then_rollback(
    page: Page, browser: Browser, app_url: str, tmp_path: Path
) -> None:
    # ---- 管理员：上传 → 等到 READY → 发布
    ui_login(page, app_url, "kb_admin", "#/kb")
    page.wait_for_url("**/#/kb")
    expect(page.get_by_test_id("kb-active")).to_contain_text("v1")
    expect(_row(page, 1)).to_have_attribute("data-status", "ACTIVE")

    doc = tmp_path / "policy-tradein.md"
    doc.write_bytes(TRADEIN_MD)
    page.get_by_test_id("kb-files").set_input_files(str(doc))
    page.get_by_test_id("kb-upload").click()
    expect(_row(page, 2)).to_be_visible()
    expect(_row(page, 2)).to_have_attribute("data-status", "READY", timeout=20_000)  # 页面轮询后台导入
    expect(_row(page, 2).get_by_test_id("kb-progress")).to_contain_text("13 篇")
    expect(_row(page, 1)).to_have_attribute("data-status", "ACTIVE")  # 发布前线上不变

    _row(page, 2).get_by_test_id("kb-activate").click()
    expect(_row(page, 2)).to_have_attribute("data-status", "ACTIVE")
    expect(_row(page, 1)).to_have_attribute("data-status", "RETIRED")
    expect(page.get_by_test_id("kb-active")).to_contain_text("v2")

    # ---- 客户：提问命中新内容，引用标注版本 v2
    customer_ctx = browser.new_context()
    customer = customer_ctx.new_page()
    ui_login(customer, app_url, "demo_customer", "#/chat")
    customer.wait_for_url(re.compile(r"#/chat/[^/]+$"))
    hit = _ask(customer, TRADEIN_QUERY)
    expect(hit.get_by_test_id("sources")).to_contain_text("以旧换新政策")
    expect(hit.get_by_test_id("kb-version-note")).to_have_text(" · 引用自知识库版本 v2")
    expect(customer.get_by_test_id("basis-kb-version")).to_have_text("v2")

    # ---- 管理员：回滚到 v1
    _row(page, 1).get_by_test_id("kb-rollback").click()
    expect(_row(page, 1)).to_have_attribute("data-status", "ACTIVE")
    expect(_row(page, 2)).to_have_attribute("data-status", "RETIRED")
    page.get_by_role("link", name="v2", exact=True).first.click()
    page.wait_for_url("**/#/kb/versions/2")
    expect(page.get_by_test_id("kb-detail")).to_have_attribute("data-status", "RETIRED")
    expect(page.get_by_test_id("kb-audit")).to_have_count(5)
    actions =page.get_by_test_id("kb-audit").evaluate_all("els => els.map(e => e.dataset.action)")
    assert actions == ["CREATED", "INGEST_STARTED", "INGEST_READY", "ACTIVATED", "RETIRED"]

    # ---- 客户：同一问题不再命中新内容
    miss = _ask(customer, TRADEIN_QUERY)
    expect(miss).not_to_contain_text("以旧换新政策")
    expect(customer.get_by_test_id("basis-kb-version")).not_to_contain_text("v2")

    # ---- 刷新后，旧回答仍能查到 v2（已退役）里的原文
    customer.reload()
    old = customer.get_by_test_id("msg-assistant").filter(has=customer.get_by_test_id("kb-version-note")).first
    expect(old.get_by_test_id("kb-version-note")).to_contain_text("v2")
    old.get_by_test_id("show-citations").click()
    cited = old.get_by_test_id("citation").first
    expect(cited).to_have_attribute("data-version-id", "2")
    expect(cited).to_contain_text("已退役")
    expect(old.get_by_test_id("citations")).to_contain_text("旧耳机回收可抵扣 80 元")
    customer_ctx.close()


def test_upload_rejection_is_shown_per_file_and_non_admin_is_forbidden(
    page: Page, app_url: str, tmp_path: Path
) -> None:
    ui_login(page, app_url, "kb_admin", "#/kb")
    page.wait_for_url("**/#/kb")
    bad = tmp_path / "notes.txt"
    bad.write_bytes(TRADEIN_MD)
    gbk = tmp_path / "gbk.md"
    gbk.write_bytes(TRADEIN_MD.decode().encode("gbk"))
    page.get_by_test_id("kb-files").set_input_files([str(bad), str(gbk)])
    page.get_by_test_id("kb-upload").click()
    errors = page.get_by_test_id("kb-upload-error")
    expect(errors).to_have_count(2)
    expect(errors.nth(0)).to_have_attribute("data-check", "extension")
    expect(errors.nth(0)).to_contain_text("notes.txt")
    expect(errors.nth(1)).to_have_attribute("data-check", "encoding")
    expect(page.get_by_test_id("kb-row")).to_have_count(1)  # 整体拒绝，没有新版本

    # 客户直接打开管理页只会被前端带回首页；即使绕过前端直接调接口也是 403
    for username in ("demo_customer", "support_agent"):
        resp = httpx.get(f"{app_url}/api/kb/versions", headers=api_headers(app_url, username))
        assert resp.status_code == 403


def test_merge_disabled_when_active_version_is_migrated_without_originals(page: Page, app_url: str, db) -> None:  # noqa: ANN001
    from sqlalchemy import text

    with db() as s:  # 模拟迁移归档的 v1：只有 chunk、没有文档原文
        s.execute(text("DELETE FROM kb_documents WHERE version_id = 1"))
        s.execute(text("UPDATE kb_versions SET doc_count = 0, source = 'MIGRATION' WHERE id = 1"))
        s.commit()
    ui_login(page, app_url, "kb_admin", "#/kb")
    page.wait_for_url("**/#/kb")
    mode = page.get_by_test_id("kb-mode")
    expect(mode).to_have_value("replace")
    expect(mode.locator("option[value=merge]")).to_be_disabled()
    expect(page.get_by_test_id("kb-merge-hint")).to_contain_text("只能完整替换")
