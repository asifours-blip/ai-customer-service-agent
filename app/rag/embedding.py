"""Embedding 抽象（外部审核修订②）。

- FakeEmbedding：字符 bigram 特征哈希，确定性、零依赖，CI/离线测试用；
  相同词频的文本余弦相似度高，离线检索质量可用。
- LocalBGEEmbedding：BAAI/bge-small-zh-v1.5（512 维），真实 Demo/Evaluation 用。
切换由 EMBEDDING_BACKEND 控制，Retriever 不感知具体实现。
"""

from __future__ import annotations

import hashlib
import math
from importlib.util import find_spec
from typing import Protocol

FAKE_DIM = 512  # 与 pgvector 列维度一致（BGE 也为 512）；Fake 向量稀疏，余弦不受填充影响
BGE_DIM = 512


class EmbeddingClient(Protocol):
    dim: int

    def embed_query(self, text: str) -> list[float]:
        """查询文本 → 向量。"""
        ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """文档批量嵌入。"""
        ...


_STOP_CHARS = set("的了吗呢吧啊呀哦嘛嗯我你他她它们是是在有和不这那就还很也把被对与以及个请会能要可怎么为什么等哈")
# 停用字过滤：通用虚词 bigram 会给无关查询带来 0.1+ 的噪声相似度，
# 过滤后无关查询余弦接近 0，阈值才有区分度（fake 后端专用；BGE 无此问题）。


def _bigrams(text: str) -> list[str]:
    normalized = "".join(ch for ch in text.lower() if ch.isalnum() and ch not in _STOP_CHARS)
    return [normalized[i : i + 2] for i in range(len(normalized) - 1)] or (
        [normalized] if normalized else []
    )


class FakeEmbedding:
    """确定性字符 bigram 哈希嵌入（离线/CI）。"""

    def __init__(self, dim: int = FAKE_DIM) -> None:
        self.dim = dim

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for bg in _bigrams(text):
            h = int.from_bytes(hashlib.md5(bg.encode("utf-8")).digest()[:4], "big")
            vec[h % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [round(v / norm, 8) for v in vec]

    def embed_query(self, text: str) -> list[float]:
        return self._embed_one(text)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]


class LocalBGEEmbedding:
    """本地 BAAI/bge-small-zh-v1.5（需要 [rag-local] 可选依赖组）。"""

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5") -> None:
        if find_spec("sentence_transformers") is None:
            raise RuntimeError(
                "LocalBGEEmbedding 需要 [rag-local] 依赖组：pip install -e '.[rag-local]'"
            )
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name)
        self.dim = int(self._model.get_sentence_embedding_dimension() or BGE_DIM)

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(texts, normalize_embeddings=True)
        return [[float(x) for x in vec] for vec in vectors]


def get_embedding_client(backend: str, bge_model_name: str) -> EmbeddingClient:
    if backend == "fake":
        return FakeEmbedding()
    if backend == "bge":
        return LocalBGEEmbedding(bge_model_name)
    raise ValueError(f"未知 embedding backend: {backend}（可选 fake | bge）")
