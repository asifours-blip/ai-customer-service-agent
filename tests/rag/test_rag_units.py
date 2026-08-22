"""RAG 纯单测：loader / chunker / embedding / 拒答阈值逻辑。零 DB。"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from app.llm.client import FakeLLMClient
from app.rag import FakeEmbedding

ROOT = Path(__file__).resolve().parents[2]
KB = ROOT / "knowledge_base"


# --- loader ---


def test_load_corpus_real_kb() -> None:
    from app.rag import load_corpus

    docs = load_corpus(KB)
    assert len(docs) == 12
    ids = [d.document_id for d in docs]
    assert len(ids) == len(set(ids))
    assert all(d.policy_version == "2026.08" for d in docs)
    by_id = {d.document_id: d for d in docs}
    assert by_id["doc-policy-refund"].category == "policies"
    assert len(by_id["doc-p001"].sections) >= 4  # 概述 + 各功能节


def test_load_document_missing_front_matter(tmp_path: Path) -> None:
    from app.rag.loader import load_document

    bad = tmp_path / "bad.md"
    bad.write_text("没有 front-matter 的文档", encoding="utf-8")
    with pytest.raises(ValueError, match="front-matter"):
        load_document(bad)


# --- chunker ---


def test_chunk_deterministic() -> None:
    from app.rag import chunk_corpus, load_corpus

    docs = load_corpus(KB)
    a = chunk_corpus(docs)
    b = chunk_corpus(docs)
    assert [(c.chunk_id, c.content) for c in a] == [(c.chunk_id, c.content) for c in b]


def test_chunk_structure() -> None:
    from app.rag import chunk_corpus, load_corpus

    chunks = chunk_corpus(load_corpus(KB))
    assert chunks, "知识库不应为空"
    c = chunks[0]
    assert c.document_id and c.document_name and c.section and c.content
    assert c.metadata["policy_version"] == "2026.08"
    assert len({x.chunk_id for x in chunks}) == len(chunks)  # chunk_id 全局唯一


def test_long_section_windowed() -> None:
    from app.rag.chunker import MAX_CHUNK_CHARS, _windows

    text = "字" * (MAX_CHUNK_CHARS * 3)
    ws = _windows(text, MAX_CHUNK_CHARS, 60)
    assert len(ws) >= 3
    assert all(len(w) <= MAX_CHUNK_CHARS for w in ws)


# --- embedding ---


def test_fake_embedding_deterministic_and_normalized() -> None:
    from app.rag import FakeEmbedding

    e = FakeEmbedding()
    v1 = e.embed_query("耳机保修期多长")
    v2 = e.embed_query("耳机保修期多长")
    assert v1 == v2
    assert len(v1) == 512
    assert math.isclose(sum(x * x for x in v1), 1.0, rel_tol=1e-6)


def test_fake_embedding_similarity_ranking() -> None:
    """相近文本相似度 > 无关文本（离线检索可用的前提）。"""
    from app.rag import FakeEmbedding

    e = FakeEmbedding()

    def cos(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b, strict=True))

    q = e.embed_query("耳机支持多久保修")
    hit = e.embed_documents(["耳机整机保修 12 个月，电池保修 6 个月"])[0]
    miss = e.embed_documents(["请提供银行卡号与短信验证码以完成退款"])[0]
    assert cos(q, hit) > cos(q, miss)


def test_get_embedding_client_unknown_backend() -> None:
    from app.rag import get_embedding_client

    with pytest.raises(ValueError, match="未知"):
        get_embedding_client("openai", "x")


# --- answerer 拒答逻辑（monkeypatch store.search，零 DB）---


class _FakeSearch:
    def __init__(self, hits: list) -> None:
        self.hits = hits

    def __call__(self, db, query_vector, top_k=5):  # noqa: ANN001
        return self.hits


def test_answerer_abstains_when_no_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.rag import answerer
    from app.rag.answerer import RagService

    monkeypatch.setattr(answerer.store, "search", _FakeSearch([]))
    svc = RagService(FakeEmbedding(), FakeLLMClient(), score_threshold=0.10)
    result = svc.answer(None, "任意问题")  # type: ignore[arg-type]
    assert result.abstained
    assert result.sources == []
    assert "没有足够信息" in result.answer


def test_answerer_abstains_when_score_below_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.rag import answerer
    from app.rag.answerer import RagService
    from app.rag.store import RetrievedChunk

    low = RetrievedChunk("c1", "d1", "无关文档", "节", "内容", 0.05)
    monkeypatch.setattr(answerer.store, "search", _FakeSearch([low]))
    svc = RagService(FakeEmbedding(), FakeLLMClient(), score_threshold=0.10)
    result = svc.answer(None, "问题")  # type: ignore[arg-type]
    assert result.abstained
    assert result.retrieval["top_score"] == 0.05


def test_answerer_answers_with_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.llm.client import FakeLLMClient
    from app.rag import answerer
    from app.rag.answerer import RagService
    from app.rag.store import RetrievedChunk

    hit = RetrievedChunk("c1", "doc-policy-warranty", "保修政策", "保修期限", "整机保修 12 个月", 0.42)
    monkeypatch.setattr(answerer.store, "search", _FakeSearch([hit]))
    svc = RagService(FakeEmbedding(), FakeLLMClient(), score_threshold=0.10)
    result = svc.answer(None, "耳机保修多久")  # type: ignore[arg-type]
    assert not result.abstained
    assert result.sources[0]["document"] == "保修政策"
    assert result.sources[0]["chunk_id"] == "c1"
    assert result.retrieval["top_score"] == 0.42
    assert result.retrieval["top_k"] == 1
    assert result.usage_prompt_tokens > 0
