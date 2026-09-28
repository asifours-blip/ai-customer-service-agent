"""客户端 + 客服工作台端到端：真实浏览器（Chromium）→ 真实 uvicorn → 真实 PostgreSQL。

闭环：客户登录 → 提问看引用 → 售后资格 → 确认卡片 → 建单 → 客服领取 / 回复 / 推进 → 客户看结果并反馈。
权限与并发用 API 请求直接验证，不只看页面。
"""

from __future__ import annotations

import re
import threading

import httpx
from playwright.sync_api import Page, expect
from sqlalchemy import select

from app.models.ticket import Ticket, TicketEvent
from scripts.seed_db import DEMO_PASSWORD
from tests_e2e.helpers import api_headers, ui_login

ASK_POLICY = "这个耳机支持多久保修？"
ASK_REFUND = "A10001 买的耳机用了三天坏了，可以退款吗"


def _status(page: Page) -> str:
    return page.get_by_test_id("ticket-detail").get_by_test_id("status-pill").first.get_attribute("data-status") or ""


# ---------- 1. 客户：提问 → 引用 → 确认卡片 → 确认 → 工单列表 → 刷新仍停留在同一工单 ----------


def test_customer_chat_citation_confirm_ticket_and_refresh(page: Page, app_url: str, db) -> None:  # noqa: ANN001
    ui_login(page, app_url, "demo_customer", "#/chat")
    page.wait_for_url(re.compile(r"#/chat/[^/]+$"))
    expect(page.get_by_test_id("current-user")).to_contain_text("demo_customer")

    page.get_by_role("button", name=ASK_POLICY).click()
    answer = page.get_by_test_id("msg-assistant").last
    expect(answer.get_by_test_id("sources")).to_contain_text("《")

    page.get_by_role("button", name=ASK_REFUND).click()
    eligibility = page.get_by_test_id("msg-assistant").last.get_by_test_id("eligibility")
    expect(eligibility).to_contain_text("符合")
    expect(eligibility).to_contain_text("RETURN_WITHIN_7_DAYS")
    card = page.get_by_test_id("confirm-card")
    expect(card).to_be_visible()
    pending_id = card.get_attribute("data-pending-id")
    assert pending_id and pending_id.startswith("pa-")

    card.get_by_test_id("confirm-yes").click()
    expect(card).to_be_hidden()
    link = page.get_by_test_id("msg-assistant").last.get_by_test_id("ticket-link")
    expect(link).to_have_text(re.compile(r"^T\d+$"))
    ticket_id = link.inner_text()

    # 卡片确认复用了 graph 的确认路径：幂等键就是卡片上的 pending_action.id
    with db() as s:
        ticket = s.get(Ticket, ticket_id)
        assert ticket is not None and ticket.idempotency_key == pending_id and ticket.user_id == "U001"

    page.get_by_role("link", name="我的工单").click()
    row = page.locator(f"[data-testid=ticket-row][data-ticket-id='{ticket_id}']")
    expect(row).to_be_visible()
    row.get_by_role("link", name=ticket_id).click()
    page.wait_for_url(f"**/#/tickets/{ticket_id}")
    expect(page.get_by_test_id("ticket-title")).to_contain_text(ticket_id)

    page.reload()
    page.wait_for_url(f"**/#/tickets/{ticket_id}")
    expect(page.get_by_test_id("ticket-title")).to_contain_text(ticket_id)
    expect(page.locator("[data-testid=timeline-item][data-event=CREATED]")).to_have_count(1)


# ---------- 2. 客服：领取 → 回复 → 推进到 RESOLVED ----------


def test_support_claim_reply_resolve(page: Page, app_url: str) -> None:
    ui_login(page, app_url, "support_agent", "#/support/tickets?scope=unassigned")
    page.wait_for_url("**/#/support/tickets?scope=unassigned")
    row = page.locator("[data-testid=queue-row][data-ticket-id=T10001]")
    expect(row).to_be_visible()
    expect(page.locator("[data-testid=queue-row][data-ticket-id=T10002]")).to_have_count(0)  # 已有处理人

    row.get_by_test_id("claim").click()
    page.wait_for_url("**/#/support/tickets/T10001")
    expect(page.get_by_test_id("assignee")).to_contain_text("我")

    page.get_by_test_id("support-reply").locator("textarea").fill("您好，已安排检测，请寄回耳机。")
    page.get_by_test_id("support-reply-submit").click()
    expect(page.get_by_test_id("reply-content").last).to_have_text("您好，已安排检测，请寄回耳机。")

    page.get_by_test_id("advance").click()
    expect(page.get_by_test_id("advance")).to_have_attribute("data-target", "RESOLVED")
    page.get_by_test_id("advance").click()
    expect(page.get_by_test_id("advance")).to_have_attribute("data-target", "CLOSED")
    assert _status(page) == "RESOLVED"

    events = page.get_by_test_id("timeline-item").evaluate_all("els => els.map(e => e.dataset.event)")
    assert events == ["CREATED", "CLAIMED", "REPLIED", "STATUS_CHANGED", "STATUS_CHANGED"]

    page.reload()  # 刷新后仍是同一工单、同一状态
    expect(page.get_by_test_id("ticket-title")).to_contain_text("T10001")
    assert _status(page) == "RESOLVED"
    page.get_by_role("link", name="← 工单队列").click()
    page.get_by_test_id("scope-mine").click()
    expect(page.locator("[data-testid=queue-row][data-ticket-id=T10001]")).to_be_visible()


# ---------- 3. 客户：看到回复与状态 → 反馈 → 重复提交被拒 ----------


def test_customer_sees_reply_and_submits_feedback_once(page: Page, app_url: str) -> None:
    support = api_headers(app_url, "support_agent")
    base = f"{app_url}/api/support/tickets/T10001"
    assert httpx.post(f"{base}/claim", headers=support).status_code == 200
    reply = "检测完成，已为您更换新耳机。"
    assert httpx.post(f"{base}/replies", json={"content": reply}, headers=support).status_code == 201
    for target in ("PROCESSING", "RESOLVED"):
        assert httpx.patch(f"{base}/status", json={"status": target}, headers=support).status_code == 200

    ui_login(page, app_url, "demo_customer", "#/tickets/T10001")
    page.wait_for_url("**/#/tickets/T10001")
    expect(page.get_by_test_id("reply-content")).to_have_text(reply)
    assert _status(page) == "RESOLVED"
    expect(page.get_by_test_id("ticket-detail")).not_to_contain_text("SUPPORT001")  # 客户看不到客服账号

    # 第二个标签页先打开同一工单（此时表单仍可提交），用来验证重复提交在界面上被拒
    other = page.context.new_page()
    ui_login(other, app_url, "demo_customer", "#/tickets/T10001")
    expect(other.get_by_test_id("feedback-form")).to_be_visible()

    page.get_by_test_id("rate-5").click()
    page.get_by_test_id("feedback-form").locator("textarea").fill("处理很快")
    page.get_by_test_id("feedback-submit").click()
    expect(page.get_by_test_id("feedback-done")).to_contain_text("5 / 5")
    expect(page.locator("[data-testid=timeline-item][data-event=FEEDBACK_SUBMITTED]")).to_have_count(1)

    other.get_by_test_id("rate-1").click()
    other.get_by_test_id("feedback-submit").click()
    expect(other.get_by_test_id("feedback-error")).to_contain_text("不能重复提交")

    dup = httpx.post(f"{app_url}/api/tickets/T10001/feedback", json={"rating": 2},
                     headers=api_headers(app_url, "demo_customer"))
    assert dup.status_code == 409 and dup.json()["detail"]["type"] == "DUPLICATE"
    feedback = httpx.get(f"{app_url}/api/support/feedback", headers=support).json()
    assert [(f["ticket_id"], f["rating"], f["comment"]) for f in feedback] == [("T10001", 5, "处理很快")]


# ---------- 4. 权限：客户 A 打开客户 B 的工单链接 ----------


def test_customer_cannot_open_other_customers_ticket(page: Page, app_url: str) -> None:
    with page.expect_response(lambda r: r.url.endswith("/api/tickets/T10002")) as resp_info:
        ui_login(page, app_url, "demo_customer", "#/tickets/T10002")
    assert resp_info.value.status == 403
    page.wait_for_url("**/#/tickets/T10002")
    expect(page.get_by_test_id("access-denied")).to_be_visible()
    expect(page.locator("main")).not_to_contain_text("超期退款申请咨询")  # T10002 的标题不泄露


# ---------- 5. 权限：客户 token 调用客服接口 ----------


def test_customer_token_forbidden_on_support_api(page: Page, app_url: str) -> None:
    customer = api_headers(app_url, "demo_customer")
    calls = [
        ("GET", "/api/support/tickets", None),
        ("GET", "/api/support/tickets/T10001", None),
        ("POST", "/api/support/tickets/T10001/claim", None),
        ("PATCH", "/api/support/tickets/T10001/status", {"status": "PROCESSING"}),
        ("POST", "/api/support/tickets/T10001/replies", {"content": "冒充客服"}),
        ("GET", "/api/support/feedback", None),
    ]
    for method, path, body in calls:
        r = httpx.request(method, f"{app_url}{path}", json=body, headers=customer)
        assert r.status_code == 403, (method, path, r.status_code)
        assert r.json()["detail"]["type"] == "PERMISSION_DENIED"

    support = api_headers(app_url, "support_agent")
    detail = httpx.get(f"{app_url}/api/support/tickets/T10001", headers=support).json()
    assert detail["assignee_id"] is None and detail["status"] == "OPEN" and detail["replies"] == []

    # 体验层：客户在浏览器里打开客服页面会被带回客户首页（真正的拦截在上面的 403）
    ui_login(page, app_url, "demo_customer", "#/support/tickets")
    page.wait_for_url(re.compile(r"#/chat/[^/]+$"))


# ---------- 6. 并发：两个客服同时领取同一张工单 ----------


def _race_claim(app_url: str, ticket_id: str, agents: dict[str, dict[str, str]]) -> dict[str, httpx.Response]:
    """每个客服一个线程、一个独立连接，Barrier 对齐后同时发出领取请求。"""
    barrier = threading.Barrier(len(agents))
    results: dict[str, httpx.Response] = {}

    def claim(agent: str) -> None:
        with httpx.Client(timeout=10) as client:
            barrier.wait()
            results[agent] = client.post(f"{app_url}/api/support/tickets/{ticket_id}/claim", headers=agents[agent])

    threads = [threading.Thread(target=claim, args=(a,)) for a in agents]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_claim_only_one_wins(app_url: str, db) -> None:  # noqa: ANN001
    customer = api_headers(app_url, "demo_customer")
    agents = {"SUPPORT001": api_headers(app_url, "support_agent"), "SUPPORT002": api_headers(app_url, "support_agent2")}

    for round_no in range(5):  # 多轮新工单，降低偶然性
        created = httpx.post(
            f"{app_url}/api/tickets",
            json={"category": "OTHER", "title": f"并发领取 #{round_no}", "description": "e2e"},
            headers=customer,
        )
        assert created.status_code == 201, created.text
        ticket_id = created.json()["id"]

        results = _race_claim(app_url, ticket_id, agents)
        codes = sorted(r.status_code for r in results.values())
        assert codes == [200, 409], {a: (r.status_code, r.text) for a, r in results.items()}
        winner = next(a for a, r in results.items() if r.status_code == 200)
        loser = next(a for a, r in results.items() if r.status_code == 409)
        assert results[loser].json()["detail"]["type"] == "ALREADY_ASSIGNED"
        with db() as s:
            assert s.get(Ticket, ticket_id).assignee_id == winner
            claims = s.scalars(select(TicketEvent).where(TicketEvent.ticket_id == ticket_id,
                                                         TicketEvent.event_type == "CLAIMED")).all()
            assert [c.actor_id for c in claims] == [winner]


# ---------- 7. 会话失效：401 → 登录页 → 登录后回到原视图 ----------


def test_expired_token_redirects_to_login_and_back(page: Page, app_url: str) -> None:
    ui_login(page, app_url, "demo_customer", "#/tickets/T10001")
    page.wait_for_url("**/#/tickets/T10001")
    expect(page.get_by_test_id("ticket-title")).to_contain_text("T10001")

    # 模拟令牌过期/被篡改：后端返回 401，前端清会话并带着 next 跳转登录
    page.evaluate("""() => {
        const s = JSON.parse(sessionStorage.getItem('csagent.session'));
        s.token = s.token.slice(0, -4) + 'xxxx';
        sessionStorage.setItem('csagent.session', JSON.stringify(s));
    }""")
    page.reload()
    page.wait_for_url("**/#/login?next=*")
    page.fill("#login-username", "demo_customer")
    page.fill("#login-password", DEMO_PASSWORD)
    page.click("form.login-card button[type=submit]")
    page.wait_for_url("**/#/tickets/T10001")
    expect(page.get_by_test_id("ticket-title")).to_contain_text("T10001")
