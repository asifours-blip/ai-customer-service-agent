"""知识库冒烟查询集：放在 knowledge_base/ 旁边的 kb_smoke_queries.yaml。

每个版本 READY 之前逐题检索该版本：期望文档必须出现在 top_k 内，且分数达到线上拒答阈值
（否则线上 RAG 会拒答，等于没命中）。任一题不通过，版本判 FAILED。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

SMOKE_PATH = Path(__file__).resolve().parents[2] / "kb_smoke_queries.yaml"


@dataclass(frozen=True)
class SmokeQuery:
    query: str
    expect_document_id: str


@dataclass(frozen=True)
class SmokeConfig:
    top_k: int
    queries: tuple[SmokeQuery, ...]


def load_smoke_config(path: Path | None = None) -> SmokeConfig:
    data = yaml.safe_load((path or SMOKE_PATH).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("queries"), list) or not data["queries"]:
        raise ValueError("冒烟查询配置需要非空的 queries 列表")
    queries: list[SmokeQuery] = []
    for i, item in enumerate(data["queries"]):
        if not isinstance(item, dict) or not item.get("query") or not item.get("expect_document_id"):
            raise ValueError(f"冒烟查询第 {i + 1} 题缺少 query 或 expect_document_id")
        queries.append(SmokeQuery(str(item["query"]), str(item["expect_document_id"])))
    top_k = int(data.get("top_k", 3))
    if top_k < 1:
        raise ValueError("top_k 必须 ≥ 1")
    return SmokeConfig(top_k=top_k, queries=tuple(queries))
