"""评测集加载与结构校验。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DATASET_DIR = Path(__file__).resolve().parent / "datasets"

# 规格 §26（修订⑤）：110 条配比
EXPECTED_COUNTS: dict[str, int] = {
    "rag": 30,
    "order": 15,
    "logistics": 10,
    "ticket": 10,
    "multi_turn": 10,
    "abstention": 10,
    "injection": 10,
    "idor": 10,
    "tool_failure": 5,
}

VALID_OUTCOMES = {"SUCCESS", "REFUSED", "BLOCKED", "CLARIFY", "NOT_FOUND", "DUPLICATE"}


def load_category(name: str) -> list[dict[str, Any]]:
    path = DATASET_DIR / f"{name}.jsonl"
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def load_dataset() -> list[dict[str, Any]]:
    """加载全部 9 类并做结构校验（数量/唯一性/枚举）。"""
    all_cases: list[dict[str, Any]] = []
    for category, expected in EXPECTED_COUNTS.items():
        records = load_category(category)
        if len(records) != expected:
            raise ValueError(f"数据集 {category}: 期望 {expected} 条，实际 {len(records)}")
        for r in records:
            if r.get("category") != category:
                raise ValueError(f"{r.get('case_id')}: category 字段与文件不一致")
            if r.get("expected_outcome") not in VALID_OUTCOMES:
                raise ValueError(f"{r.get('case_id')}: 非法 expected_outcome")
            if "turns" not in r and not r.get("input"):
                raise ValueError(f"{r.get('case_id')}: 缺少 input 或 turns")
        all_cases.extend(records)
    ids = [c["case_id"] for c in all_cases]
    if len(ids) != len(set(ids)):
        raise ValueError("case_id 存在重复")
    if len(all_cases) != 110:
        raise ValueError(f"总数应为 110，实际 {len(all_cases)}")
    return all_cases
