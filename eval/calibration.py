"""盲标校准：分层导出 → 人工标注 → Agreement/Cohen's κ/混淆矩阵/分歧清单。

规则（v1.1 补丁 §6）：Judge 不得提前看到人工标签；κ<0.70 不得发布 Judge 指标。
"""

from __future__ import annotations

import random
from typing import Any

# 分层配比：24 条覆盖全部类别（含各类失败形态）
STRATA: dict[str, int] = {
    "rag": 8,
    "order": 3,
    "logistics": 2,
    "ticket": 2,
    "multi_turn": 3,
    "abstention": 2,
    "injection": 2,
    "idor": 1,
    "tool_failure": 1,
}

CALIBRATION_TARGETS = {"agreement_min": 0.85, "kappa_min": 0.70}


def build_calibration_set(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """分层抽取 24 条（确定性：按 case_id 排序后取前 N，不随机）。"""
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for r in sorted(results, key=lambda x: x["case_id"]):
        by_cat.setdefault(r["category"], []).append(r)
    picked: list[dict[str, Any]] = []
    for cat, n in STRATA.items():
        picked.extend(by_cat.get(cat, [])[:n])
    return picked


def export_blind(picked: list[dict[str, Any]], source: str = "offline") -> dict[str, Any]:
    """导出盲标文件（不含任何 judge/期望信息）。

    source 标记答案来源：κ 校准要求人工与 Judge 评同一批答案，
    因此只有 source="live"（与 judge_scores.json 同一次运行）的导出可用于 --calibrate；
    "offline"（FakeLLM 答案）仅供熟悉评分标准（D-017）。
    """
    items = [
        {
            "item_id": f"cal-{i + 1:02d}",
            "case_id": r["case_id"],
            "question": r["final"]["input"],
            "answer": r["final"]["answer"],
        }
        for i, r in enumerate(picked)
    ]
    random.Random(42).shuffle(items)  # 打乱呈现顺序（固定种子，可复现）
    instructions = (
        "请独立为每条 answer 评分（0=错误/答非所问/编造，1=部分正确，2=正确），"
        '填写到每项的 human_score 字段（0/1/2）。Judge 尚未评分，请勿参考系统内部判定。'
        "事实核查来源（知识库清单、订单/工单事实表、拒答与越权判定方法）"
        "见 eval/calibration/LABELING_GUIDE.md。"
    )
    if source == "offline":
        instructions += "注意：本文件答案来自离线 FakeLLM，仅供熟悉评分标准；κ 校准必须使用 blind_export_live.json。"
    return {"version": "2026.08", "source": source, "instructions": instructions, "items": items}


def cohen_kappa(a: list[int], b: list[int]) -> float:
    """Cohen's κ（无权重）。"""
    if len(a) != len(b) or not a:
        return 0.0
    cats = sorted(set(a) | set(b))
    n = len(a)
    po = sum(1 for x, y in zip(a, b, strict=True) if x == y) / n
    pe = sum((a.count(c) / n) * (b.count(c) / n) for c in cats)
    if pe == 1.0:
        return 1.0 if po == 1.0 else 0.0
    return round((po - pe) / (1 - pe), 4)


def weighted_kappa(a: list[int], b: list[int]) -> float:
    """二次加权 κ（有序评分 0/1/2）。"""
    if len(a) != len(b) or not a:
        return 0.0
    cats = [0, 1, 2]
    n = len(a)
    k = len(cats)
    w = [[(i - j) ** 2 / (k - 1) ** 2 for j in cats] for i in cats]
    pa = [a.count(c) / n for c in cats]
    pb = [b.count(c) / n for c in cats]
    num = sum(w[i][j] * sum(1 for x, y in zip(a, b, strict=True) if x == cats[i] and y == cats[j]) / n
              for i in range(k) for j in range(k))
    den = sum(w[i][j] * pa[i] * pb[j] for i in range(k) for j in range(k))
    if den == 0:
        return 1.0
    return round(1 - num / den, 4)


def confusion_matrix(a: list[int], b: list[int]) -> list[list[int]]:
    cats = [0, 1, 2]
    return [[sum(1 for x, y in zip(a, b, strict=True) if x == i and y == j) for j in cats] for i in cats]


def calibrate(
    labeled: dict[str, Any], judge_scores: dict[str, Any]
) -> dict[str, Any]:
    """输入：盲标结果（含 human_score）+ judge 评分（含 score）。输出校准报告。"""
    if labeled.get("source") != "live":
        raise ValueError(
            "盲标文件 source != 'live'：κ 校准要求人工与 Judge 评同一批 live 答案，"
            "请标注 blind_export_live.json（live 运行导出）后重试（D-017）"
        )
    human_by_case = {it["case_id"]: int(it["human_score"]) for it in labeled["items"]}
    judge_by_case: dict[str, int] = {}
    judge_reason: dict[str, str] = {}
    for it in judge_scores["items"]:
        s = int(it["score"])
        if s >= 0:  # -1 为解析失败，剔除
            judge_by_case[it["case_id"]] = s
            judge_reason[it["case_id"]] = str(it.get("reason", ""))

    common = [cid for cid in human_by_case if cid in judge_by_case]
    a = [human_by_case[c] for c in common]
    b = [judge_by_case[c] for c in common]
    n = len(common)
    agreement = round(sum(1 for x, y in zip(a, b, strict=True) if x == y) / n, 4) if n else 0.0
    kappa = cohen_kappa(a, b)
    wkappa = weighted_kappa(a, b)
    disagreements = [
        {
            "case_id": c,
            "human_score": human_by_case[c],
            "judge_score": judge_by_case[c],
            "judge_reason": judge_reason.get(c, ""),
            "difference": human_by_case[c] - judge_by_case[c],
        }
        for c in common
        if human_by_case[c] != judge_by_case[c]
    ]
    ok = agreement >= CALIBRATION_TARGETS["agreement_min"] and kappa >= CALIBRATION_TARGETS["kappa_min"]
    return {
        "n": n,
        "agreement": agreement,
        "cohen_kappa": kappa,
        "weighted_kappa": wkappa,
        "confusion_matrix": confusion_matrix(a, b),
        "disagreements": disagreements,
        "targets": CALIBRATION_TARGETS,
        "judge_publishable": ok,
        "verdict": "PASS：Judge 指标可发布" if ok else "FAIL：κ 或 Agreement 未达标，须迭代 judge prompt 后重校准",
    }
