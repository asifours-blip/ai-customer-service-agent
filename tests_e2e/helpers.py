"""E2E 公共操作：API 登录取 token、浏览器内登录。口令取自种子脚本，不在测试里重复写。"""

from __future__ import annotations

import httpx
from playwright.sync_api import Page

from scripts.seed_db import DEMO_PASSWORD


def api_headers(base: str, username: str) -> dict[str, str]:
    resp = httpx.post(f"{base}/api/auth/login", json={"username": username, "password": DEMO_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def ui_login(page: Page, base: str, username: str, target_hash: str) -> None:
    """直接打开目标链接 → 被重定向到登录页 → 登录后应回到目标链接。"""
    page.goto(f"{base}/{target_hash}")
    page.wait_for_url("**/#/login?next=*")
    page.fill("#login-username", username)
    page.fill("#login-password", DEMO_PASSWORD)
    page.click("form.login-card button[type=submit]")
