"""备份恢复演练：在测试库里造出一份「完整状态」快照（本脚本不是交付物，只是演练用一次性工具）。

覆盖 docs/backup-restore-drill.md 要求的场景：
  - 会话、工单及其时间线、反馈
  - 多个知识库版本（含 RETIRED）与引用
  - 一条待确认操作（未确认）
  - 一条已确认的售后申请（用于恢复后验证幂等键不会重复开单）
  - 一张已领取的工单（种子数据自带 T10002）

用法：
  DATABASE_URL=postgresql+psycopg://app:app@localhost:55432/agent_cs_test \
  TEST_DATABASE_URL=postgresql+psycopg://app:app@localhost:55432/agent_cs_test \
    .venv/Scripts/python scripts/_drill_seed.py
"""

from __future__ import annotations

import io
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("NO_PAID_API", "true")
os.environ.setdefault("EMBEDDING_BACKEND", "fake")

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402


def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    engine = create_engine(database_url)

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(cfg, "head")

    from app.models import Base

    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE kb_audit_log, kb_chunks, kb_documents, kb_versions RESTART IDENTITY"))
        for table in reversed(Base.metadata.sorted_tables):
            if table.name in ("ticket_events", "feedback_review_audit"):
                conn.execute(text(f"TRUNCATE TABLE {table.name}"))
            else:
                conn.execute(table.delete())

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    import app.services.database as database_module

    database_module.SessionLocal = factory

    from scripts.seed_db import seed

    seed.__globals__["SessionLocal"] = factory
    seed()

    from app.kb.service import Retrieval, bootstrap_from_directory
    from app.kb.smoke import load_smoke_config
    from app.rag import FakeEmbedding

    retrieval = Retrieval(embedder=FakeEmbedding(), threshold=0.22, smoke=load_smoke_config())
    result, _ = bootstrap_from_directory(factory, ROOT / "knowledge_base", retrieval)
    assert result == "ACTIVATED", result
    print(f"[drill] kb bootstrap: {result}")

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:

        def login(username: str, password: str = "demo123") -> dict[str, str]:
            r = client.post("/api/auth/login", json={"username": username, "password": password})
            assert r.status_code == 200, r.text
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        u1 = login("demo_customer")
        kb_admin = login("kb_admin")

        # --- 会话 + RAG 引用（citations） ---
        r = client.post(
            "/api/chat", json={"session_id": "drill-session-1", "message": "这个耳机支持多久保修？"}, headers=u1
        )
        assert r.status_code == 200, r.text
        print("[drill] chat with citation ok, trace_id =", r.json()["trace_id"])

        # --- 反馈 ---
        conversations = client.get("/api/conversations", headers=u1).json()
        conv_id = next(c["id"] for c in conversations if c["session_id"] == "drill-session-1")
        detail = client.get(f"/api/conversations/{conv_id}", headers=u1).json()
        assistant_mid = next(m["id"] for m in detail["messages"] if m["role"] == "assistant")
        fb = client.post(f"/api/feedback/{assistant_mid}", json={"helpful": True, "note": "回答准确"}, headers=u1)
        assert fb.status_code == 200, fb.text
        print("[drill] feedback submitted, id =", fb.json()["id"])

        # --- 一条待确认操作（不确认，留给恢复后验证） ---
        r = client.post(
            "/api/chat",
            json={"session_id": "drill-pending", "message": "A10001 买的耳机用了三天坏了，可以退款吗"},
            headers=u1,
        )
        assert r.status_code == 200 and "符合申请条件" in r.json()["answer"], r.text
        print("[drill] pending after-sales action created (未确认)")

        # --- 一条已确认的售后申请（用于验证恢复后同一幂等键不会重复开单）---
        # 用 EXCHANGE 类目、同一订单 A10001：与上面 drill-pending 的 REFUND 类目不冲突
        # （dedup 规则是同 order+category+OPEN 才算重复，见 app/services/tickets.py create_ticket）
        r = client.post(
            "/api/chat", json={"session_id": "drill-confirmed", "message": "A10001 耳机想换一个新的"}, headers=u1
        )
        assert r.status_code == 200 and "符合申请条件" in r.json()["answer"], r.text
        r2 = client.post("/api/chat", json={"session_id": "drill-confirmed", "message": "确认"}, headers=u1)
        assert r2.status_code == 200 and "已为您创建售后工单" in r2.json()["answer"], r2.text
        print("[drill] confirmed after-sales ticket created:", r2.json()["answer"][:60])

        # --- 新知识库版本（v2）：v1 RETIRED，v2 ACTIVE ---
        warranty_path = ROOT / "knowledge_base" / "policies" / "policy-warranty.md"
        content = warranty_path.read_text(encoding="utf-8") + "\n\n<!-- drill v2 marker -->\n"
        files = {"files": ("policy-warranty.md", io.BytesIO(content.encode("utf-8")), "text/markdown")}
        up = client.post("/api/kb/versions?mode=merge", files=files, headers=kb_admin)
        assert up.status_code == 202, up.text
        v2_id = up.json()["id"]
        for _ in range(50):
            v = client.get(f"/api/kb/versions/{v2_id}", headers=kb_admin).json()
            if v["status"] in ("READY", "FAILED"):
                break
            time.sleep(0.2)
        assert v["status"] == "READY", v
        versions = client.get("/api/kb/versions", headers=kb_admin).json()["versions"]
        active_id = next(vv["id"] for vv in versions if vv["status"] == "ACTIVE")
        pub = client.post(
            f"/api/kb/versions/{v2_id}/activate", json={"expected_active_version_id": active_id}, headers=kb_admin
        )
        assert pub.status_code == 200, pub.text
        print(f"[drill] kb v{v2_id} ACTIVE, v{active_id} RETIRED")

    print("[drill] done.")


if __name__ == "__main__":
    main()
