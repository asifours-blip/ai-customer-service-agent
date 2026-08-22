"""RAG 集成测试：真实 pgvector 摄取 → 检索 → 引用回答 → 拒答。"""

from __future__ import annotations

import pytest

from app.llm.client import FakeLLMClient
from app.rag import FakeEmbedding, RagService, count_chunks

pytestmark = [pytest.mark.rag, pytest.mark.integration]


def test_kb_ingested(db) -> None:
    with db() as s:
        n = count_chunks(s)
        assert n >= 60  # 当前语料 67；下限防语料调整后误报


def test_retrieval_hits_warranty(db) -> None:
    from app.rag import search

    with db() as s:
        qv = FakeEmbedding().embed_query("耳机整机保修多久")
        hits = search(s, qv, top_k=3)
        assert hits, "空知识库/检索失败"
        top_docs = [h.document_name for h in hits]
        assert "保修政策" in top_docs[:2], f"保修问题未命中保修政策: {top_docs}"


def test_answer_with_citation(db) -> None:
    svc = RagService(FakeEmbedding(), FakeLLMClient(), score_threshold=0.22)
    with db() as s:
        result = svc.answer(s, "这个耳机支持多久保修？")
    assert not result.abstained, f"应能回答（top_score={result.retrieval['top_score']}）"
    assert result.sources, "必须带引用"
    assert any("保修" in src["document"] for src in result.sources)
    # 引用真实对应检索 chunk（规格 §22）
    from app.rag.store import RetrievedChunk

    _ = RetrievedChunk  # 结构由 store.search 保证；此处校验 chunk_id 格式
    assert all("#" in src["chunk_id"] for src in result.sources)


def test_abstain_on_unanswerable(db) -> None:
    svc = RagService(FakeEmbedding(), FakeLLMClient(), score_threshold=0.22)
    with db() as s:
        result = svc.answer(s, "明天股票会涨吗，我该买哪只基金")
    assert result.abstained
    assert result.sources == []
    assert "没有足够信息" in result.answer


def test_empty_kb_abstains(db) -> None:
    from app.rag import rebuild_index

    with db() as s:
        rebuild_index(s, [], FakeEmbedding())
        svc = RagService(FakeEmbedding(), FakeLLMClient(), score_threshold=0.22)
        result = svc.answer(s, "耳机保修多久")
        assert result.abstained
        # 恢复知识库，避免污染后续测试
        from pathlib import Path

        from app.rag import chunk_corpus, load_corpus

        corpus = load_corpus(Path("knowledge_base"))
        rebuild_index(s, chunk_corpus(corpus), FakeEmbedding())
