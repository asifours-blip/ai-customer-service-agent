"""从客户反馈审核转出的评测用例：独立于固定 110 条数据集（eval/loader.py）。

不参与 EXPECTED_COUNTS 校验、不改动 eval/datasets/*.jsonl 里任何一行——那 110 条的条数是
硬编码校验的（见 eval/loader.py: load_dataset）。审核通过的用例按版本号写进
eval/datasets/converted/<version>.jsonl，同一版本只追加、不修改已写入的行；
需要变更历史用例时新开一个版本文件，不回改旧版本。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONVERTED_DIR = Path(__file__).resolve().parent / "datasets" / "converted"
CURRENT_VERSION = "v1"


def version_path(version: str = CURRENT_VERSION) -> Path:
    return CONVERTED_DIR / f"{version}.jsonl"


def append_case(case: dict[str, Any], *, version: str = CURRENT_VERSION) -> None:
    """把一条审核通过的反馈用例追加进指定版本文件（只追加，不改动已有行）。"""
    CONVERTED_DIR.mkdir(parents=True, exist_ok=True)
    path = version_path(version)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(case, ensure_ascii=False) + "\n")


def load_version(version: str = CURRENT_VERSION) -> list[dict[str, Any]]:
    path = version_path(version)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def next_case_id(version: str = CURRENT_VERSION) -> str:
    existing = load_version(version)
    return f"fb_{version}_{len(existing) + 1:03d}"
