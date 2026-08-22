"""知识库文档加载：解析 front-matter（document_id/name/category/policy_version）+ 正文分节。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
_FIELD = re.compile(r"^(\w+):\s*\"?([^\"\n]*)\"?\s*$", re.M)


@dataclass(frozen=True)
class KnowledgeDocument:
    document_id: str
    document_name: str
    category: str
    policy_version: str
    sections: list[tuple[str, str]]  # (heading, body)


def _parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    m = _FRONT_MATTER.match(text)
    if m is None:
        return {}, text
    fields = dict(_FIELD.findall(m.group(1)))
    return fields, text[m.end() :]


def _split_sections(body: str) -> list[tuple[str, str]]:
    parts = re.split(r"^##\s+(.*)$", body, flags=re.M)
    # parts: [前言, h1, body1, h2, body2, ...]
    sections: list[tuple[str, str]] = []
    if parts and parts[0].strip():
        sections.append(("概述", parts[0].strip()))
    for i in range(1, len(parts) - 1, 2):
        sections.append((parts[i].strip(), parts[i + 1].strip()))
    return sections


def load_document(path: Path) -> KnowledgeDocument:
    raw = path.read_text(encoding="utf-8")
    fields, body = _parse_front_matter(raw)
    required = ("document_id", "document_name", "category", "policy_version")
    missing = [k for k in required if not fields.get(k)]
    if missing:
        raise ValueError(f"{path}: front-matter 缺少字段 {missing}")
    return KnowledgeDocument(
        document_id=fields["document_id"],
        document_name=fields["document_name"],
        category=fields["category"],
        policy_version=fields["policy_version"],
        sections=_split_sections(body),
    )


def load_corpus(root: Path) -> list[KnowledgeDocument]:
    docs = [load_document(p) for p in sorted(root.rglob("*.md"))]
    ids = [d.document_id for d in docs]
    if len(ids) != len(set(ids)):
        dupes = {i for i in ids if ids.count(i) > 1}
        raise ValueError(f"知识库 document_id 重复: {dupes}")
    return docs
