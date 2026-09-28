"""知识库版本对比：复用评测执行器，让同一组样本分别在版本 A / B 上跑，输出逐题差异报告。

设计：
- 两次运行共用同一份评测 case 列表与同一个 Agent 装配方式（build_agent，只是 version_id 不同），
  每次运行前都 reset_environment（业务表清空重建，知识库表不动，与 scripts/run_eval.py 完全一致）。
- 只比较「同一 case_id」在两个版本下的最终结果：outcome、是否拒答、引用的文档名集合、
  final answer 文本是否相同。逐题差异 + 汇总计数一起写进报告。
- 不改评测集加载逻辑、不改 eval/loader.py 的 110 条校验；两个版本必须都能通过
  eval.runner.resolve_kb_version 的校验（状态 READY/ACTIVE/RETIRED，向量后端一致）。
"""

from __future__ import annotations

from typing import Any


def _doc_set(sources: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(sorted({str(s.get("document")) for s in sources if s.get("document")}))


def diff_case(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """比较同一 case 在版本 A / B 下的执行结果，返回差异摘要（不含原始逐轮细节，报告体积可控）。"""
    fa, fb = a["final"], b["final"]
    docs_a, docs_b = _doc_set(fa.get("sources", [])), _doc_set(fb.get("sources", []))
    changed = {
        "outcome": a["actual_outcome"] != b["actual_outcome"],
        "abstained": bool(fa.get("abstained")) != bool(fb.get("abstained")),
        "citations": docs_a != docs_b,
        "answer": fa.get("answer") != fb.get("answer"),
    }
    return {
        "case_id": a["case_id"],
        "category": a["category"],
        "input": fa.get("input"),
        "changed": any(changed.values()),
        "changed_fields": [k for k, v in changed.items() if v],
        "a": {
            "outcome": a["actual_outcome"],
            "abstained": bool(fa.get("abstained")),
            "documents": list(docs_a),
            "answer": fa.get("answer"),
        },
        "b": {
            "outcome": b["actual_outcome"],
            "abstained": bool(fb.get("abstained")),
            "documents": list(docs_b),
            "answer": fb.get("answer"),
        },
    }


def diff_results(results_a: list[dict[str, Any]], results_b: list[dict[str, Any]]) -> dict[str, Any]:
    """按 case_id 对齐两组评测结果并逐题比较；case 集合不一致时报错（两次必须跑同一组样本）。"""
    by_id_a = {r["case_id"]: r for r in results_a}
    by_id_b = {r["case_id"]: r for r in results_b}
    if by_id_a.keys() != by_id_b.keys():
        only_a = sorted(by_id_a.keys() - by_id_b.keys())
        only_b = sorted(by_id_b.keys() - by_id_a.keys())
        raise ValueError(f"两次运行的 case 集合不一致：仅 A 有 {only_a}，仅 B 有 {only_b}")
    cases = [diff_case(by_id_a[cid], by_id_b[cid]) for cid in sorted(by_id_a)]
    changed = [c for c in cases if c["changed"]]
    return {
        "total": len(cases),
        "changed_count": len(changed),
        "unchanged_count": len(cases) - len(changed),
        "cases": cases,
    }
