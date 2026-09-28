"""知识库版本对比：逐题差异计算。纯单测（不连数据库）。"""

from __future__ import annotations

import pytest

from eval.kb_diff import diff_case, diff_results


def _result(case_id: str, *, outcome: str, abstained: bool, answer: str, sources: list[dict] | None = None) -> dict:
    return {
        "case_id": case_id,
        "category": "rag",
        "actual_outcome": outcome,
        "final": {
            "input": "问题",
            "answer": answer,
            "abstained": abstained,
            "sources": sources or [],
        },
    }


def test_identical_results_are_not_changed() -> None:
    a = _result("rag_001", outcome="SUCCESS", abstained=False, answer="12 个月", sources=[{"document": "保修政策"}])
    b = _result("rag_001", outcome="SUCCESS", abstained=False, answer="12 个月", sources=[{"document": "保修政策"}])
    d = diff_case(a, b)
    assert d["changed"] is False
    assert d["changed_fields"] == []


def test_outcome_and_citation_change_detected() -> None:
    a = _result(
        "rag_025", outcome="SUCCESS", abstained=False, answer="5-7 个工作日", sources=[{"document": "保修政策"}]
    )
    b = _result(
        "rag_025", outcome="REFUSED", abstained=True, answer="当前知识库中没有足够信息回答这个问题。", sources=[]
    )
    d = diff_case(a, b)
    assert d["changed"] is True
    assert set(d["changed_fields"]) == {"outcome", "abstained", "citations", "answer"}
    assert d["a"]["documents"] == ["保修政策"]
    assert d["b"]["documents"] == []


def test_citation_set_ignores_order_and_duplicates() -> None:
    docs_ab = [{"document": "A"}, {"document": "B"}]
    docs_ba = [{"document": "B"}, {"document": "A"}]
    a = _result("rag_002", outcome="SUCCESS", abstained=False, answer="x", sources=docs_ab)
    b = _result("rag_002", outcome="SUCCESS", abstained=False, answer="x", sources=docs_ba)
    d = diff_case(a, b)
    assert d["changed"] is False


def test_diff_results_summarizes_and_sorts_by_case_id() -> None:
    results_a = [
        _result("rag_002", outcome="SUCCESS", abstained=False, answer="x"),
        _result("rag_001", outcome="SUCCESS", abstained=False, answer="y"),
    ]
    results_b = [
        _result("rag_002", outcome="SUCCESS", abstained=False, answer="x"),
        _result("rag_001", outcome="REFUSED", abstained=True, answer="没有足够信息"),
    ]
    out = diff_results(results_a, results_b)
    assert out["total"] == 2
    assert out["changed_count"] == 1
    assert out["unchanged_count"] == 1
    assert [c["case_id"] for c in out["cases"]] == ["rag_001", "rag_002"]


def test_diff_results_rejects_mismatched_case_sets() -> None:
    results_a = [_result("rag_001", outcome="SUCCESS", abstained=False, answer="x")]
    results_b = [_result("rag_002", outcome="SUCCESS", abstained=False, answer="x")]
    with pytest.raises(ValueError, match="case 集合不一致"):
        diff_results(results_a, results_b)
