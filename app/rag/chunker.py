"""确定性切块（规格 §8：chunk 结构含 document/section/chunk_id/content/metadata）。

按 ## 标题分节，节内按固定字符窗口滑切；同一输入永远产生同一批 chunk。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.rag.loader import KnowledgeDocument

MAX_CHUNK_CHARS = 400
OVERLAP_CHARS = 60


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    document_id: str
    document_name: str
    section: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _clean(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _windows(text: str, size: int, overlap: int) -> list[str]:
    if len(text) <= size:
        return [text]
    step = size - overlap
    out: list[str] = []
    i = 0
    while i < len(text):
        out.append(text[i : i + size])
        if i + size >= len(text):
            break
        i += step
    return out


def chunk_document(doc: KnowledgeDocument) -> list[Chunk]:
    chunks: list[Chunk] = []
    for heading, body in doc.sections:
        for w_idx, window in enumerate(_windows(_clean(body), MAX_CHUNK_CHARS, OVERLAP_CHARS)):
            if not window:
                continue
            chunks.append(
                Chunk(
                    chunk_id=f"{doc.document_id}#{heading}#{w_idx}",
                    document_id=doc.document_id,
                    document_name=doc.document_name,
                    section=heading,
                    content=window,
                    metadata={
                        "category": doc.category,
                        "policy_version": doc.policy_version,
                    },
                )
            )
    return chunks


def chunk_corpus(docs: list[KnowledgeDocument]) -> list[Chunk]:
    out: list[Chunk] = []
    for d in docs:
        out.extend(chunk_document(d))
    return out
