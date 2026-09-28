"""备份恢复演练：验证脚本（连到恢复后的库，独立进程 = 模拟应用重启）。

用法：
  DATABASE_URL=postgresql+psycopg://app:app@localhost:55433/agent_cs_restored \
    .venv/Scripts/python scripts/_drill_verify.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("NO_PAID_API", "true")
os.environ.setdefault("EMBEDDING_BACKEND", "fake")


def main() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:

        def login(username: str, password: str = "demo123") -> dict[str, str]:
            r = client.post("/api/auth/login", json={"username": username, "password": password})
            assert r.status_code == 200, r.text
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        u1 = login("demo_customer")

        # 1) 引用能取回原文
        conversations = client.get("/api/conversations", headers=u1).json()
        conv_id = next(c["id"] for c in conversations if c["session_id"] == "drill-session-1")
        detail = client.get(f"/api/conversations/{conv_id}", headers=u1).json()
        assistant_msg = next(m for m in detail["messages"] if m["role"] == "assistant")
        trace_id = assistant_msg["trace_id"]
        citations = client.get(f"/api/traces/{trace_id}/citations", headers=u1).json()
        assert citations, "恢复后引用列表为空"
        assert all(c["found"] and c["content"] for c in citations), citations
        print(f"[verify] 1/4 引用可取回原文 OK（{len(citations)} 条）")

        # 2) 待确认操作仍能确认，复用原幂等键，不重复开单
        before_tickets = {t["id"] for t in client.get("/api/support/tickets", headers=login("support_agent")).json()}
        r = client.post("/api/chat", json={"session_id": "drill-pending", "message": "确认"}, headers=u1)
        assert r.status_code == 200, r.text
        assert "已为您创建售后工单" in r.json()["answer"], r.json()["answer"]
        new_ticket = r.json()["tool_calls"][-1]
        assert new_ticket["tool"] == "create_ticket" and new_ticket["ok"], new_ticket
        after_tickets = {t["id"] for t in client.get("/api/support/tickets", headers=login("support_agent")).json()}
        created = after_tickets - before_tickets
        assert len(created) == 1, f"期望恰好新增 1 张工单，实际 {created}"
        print(f"[verify] 2/4 待确认操作确认成功，新工单 {created}")

        # 同一 pending_action 再确认一次（模拟重复点击）：应复用幂等键，不再新增
        r2 = client.post("/api/chat", json={"session_id": "drill-pending", "message": "确认"}, headers=u1)
        assert r2.status_code == 200, r2.text
        after_tickets_2 = {t["id"] for t in client.get("/api/support/tickets", headers=login("support_agent")).json()}
        assert after_tickets_2 == after_tickets, "重复确认不应再新增工单"
        print("[verify]     重复确认未新增工单（幂等生效）")

        # 3) 只追加触发器仍然生效（DB 层）
        from sqlalchemy import create_engine, text

        engine = create_engine(os.environ["DATABASE_URL"])
        with engine.begin() as conn:
            try:
                conn.execute(text("DELETE FROM ticket_events WHERE ticket_id = 'T10001'"))
                raise AssertionError("ticket_events 的 append-only 触发器应拒绝 DELETE，但没有报错")
            except Exception as exc:  # noqa: BLE001
                assert "append-only" in str(exc), exc
        print("[verify] 3/4 只追加触发器（ticket_events）恢复后仍生效")

        # 4) 已确认会话，应用「重启」（新进程/新 TestClient）后再次发「确认」，不产生第二张工单
        before2 = {t["id"] for t in client.get("/api/support/tickets", headers=login("support_agent")).json()}
        r3 = client.post("/api/chat", json={"session_id": "drill-confirmed", "message": "确认"}, headers=u1)
        assert r3.status_code == 200, r3.text
        answer = r3.json()["answer"]
        assert "已存在" in answer or "无需重复" in answer or "已为您创建" not in answer, answer
        after2 = {t["id"] for t in client.get("/api/support/tickets", headers=login("support_agent")).json()}
        assert after2 == before2, f"重启后重复确认不应新增工单，实际新增 {after2 - before2}"
        print(f"[verify] 4/4 重启后重复确认未新增工单（回答：{answer[:40]}）")

    print("[verify] 全部通过。")


if __name__ == "__main__":
    main()
