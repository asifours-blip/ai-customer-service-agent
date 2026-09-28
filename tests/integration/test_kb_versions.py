"""知识库版本管理集成测试：真实 PostgreSQL + pgvector。

覆盖：导入失败隔离、非 ACTIVE 不可检索、并发发布只有一个成功、回滚、旧回答引用可追溯、
重启恢复（含真实进程被杀）、上传校验反例、非管理员 403、校验失败原因、启动初始化幂等、评测重置不碰知识库。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.kb.service import (
    KbConflictError,
    Retrieval,
    bootstrap_from_directory,
    create_draft,
    ingest_version,
    merge_with_active,
    publish,
    recover_interrupted,
)
from app.kb.smoke import load_smoke_config
from app.kb.validation import UploadedFile, validate_documents
from app.models.knowledge import KbAuditLog, KbChunk, KbVersion
from app.rag import FakeEmbedding, search
from tests.conftest import ROOT, TEST_DATABASE_URL, auth_headers

pytestmark = pytest.mark.integration

TRADEIN_QUERY = "以旧换新补贴政策是什么"
TRADEIN_MD = """---
document_id: doc-policy-tradein
document_name: 以旧换新政策
category: policies
policy_version: "2026.09"
---

# 以旧换新政策

## 补贴标准
以旧换新补贴政策：任意品牌旧耳机回收可抵扣 80 元，旧手表回收抵扣 150 元，补贴以抵扣券形式发放。

## 领取方式
在订单页选择以旧换新，旧机寄回检测通过后 3 个工作日内发放抵扣券。
""".encode()


def retrieval(embedder: Any = None) -> Retrieval:
    return Retrieval(embedder=embedder or FakeEmbedding(), threshold=0.22, smoke=load_smoke_config())


def new_version(factory, files: dict[str, bytes], *, mode: str = "merge", embedder: Any = None) -> KbVersion:  # noqa: ANN001
    """与上传接口同一路径：校验 → （合并）→ 草稿 → 导入。"""
    docs = validate_documents([UploadedFile(name, data) for name, data in files.items()])
    with factory() as s:
        if mode == "merge":
            docs = merge_with_active(s, docs)
        vid = create_draft(s, docs, source="UPLOAD", actor="KBADMIN001").id
    ingest_version(factory, vid, retrieval(embedder))
    with factory() as s:
        v = s.get(KbVersion, vid)
        assert v is not None
        return v


def hits(factory, query: str, *, version_id: int | None = None) -> list[tuple[int, str, str, float]]:  # noqa: ANN001
    with factory() as s:
        return [
            (h.version_id, h.document_id, h.chunk_id, h.score)
            for h in search(s, FakeEmbedding().embed_query(query), 5, version_id=version_id)
        ]


def status_of(factory, vid: int) -> str:  # noqa: ANN001
    with factory() as s:
        return str(s.scalar(select(KbVersion.status).where(KbVersion.id == vid)))


def active_ids(factory) -> list[int]:  # noqa: ANN001
    with factory() as s:
        return list(s.scalars(select(KbVersion.id).where(KbVersion.status == "ACTIVE")))


def activate(factory, vid: int, expected: int | None, *, rollback: bool = False) -> KbVersion:  # noqa: ANN001
    with factory() as s:
        return publish(s, vid, actor="KBADMIN001", expected_active_version_id=expected, rollback=rollback)


# ---------------------------------------------------------------- 初始状态


def test_fixture_bootstraps_v1_active_with_documents(db) -> None:  # noqa: ANN001
    with db() as s:
        v1 = s.get(KbVersion, 1)
        assert v1 is not None and v1.status == "ACTIVE" and v1.source == "BOOTSTRAP"
        assert v1.doc_count == 12 and v1.chunk_count and v1.chunk_count >= 60
        assert v1.embedding_backend == "fake"
        assert v1.checks and v1.checks["smoke"]["passed"] and v1.checks["embedding_dim"]["actual"] == 512
        actions = list(s.scalars(select(KbAuditLog.action).where(KbAuditLog.version_id == 1).order_by(KbAuditLog.id)))
        assert actions == ["CREATED", "INGEST_STARTED", "INGEST_READY", "ACTIVATED"]


def test_bootstrap_does_nothing_when_active_exists(db) -> None:  # noqa: ANN001
    result, version = bootstrap_from_directory(db, ROOT / "knowledge_base", retrieval())
    assert (result, version.id) == ("SKIPPED", 1)
    with db() as s:
        assert s.scalar(select(func.count()).select_from(KbVersion)) == 1


# ---------------------------------------------------------------- 失败隔离


def test_ingest_failure_keeps_active_retrieval_unchanged(db, client: TestClient, monkeypatch) -> None:  # noqa: ANN001
    admin = auth_headers(client, "kb_admin")
    before = hits(db, "耳机整机保修多久")
    assert before and before[0][0] == 1

    def boom(self, texts):  # noqa: ANN001
        raise RuntimeError("embedding 服务不可用（注入）")

    monkeypatch.setattr(FakeEmbedding, "embed_documents", boom)
    resp = client.post("/api/kb/versions", headers=admin, files=[("files", ("policy-tradein.md", TRADEIN_MD))])
    assert resp.status_code == 202, resp.text
    vid = resp.json()["id"]

    detail = client.get(f"/api/kb/versions/{vid}", headers=admin).json()  # TestClient 返回前后台任务已跑完
    assert detail["status"] == "FAILED"
    assert "RuntimeError" in detail["failure_reason"] and "embedding 服务不可用" in detail["failure_reason"]
    assert [a["action"] for a in detail["audit"]] == ["CREATED", "INGEST_STARTED", "INGEST_FAILED"]
    assert active_ids(db) == [1]
    assert hits(db, "耳机整机保修多久") == before  # 线上检索结果逐项不变
    with db() as s:
        assert s.scalar(select(func.count()).select_from(KbChunk).where(KbChunk.version_id == vid)) == 0


def test_smoke_check_failure_marks_failed_with_reason(db) -> None:  # noqa: ANN001
    # 完整替换成只有新文档的版本：冒烟集要求的保修/退款等文档都不在 → 必须 FAILED 并写明哪题没命中
    v = new_version(db, {"policy-tradein.md": TRADEIN_MD}, mode="replace")
    assert v.status == "FAILED"
    assert v.failure_reason and "冒烟查询「耳机整机保修多久」未命中 doc-policy-warranty" in v.failure_reason
    assert v.checks and v.checks["smoke"]["passed"] is False
    assert active_ids(db) == [1]


def test_embedding_dimension_check_marks_failed(db) -> None:  # noqa: ANN001
    v = new_version(db, {"policy-tradein.md": TRADEIN_MD}, embedder=FakeEmbedding(dim=384))
    assert v.status == "FAILED"
    assert v.failure_reason and "向量维度" in v.failure_reason and "384" in v.failure_reason
    assert v.checks and v.checks["embedding_dim"] == {"passed": False, "expected": 512, "actual": 384}


# ---------------------------------------------------------------- 只检索 ACTIVE


def test_non_active_versions_are_not_searchable(db) -> None:  # noqa: ANN001
    v2 = new_version(db, {"policy-tradein.md": TRADEIN_MD})
    assert v2.status == "READY"
    assert v2.doc_count == 13  # 合并：v1 的 12 篇 + 新增 1 篇
    # 内容确实在库里：指定版本检索能命中
    assert hits(db, TRADEIN_QUERY, version_id=v2.id)[0][1] == "doc-policy-tradein"
    for status in ("READY", "DRAFT", "INGESTING", "FAILED", "RETIRED"):
        with db() as s:
            s.execute(text("UPDATE kb_versions SET status = :st WHERE id = :id"), {"st": status, "id": v2.id})
            s.commit()
        found = hits(db, TRADEIN_QUERY)
        assert all(vid == 1 for vid, *_ in found), status
        assert "doc-policy-tradein" not in {doc for _, doc, *_ in found}, status


def test_database_rejects_second_active_version(db) -> None:  # noqa: ANN001
    v2 = new_version(db, {"policy-tradein.md": TRADEIN_MD})
    with db() as s, pytest.raises(IntegrityError):
        s.execute(text("UPDATE kb_versions SET status = 'ACTIVE' WHERE id = :id"), {"id": v2.id})
        s.commit()


# ---------------------------------------------------------------- 发布 / 并发 / 回滚


def test_concurrent_publish_only_one_succeeds(db) -> None:  # noqa: ANN001
    v2 = new_version(db, {"policy-tradein.md": TRADEIN_MD})
    v3 = new_version(db, {"policy-tradein.md": TRADEIN_MD.replace(b"80 ", b"90 ")})
    assert (v2.status, v3.status) == ("READY", "READY")

    barrier = threading.Barrier(2)
    outcomes: dict[int, str] = {}

    def worker(vid: int) -> None:
        barrier.wait()
        try:
            activate(db, vid, expected=1)  # 两个管理员都基于「v1 生效」发起发布
            outcomes[vid] = "ok"
        except KbConflictError as exc:
            outcomes[vid] = f"conflict: {exc.message}"

    threads = [threading.Thread(target=worker, args=(vid,)) for vid in (v2.id, v3.id)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    winners = [vid for vid, r in outcomes.items() if r == "ok"]
    assert len(winners) == 1, outcomes
    loser = ({v2.id, v3.id} - set(winners)).pop()
    assert "生效版本已变化" in outcomes[loser]
    assert active_ids(db) == winners
    assert status_of(db, 1) == "RETIRED"
    assert status_of(db, loser) == "READY"  # 失败方原样保留，可重新发布


def test_publish_requires_expected_active_and_valid_status(db) -> None:  # noqa: ANN001
    v2 = new_version(db, {"policy-tradein.md": TRADEIN_MD})
    with pytest.raises(KbConflictError, match="生效版本已变化"):
        activate(db, v2.id, expected=None)
    with pytest.raises(KbConflictError, match="只有 RETIRED 版本可以回滚"):
        activate(db, v2.id, expected=1, rollback=True)
    with pytest.raises(KbConflictError, match="只有 READY 版本可以发布"):
        activate(db, 1, expected=1)
    assert active_ids(db) == [1]


def test_activate_then_rollback_restores_previous_results(db) -> None:  # noqa: ANN001
    baseline = hits(db, TRADEIN_QUERY)
    assert "doc-policy-tradein" not in {doc for _, doc, *_ in baseline}

    v2 = new_version(db, {"policy-tradein.md": TRADEIN_MD})
    activate(db, v2.id, expected=1)
    after = hits(db, TRADEIN_QUERY)
    assert after[0][:2] == (v2.id, "doc-policy-tradein")

    activate(db, 1, expected=v2.id, rollback=True)
    assert active_ids(db) == [1]
    assert status_of(db, v2.id) == "RETIRED"
    assert hits(db, TRADEIN_QUERY) == baseline  # 检索结果逐项回到旧版本

    with db() as s:
        audit = [(a.version_id, a.action) for a in s.scalars(select(KbAuditLog).order_by(KbAuditLog.id))][-4:]
    assert audit == [(1, "RETIRED"), (v2.id, "ACTIVATED"), (v2.id, "RETIRED"), (1, "ROLLED_BACK")]


def test_publish_api_conflicts_and_admin_flow(db, client: TestClient) -> None:  # noqa: ANN001
    admin = auth_headers(client, "kb_admin")
    resp = client.post("/api/kb/versions", headers=admin, files=[("files", ("policy-tradein.md", TRADEIN_MD))])
    vid = resp.json()["id"]
    listing = client.get("/api/kb/versions", headers=admin).json()
    assert listing["active_version_id"] == 1
    assert [(v["id"], v["status"]) for v in listing["versions"]] == [(vid, "READY"), (1, "ACTIVE")]

    stale = client.post(f"/api/kb/versions/{vid}/activate", headers=admin, json={"expected_active_version_id": None})
    assert stale.status_code == 409 and stale.json()["detail"]["type"] == "KB_CONFLICT"
    ok = client.post(f"/api/kb/versions/{vid}/activate", headers=admin, json={"expected_active_version_id": 1})
    assert ok.status_code == 200 and ok.json()["status"] == "ACTIVE"
    back = client.post("/api/kb/versions/1/rollback", headers=admin, json={"expected_active_version_id": vid})
    assert back.status_code == 200 and back.json()["status"] == "ACTIVE"
    missing = client.post("/api/kb/versions/999/activate", headers=admin, json={"expected_active_version_id": 1})
    assert missing.status_code == 404


# ---------------------------------------------------------------- 引用追溯


WARRANTY_V2 = (ROOT / "knowledge_base" / "policies" / "policy-warranty.md").read_bytes().replace(
    "耳机类整机保修 12 个月".encode(), "耳机类整机保修 24 个月（2026.09 起延长）".encode()
)


def test_old_answer_citation_resolves_original_text_after_new_version(db, client: TestClient) -> None:  # noqa: ANN001
    customer = auth_headers(client, "demo_customer")
    ask = {"session_id": "kb-cite-1", "message": "这个耳机支持多久保修？"}
    first = client.post("/api/chat", headers=customer, json=ask)
    assert first.status_code == 200, first.text
    old = first.json()
    assert old["sources"] and {s["version_id"] for s in old["sources"]} == {"1"}

    admin = auth_headers(client, "kb_admin")
    up = client.post("/api/kb/versions", headers=admin, files=[("files", ("policy-warranty.md", WARRANTY_V2))])
    vid = up.json()["id"]
    assert client.get(f"/api/kb/versions/{vid}", headers=admin).json()["status"] == "READY"
    assert client.post(
        f"/api/kb/versions/{vid}/activate", headers=admin, json={"expected_active_version_id": 1}
    ).status_code == 200

    # 新回答引用新版本
    new = client.post("/api/chat", headers=customer, json=ask)
    assert {s["version_id"] for s in new.json()["sources"]} == {str(vid)}
    new_cites = client.get(f"/api/traces/{new.json()['trace_id']}/citations", headers=customer).json()
    assert any("24 个月" in (c["content"] or "") for c in new_cites)

    # 旧回答的引用：按 (v1, chunk_id) 取回当时原文，版本已 RETIRED 仍可查
    cites = client.get(f"/api/traces/{old['trace_id']}/citations", headers=customer).json()
    assert cites and all(c["found"] and c["version_id"] == 1 and c["version_status"] == "RETIRED" for c in cites)
    warranty = [c for c in cites if c["document"] == "保修政策" and c["section"] == "保修期限"]
    assert warranty and "耳机类整机保修 12 个月" in warranty[0]["content"]
    assert all("24 个月" not in (c["content"] or "") for c in cites)

    # 会话详情里的历史消息同样带着各自的版本号
    conv = client.get("/api/conversations", headers=customer).json()[0]
    msgs = client.get(f"/api/conversations/{conv['id']}", headers=customer).json()["messages"]
    versions = [{s["version_id"] for s in m["sources"]} for m in msgs if m["role"] == "assistant"]
    assert versions == [{"1"}, {str(vid)}]

    # 他人不能查看引用
    other = auth_headers(client, "second_customer")
    assert client.get(f"/api/traces/{old['trace_id']}/citations", headers=other).status_code == 403


def test_pre_versioning_citation_is_reported_not_guessed(db) -> None:  # noqa: ANN001
    from app.kb.service import resolve_citations

    with db() as s:
        legacy = {"document": "保修政策", "section": "保修期限", "chunk_id": "doc-policy-warranty#保修期限#0"}
        [c] = resolve_citations(s, [legacy])
    assert c["found"] is False and c["version_id"] is None and "早于知识库版本化" in c["reason"]


# ---------------------------------------------------------------- 重启恢复


def _draft(db) -> int:  # noqa: ANN001
    docs = merge_with_active_docs(db)
    with db() as s:
        return create_draft(s, docs, source="UPLOAD", actor="KBADMIN001").id


def merge_with_active_docs(db):  # noqa: ANN001, ANN201
    docs = validate_documents([UploadedFile("policy-tradein.md", TRADEIN_MD)])
    with db() as s:
        return merge_with_active(s, docs)


_HANGING_INGEST = """
import sys, time
from app.kb.service import Retrieval, ingest_version
from app.kb.smoke import load_smoke_config
from app.rag.embedding import FakeEmbedding
from app.services.database import SessionLocal

class Hang(FakeEmbedding):
    def embed_documents(self, texts):
        time.sleep(300)  # 模拟导入进行中；进程会在这里被强制结束
        return super().embed_documents(texts)

ingest_version(SessionLocal, int(sys.argv[1]), Retrieval(Hang(), 0.22, load_smoke_config()))
"""


def test_killed_ingest_process_is_recovered_as_failed(db) -> None:  # noqa: ANN001
    """真实进程在导入中被杀：存活时恢复逻辑不能动它；进程死后恢复逻辑把它标为 FAILED。"""
    vid = _draft(db)
    env = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL, "NO_PAID_API": "true", "EMBEDDING_BACKEND": "fake"}
    proc = subprocess.Popen([sys.executable, "-c", _HANGING_INGEST, str(vid)], cwd=ROOT, env=env)
    try:
        deadline = time.monotonic() + 30
        while status_of(db, vid) != "INGESTING":
            assert proc.poll() is None, "导入子进程提前退出"
            assert time.monotonic() < deadline, "子进程 30s 内未进入 INGESTING"
            time.sleep(0.2)
        assert recover_interrupted(db) == []  # 子进程仍持有导入锁：不误伤
        assert status_of(db, vid) == "INGESTING"
    finally:
        proc.kill()
        proc.wait(timeout=10)

    deadline = time.monotonic() + 30
    recovered: list[int] = []
    while not recovered:  # 数据库发现连接断开并释放锁需要一点时间
        recovered = recover_interrupted(db)
        assert time.monotonic() < deadline, "进程被杀 30s 后导入锁仍未释放"
        if not recovered:
            time.sleep(0.3)
    assert recovered == [vid]
    with db() as s:
        v = s.get(KbVersion, vid)
        assert v is not None and v.status == "FAILED"
        assert v.failure_reason and "服务重启时该版本仍处于 INGESTING" in v.failure_reason
        last = s.scalars(select(KbAuditLog).where(KbAuditLog.version_id == vid).order_by(KbAuditLog.id.desc())).first()
        assert last is not None and (last.action, last.from_status, last.to_status) == (
            "RECOVERED_AFTER_RESTART", "INGESTING", "FAILED"
        )
    assert active_ids(db) == [1]


def test_app_startup_recovers_stale_ingesting_versions(db) -> None:  # noqa: ANN001
    stale_ingesting, stale_draft = _draft(db), _draft_other(db)
    with db() as s:
        s.execute(text("UPDATE kb_versions SET status = 'INGESTING' WHERE id = :id"), {"id": stale_ingesting})
        s.commit()
    from app.main import app

    with TestClient(app):  # 触发 lifespan 启动钩子 = 进程启动
        pass
    assert status_of(db, stale_ingesting) == "FAILED"
    assert status_of(db, stale_draft) == "FAILED"
    assert status_of(db, 1) == "ACTIVE"


def _draft_other(db) -> int:  # noqa: ANN001
    docs = validate_documents([UploadedFile("policy-tradein.md", TRADEIN_MD.replace(b"80 ", b"70 "))])
    with db() as s:
        return create_draft(s, merge_with_active(s, docs), source="UPLOAD", actor="KBADMIN001").id


def test_recovered_version_is_not_ingested_later(db) -> None:  # noqa: ANN001
    vid = _draft(db)
    assert recover_interrupted(db) == [vid]
    assert ingest_version(db, vid, retrieval()) == "FAILED"  # 已被标记失败的版本不会再被导入
    with db() as s:
        assert s.scalar(select(func.count()).select_from(KbChunk).where(KbChunk.version_id == vid)) == 0


# ---------------------------------------------------------------- 上传校验与权限


def _upload(client: TestClient, headers: dict[str, str], files: list[tuple[str, bytes]], mode: str = "merge"):  # noqa: ANN202
    return client.post(
        "/api/kb/versions", headers=headers, data={"mode": mode}, files=[("files", (n, b)) for n, b in files]
    )


@pytest.mark.parametrize(
    "name,data,check",
    [
        ("notes.txt", TRADEIN_MD, "extension"),
        ("big.md", TRADEIN_MD + "字".encode() * 80_000, "size"),
        ("gbk.md", TRADEIN_MD.decode().encode("gbk"), "encoding"),
        ("plain.md", "# 标题\n\n## 节\n正文".encode(), "front_matter"),
        ("../evil.md", TRADEIN_MD, "path"),
        ("..\\evil.md", TRADEIN_MD, "path"),
        ("sub/evil.md", TRADEIN_MD, "path"),
    ],
    ids=["non-md", "oversize", "non-utf8", "no-front-matter", "dotdot-slash", "dotdot-backslash", "subdir"],
)
def test_upload_rejections_name_the_file_and_check(db, client: TestClient, name: str, data: bytes, check: str) -> None:  # noqa: ANN001
    admin = auth_headers(client, "kb_admin")
    resp = _upload(client, admin, [("ok.md", TRADEIN_MD.replace(b"doc-policy-tradein", b"doc-ok")), (name, data)])
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["type"] == "KB_UPLOAD_INVALID"
    assert detail["errors"] == [{"file": name, "check": check, "message": detail["errors"][0]["message"]}]
    assert name in detail["message"]
    with db() as s:  # 整体拒绝：不创建任何版本
        assert s.scalar(select(func.count()).select_from(KbVersion)) == 1


def test_merge_upload_rejects_path_taken_by_other_document(db, client: TestClient) -> None:  # noqa: ANN001
    admin = auth_headers(client, "kb_admin")
    # 路径 policies/policy-refund.md 已属于 doc-policy-refund
    clash = TRADEIN_MD.replace(b"doc-policy-tradein", b"doc-another")
    resp = _upload(client, admin, [("policy-refund.md", clash)])
    assert resp.status_code == 400
    assert resp.json()["detail"]["errors"][0]["check"] == "path"


@pytest.mark.parametrize("username", ["demo_customer", "support_agent"])
def test_non_admin_gets_403_on_every_kb_endpoint(db, client: TestClient, username: str) -> None:  # noqa: ANN001
    headers = auth_headers(client, username)
    body = {"expected_active_version_id": 1}
    responses = [
        client.get("/api/kb/versions", headers=headers),
        client.get("/api/kb/versions/1", headers=headers),
        _upload(client, headers, [("policy-tradein.md", TRADEIN_MD)]),
        client.post("/api/kb/versions/1/activate", headers=headers, json=body),
        client.post("/api/kb/versions/1/rollback", headers=headers, json=body),
    ]
    assert [r.status_code for r in responses] == [403] * 5
    assert all(r.json()["detail"]["type"] == "PERMISSION_DENIED" for r in responses)
    with db() as s:
        assert s.scalar(select(func.count()).select_from(KbVersion)) == 1
    assert client.get("/api/kb/versions").status_code == 401


# ---------------------------------------------------------------- 评测重置


def test_eval_reset_keeps_kb_and_resolves_directory_version(db) -> None:  # noqa: ANN001
    from eval.runner import reset_environment, resolve_kb_version

    v2 = new_version(db, {"policy-tradein.md": TRADEIN_MD})
    activate(db, v2.id, expected=1)
    with db() as s:
        before = s.execute(text("SELECT id, status, chunk_count FROM kb_versions ORDER BY id")).all()
        chunks = s.scalar(select(func.count()).select_from(KbChunk))
        s.execute(text("INSERT INTO conversations (user_id, session_id, title, created_at, updated_at) "
                       "VALUES ('U001', 'eval-junk', 'x', now(), now())"))
        s.commit()
        reset_environment(s)
    with db() as s:
        assert s.execute(text("SELECT id, status, chunk_count FROM kb_versions ORDER BY id")).all() == before
        assert s.scalar(select(func.count()).select_from(KbChunk)) == chunks
        assert s.scalar(text("SELECT count(*) FROM conversations WHERE session_id = 'eval-junk'")) == 0
        assert s.scalar(text("SELECT count(*) FROM users")) == 5  # 业务数据已重新 seed

    # dir：v1 内容与目录一致且同为 fake 后端 → 直接复用 v1，不新建版本、不改变 ACTIVE
    assert resolve_kb_version("dir", FakeEmbedding(), 0.22).id == 1
    assert resolve_kb_version("active", FakeEmbedding(), 0.22).id == v2.id
    assert resolve_kb_version("1", FakeEmbedding(), 0.22).status == "RETIRED"
    assert active_ids(db) == [v2.id]
    with pytest.raises(ValueError, match="只能评测"):
        resolve_kb_version(str(new_version(db, {"x.md": TRADEIN_MD}, mode="replace").id), FakeEmbedding(), 0.22)
    with pytest.raises(ValueError, match="不存在"):
        resolve_kb_version("999", FakeEmbedding(), 0.22)
