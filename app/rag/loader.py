"""知识库文档加载：解析 front-matter（document_id/name/category/policy_version）+ 正文分节。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
_FIELD = re.compile(r"^(\w+):\s*\"?([^\"\n]*)\"?\s*$", re.M)

REQUIRED_FIELDS = ("document_id", "document_name", "category", "policy_version")


@dataclass(frozen=True)
class KnowledgeDocument:
    document_id: str
    document_name: str
    category: str
    policy_version: str
    sections: list[tuple[str, str]]  # (heading, body)


def parse_front_matter(text: str) -> tuple[dict[str, str] | None, str]:
    """返回 (字段, 正文)；没有 front matter 时字段为 None（区别于「有 front matter 但缺字段」）。"""
    m = _FRONT_MATTER.match(text)
    if m is None:
        return None, text
    return dict(_FIELD.findall(m.group(1))), text[m.end() :]


def _split_sections(body: str) -> list[tuple[str, str]]:
    parts = re.split(r"^##\s+(.*)$", body, flags=re.M)
    # parts: [前言, h1, body1, h2, body2, ...]
    sections: list[tuple[str, str]] = []
    if parts and parts[0].strip():
        sections.append(("概述", parts[0].strip()))
    for i in range(1, len(parts) - 1, 2):
        sections.append((parts[i].strip(), parts[i + 1].strip()))
    return sections


def parse_document(raw: str, source: str) -> KnowledgeDocument:
    """从原文解析文档：文件导入与数据库里保存的版本原文走同一套解析。"""
    fields, body = parse_front_matter(raw)
    fields = fields or {}
    missing = [k for k in REQUIRED_FIELDS if not fields.get(k)]
    if missing:
        raise ValueError(f"{source}: front-matter 缺少字段 {missing}")
    return KnowledgeDocument(
        document_id=fields["document_id"],
        document_name=fields["document_name"],
        category=fields["category"],
        policy_version=fields["policy_version"],
        sections=_split_sections(body),
    )


def load_document(path: Path) -> KnowledgeDocument:
    return parse_document(path.read_text(encoding="utf-8"), str(path))


def load_corpus(root: Path) -> list[KnowledgeDocument]:
    docs = [load_document(p) for p in sorted(root.rglob("*.md"))]
    ids = [d.document_id for d in docs]
    if len(ids) != len(set(ids)):
        dupes = {i for i in ids if ids.count(i) > 1}
        raise ValueError(f"知识库 document_id 重复: {dupes}")
    return docs
