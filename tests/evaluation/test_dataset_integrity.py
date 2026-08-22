"""评测集完整性：110 条 / 配比 / 枚举 / 唯一性 / 引用合法。纯单测。"""

from __future__ import annotations

import re

from eval.loader import EXPECTED_COUNTS, load_category, load_dataset

SEED_ORDERS_U001 = {"A10001", "A10002", "A10003", "A10004"}
SEED_ORDERS_U002 = {"A20001", "A20002"}
SEED_TICKETS = {"T10001", "T10002"}
GHOST_IDS = {"A99999", "A12345", "T99999"}  # 故意不存在（NOT_FOUND 用例）


def test_total_and_counts() -> None:
    cases = load_dataset()
    assert len(cases) == 110
    by: dict[str, int] = {}
    for c in cases:
        by[c["category"]] = by.get(c["category"], 0) + 1
    assert by == EXPECTED_COUNTS


def test_case_ids_unique() -> None:
    cases = load_dataset()
    ids = [c["case_id"] for c in cases]
    assert len(ids) == len(set(ids))


def test_users_valid() -> None:
    assert all(c["user_id"] in {"U001", "U002"} for c in load_dataset())


def test_order_ticket_references_valid() -> None:

    ok = SEED_ORDERS_U001 | SEED_ORDERS_U002 | GHOST_IDS
    for c in load_dataset():
        inputs = " ".join(t.get("input", "") for t in c.get("turns", [{"input": c.get("input", "")}]))
        for oid in re.findall(r"\b[AT]\d{5}\b", inputs):
            assert oid in ok | SEED_TICKETS, f"{c['case_id']}: 引用了未知 ID {oid}"


def test_idor_users_cross_reference() -> None:
    """IDOR 集：查询者绝不能是目标订单的属主。"""

    for c in load_category("idor"):
        text = c["input"]
        ids = re.findall(r"\bA\d{5}\b", text)
        for oid in ids:
            owner = "U001" if oid in SEED_ORDERS_U001 else "U002"
            assert owner != c["user_id"], f"{c['case_id']}: {oid} 属于 {owner}，不能由 {c['user_id']} 查询"


def test_rag_expected_documents_exist_in_kb() -> None:
    from pathlib import Path

    from app.rag import load_corpus

    kb_names = {d.document_name for d in load_corpus(Path("knowledge_base"))}
    for c in load_category("rag"):
        assert c["expected_document"] in kb_names, f"{c['case_id']}: 期望文档不在知识库"


def test_injection_all_blocked_expectation() -> None:
    assert all(c["expected_outcome"] == "BLOCKED" for c in load_category("injection"))


def test_abstention_all_refused_expectation() -> None:
    assert all(c["expected_outcome"] == "REFUSED" for c in load_category("abstention"))


def test_multi_turn_have_turns() -> None:
    for c in load_category("multi_turn"):
        assert 2 <= len(c["turns"]) <= 4
