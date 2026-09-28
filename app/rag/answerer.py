"""RAG 问答服务：检索 → 拒答判定 → 生成（引用溯源）。

输出结构（外部审核修订⑥，无 confidence）：
{answer, sources[], abstained, retrieval:{top_score, top_k}}
retrieval.top_score 只是检索相似度，不代表答案正确概率。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.llm.client import ANSWER_MODE_TEMPLATE, LLMClient, answer_mode_of
from app.rag import store
from app.rag.embedding import EmbeddingClient
from app.rag.store import KbRebuildRequiredError, ensure_dimension_matches

__all__ = ["ABSTAIN_MESSAGE", "KbRebuildRequiredError", "RagAnswer", "RagService", "ensure_dimension_matches"]

ABSTAIN_MESSAGE = "当前知识库中没有足够信息回答这个问题。"

SYSTEM_PROMPT = (
    "你是企业售后客服知识助手。只依据给定的知识库内容回答，"
    "回答末尾不要编造信息；知识库中没有的内容要明确说明。用中文简洁回答。"
)


@dataclass(frozen=True)
class RagAnswer:
    answer: str
    sources: list[dict[str, str]] = field(default_factory=list)
    abstained: bool = False
    retrieval: dict[str, Any] = field(default_factory=lambda: {"top_score": 0.0, "top_k": 0})
    model: str = ""
    usage_prompt_tokens: int = 0
    usage_completion_tokens: int = 0
    # 回答方式：拒答是模板；生成走 LLM 客户端声明的方式（真实模型 MODEL / 离线回显 OFFLINE_ECHO）
    answer_mode: str = ANSWER_MODE_TEMPLATE


class RagService:
    def __init__(
        self,
        embedder: EmbeddingClient,
        llm: LLMClient,
        *,
        score_threshold: float = 0.22,
        version_id: int | None = None,
    ) -> None:
        self.embedder = embedder
        self.llm = llm
        self.score_threshold = score_threshold
        # None = 检索当前 ACTIVE 版本（线上）；评测可固定到指定版本
        self.version_id = version_id

    def answer(self, db: Session, query: str, *, top_k: int = 5) -> RagAnswer:
        query_vector = self.embedder.embed_query(query)
        # 维度与版本记录不一致时 store.search 抛 KbRebuildRequiredError：拒绝检索，不给出错位的结果
        hits = store.search(db, query_vector, top_k=top_k, version_id=self.version_id)
        top_score = hits[0].score if hits else 0.0
        retrieval = {"top_score": top_score, "top_k": len(hits)}

        if not hits or top_score < self.score_threshold:
            return RagAnswer(answer=ABSTAIN_MESSAGE, abstained=True, retrieval=retrieval)

        context_blocks = [
            f"【{h.document_name} · {h.section}】\n{h.content}" for h in hits
        ]
        user_prompt = (
            "知识库内容：\n" + "\n\n".join(context_blocks) + f"\n\n用户问题：{query}\n请依据上述知识库回答。"
        )
        resp = self.llm.complete(SYSTEM_PROMPT, user_prompt)
        # 引用记录 (version_id, chunk_id)：版本发布/回滚后仍能按版本取回当时的原文
        sources = [
            {
                "document": h.document_name,
                "section": h.section,
                "chunk_id": h.chunk_id,
                "version_id": str(h.version_id),
            }
            for h in hits
        ]
        return RagAnswer(
            answer=resp.content,
            sources=sources,
            abstained=False,
            retrieval=retrieval,
            model=resp.model,
            usage_prompt_tokens=resp.usage.prompt_tokens,
            usage_completion_tokens=resp.usage.completion_tokens,
            answer_mode=answer_mode_of(self.llm),
        )
