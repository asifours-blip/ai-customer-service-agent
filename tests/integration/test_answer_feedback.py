"""回答反馈：提交（唯一约束）→ 管理员审核队列 → 转评测用例 / 驳回 → 审计只追加。真实 PostgreSQL。"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

import eval.converted as converted_module
from tests.conftest import auth_headers

pytestmark = pytest.mark.integration


@pytest.fixture()
def h(client) -> dict[str, dict[str, str]]:
    return {
        "u1": auth_headers(client, "demo_customer"),
        "u2": auth_headers(client, "second_customer"),
        "s1": auth_headers(client, "support_agent"),
    }


@pytest.fixture(autouse=True)
def _isolate_converted_dataset(tmp_path, monkeypatch):
    """转评测用例会真的写文件：测试用临时目录，不污染仓库里的 eval/datasets/converted/。"""
    monkeypatch.setattr(converted_module, "CONVERTED_DIR", tmp_path / "converted")


def _assistant_message_id(client, headers, session: str, text_in: str) -> int:
    r = client.post("/api/chat", json={"session_id": session, "message": text_in}, headers=headers)
    assert r.status_code == 200, r.text
    conv_id = client.get("/api/conversations", headers=headers).json()[0]["id"]
    for conv in client.get("/api/conversations", headers=headers).json():
        if conv["session_id"] == session:
            conv_id = conv["id"]
            break
    detail = client.get(f"/api/conversations/{conv_id}", headers=headers).json()
    assistant_msgs = [m for m in detail["messages"] if m["role"] == "assistant"]
    assert assistant_msgs, detail
    return assistant_msgs[-1]["id"]


def test_submit_feedback_then_duplicate_rejected(client, h) -> None:
    mid = _assistant_message_id(client, h["u1"], "s-fb-1", "这个耳机支持多久保修？")
    r1 = client.post(f"/api/feedback/{mid}", json={"helpful": True, "note": "回答很准确"}, headers=h["u1"])
    assert r1.status_code == 200, r1.text
    assert r1.json()["review_status"] == "PENDING"

    dup = client.post(f"/api/feedback/{mid}", json={"helpful": False}, headers=h["u1"])
    assert dup.status_code == 409, dup.text


def test_feedback_only_owner_can_submit(client, h) -> None:
    mid = _assistant_message_id(client, h["u1"], "s-fb-owner", "帮我查一下订单 A10001")
    r = client.post(f"/api/feedback/{mid}", json={"helpful": True}, headers=h["u2"])
    assert r.status_code == 403, r.text


def test_customer_cannot_access_review_queue(client, h) -> None:
    assert client.get("/api/feedback/admin/queue", headers=h["u1"]).status_code == 403


def test_review_queue_shows_question_answer_citations_trace(client, h) -> None:
    mid = _assistant_message_id(client, h["u1"], "s-fb-queue", "这个耳机支持多久保修？")
    client.post(f"/api/feedback/{mid}", json={"helpful": False, "note": "答案没说清楚天数"}, headers=h["u1"])

    queue = client.get("/api/feedback/admin/queue", headers=h["s1"]).json()
    item = next(i for i in queue if i["feedback"]["message_id"] == mid)
    assert item["question"] == "这个耳机支持多久保修？"
    assert item["answer"]
    assert item["trace_id"]
    assert item["route"]
    assert item["citations"], "RAG 回答的审核队列条目应带引用"


def test_convert_writes_new_versioned_file_without_touching_fixed_110(client, h) -> None:
    """转评测用例必须写进独立版本文件，不能修改 eval/datasets/*.jsonl 里那 110 条固定数据。"""
    from eval.loader import load_dataset

    before_110 = load_dataset()  # 若固定数据集被改动，这里会先炸

    mid = _assistant_message_id(client, h["u1"], "s-fb-convert", "这个耳机支持多久保修？")
    fb = client.post(f"/api/feedback/{mid}", json={"helpful": True}, headers=h["u1"]).json()

    conv_path = converted_module.version_path("v1")
    before_lines = conv_path.read_text(encoding="utf-8").splitlines() if conv_path.exists() else []

    r = client.post(
        f"/api/feedback/admin/{fb['id']}/convert",
        json={"expected_outcome": "SUCCESS", "expected_document": "保修政策", "category": "rag"},
        headers=h["s1"],
    )
    assert r.status_code == 200, r.text
    assert r.json()["review_status"] == "CONVERTED"

    after_110 = load_dataset()
    assert after_110 == before_110, "固定 110 条数据集不应被审核转出流程改动"

    after_lines = conv_path.read_text(encoding="utf-8").splitlines()
    assert len(after_lines) == len(before_lines) + 1
    new_case = json.loads(after_lines[-1])
    assert new_case["category"] == "rag"
    assert new_case["expected_outcome"] == "SUCCESS"
    assert new_case["expected_document"] == "保修政策"
    assert new_case["input"] == "这个耳机支持多久保修？"
    assert new_case["source"]["feedback_id"] == fb["id"]

    # 已审核的反馈不能重复审核
    again = client.post(
        f"/api/feedback/admin/{fb['id']}/reject",
        headers=h["s1"],
    )
    assert again.status_code == 409


def test_reject_does_not_write_eval_case(client, h) -> None:
    mid = _assistant_message_id(client, h["u1"], "s-fb-reject", "帮我查一下订单 A10001")
    fb = client.post(f"/api/feedback/{mid}", json={"helpful": False, "note": "答非所问"}, headers=h["u1"]).json()

    conv_path = converted_module.version_path("v1")
    before_lines = conv_path.read_text(encoding="utf-8").splitlines() if conv_path.exists() else []

    r = client.post(f"/api/feedback/admin/{fb['id']}/reject", headers=h["s1"])
    assert r.status_code == 200, r.text
    assert r.json()["review_status"] == "REJECTED"

    after_lines = conv_path.read_text(encoding="utf-8").splitlines() if conv_path.exists() else []
    assert after_lines == before_lines, "驳回不应写入评测数据文件"


def test_review_audit_records_action_and_is_append_only(client, h, db) -> None:
    mid = _assistant_message_id(client, h["u1"], "s-fb-audit", "帮我查一下订单 A10001")
    fb = client.post(f"/api/feedback/{mid}", json={"helpful": False}, headers=h["u1"]).json()
    client.post(f"/api/feedback/admin/{fb['id']}/reject", headers=h["s1"])

    with db() as s:
        rows = s.execute(
            text("SELECT action, actor FROM feedback_review_audit WHERE feedback_id = :fid"), {"fid": fb["id"]}
        ).all()
        assert rows and rows[0].action == "REJECT"

        with pytest.raises(Exception, match="append-only"):
            s.execute(
                text("UPDATE feedback_review_audit SET action = 'CONVERT' WHERE feedback_id = :fid"),
                {"fid": fb["id"]},
            )
        s.rollback()
        with pytest.raises(Exception, match="append-only"):
            s.execute(text("DELETE FROM feedback_review_audit WHERE feedback_id = :fid"), {"fid": fb["id"]})
        s.rollback()
