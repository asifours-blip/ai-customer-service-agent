"""Embedding 抽象（外部审核修订②）。

- FakeEmbedding：字符 bigram 特征哈希，确定性、零依赖，CI/离线测试用；
  相同词频的文本余弦相似度高，离线检索质量可用。
- LocalBGEEmbedding：BAAI/bge-small-zh-v1.5（512 维），真实 Demo/Evaluation 用。
切换由 EMBEDDING_BACKEND 控制，Retriever 不感知具体实现。
"""

from __future__ import annotations

import hashlib
import math
import os
from functools import lru_cache
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import httpx

if TYPE_CHECKING:
    from app.config import Settings

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

    backend = "fake"

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


class EmbeddingUnavailableError(RuntimeError):
    """本地向量模型不可用（依赖未装 / 本地目录不存在 / 离线模式下缓存缺失）。绝不因此自动下载。"""


def _apply_hf_env(settings: Settings) -> None:
    """只把显式配置写入进程环境（huggingface_hub 在导入时读取）；未配置时一个变量都不写，默认官方源。"""
    if settings.hf_endpoint.strip():
        os.environ["HF_ENDPOINT"] = settings.hf_endpoint.strip()
    if settings.hf_hub_offline:
        os.environ["HF_HUB_OFFLINE"] = "1"


def _offline(settings: Settings) -> bool:
    return settings.hf_hub_offline or os.environ.get("HF_HUB_OFFLINE", "").strip().lower() in ("1", "true", "yes", "on")


def _local_dir_ready(path: Path) -> bool:
    return path.is_dir() and any((path / name).is_file() for name in ("modules.json", "config.json"))


class LocalBGEEmbedding:
    """本地 BAAI/bge-small-zh-v1.5（需要 [rag-local] 可选依赖组）。

    模型来源：BGE_MODEL_PATH（本地目录，只从该目录加载）> 模型名（HF 缓存；HF_HUB_OFFLINE 时只用缓存）。
    不再默认第三方镜像：HF_ENDPOINT 只在用户显式配置时生效。
    """

    backend = "bge"

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5") -> None:
        from app.config import get_settings

        settings = get_settings()
        _apply_hf_env(settings)
        if find_spec("sentence_transformers") is None:
            raise EmbeddingUnavailableError(
                "LocalBGEEmbedding 需要 [rag-local] 依赖组：pip install -e '.[rag-local]'"
            )
        source, local_only = model_name, _offline(settings)
        if settings.bge_model_path.strip():
            path = Path(settings.bge_model_path.strip())
            if not _local_dir_ready(path):
                raise EmbeddingUnavailableError(
                    f"BGE_MODEL_PATH 指向的本地模型不存在或不完整：{path}"
                    "（需包含 modules.json / config.json）。不会自动下载，请先把模型放到该目录"
                )
            source, local_only = str(path), True
        from sentence_transformers import SentenceTransformer

        try:
            self._model = SentenceTransformer(source, local_files_only=local_only)
        except OSError as exc:
            if not local_only:
                raise
            raise EmbeddingUnavailableError(
                f"离线模式下无法加载本地模型 {source}：本地缓存缺失或不完整，不会自动下载"
                f"（{type(exc).__name__}）"
            ) from None
        try:
            self.dim = int(self._model.get_sentence_embedding_dimension() or BGE_DIM)
        except AttributeError:
            self.dim = int(self._model.get_embedding_dimension() or BGE_DIM)  # st>=6 改名兼容

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(texts, normalize_embeddings=True)
        return [[float(x) for x in vec] for vec in vectors]


def _hf_cache_has(model_name: str) -> bool:
    """只读检查 HF 缓存里是否已有该模型（不导入 huggingface_hub，不联网）。"""
    hub = os.environ.get("HF_HUB_CACHE") or os.path.join(
        os.environ.get("HF_HOME") or os.path.join(Path.home(), ".cache", "huggingface"), "hub"
    )
    snapshots = Path(hub) / ("models--" + model_name.replace("/", "--")) / "snapshots"
    return snapshots.is_dir() and any(_local_dir_ready(d) for d in snapshots.iterdir())


def bge_status(settings: Settings) -> dict[str, Any]:
    """BGE 是否就绪（只读：不加载模型、不联网、不修改环境变量）。"""
    endpoint = settings.hf_endpoint.strip() or os.environ.get("HF_ENDPOINT", "").strip()
    path = settings.bge_model_path.strip()
    status: dict[str, Any] = {
        "model": settings.bge_model_name,
        "dependency_installed": find_spec("sentence_transformers") is not None,
        "local_path_set": bool(path),
        "local_path_exists": _local_dir_ready(Path(path)) if path else None,
        "hf_endpoint": (_host(endpoint) + "（显式配置）") if endpoint else "huggingface.co（官方源）",
        "hf_hub_offline": _offline(settings),
    }
    if not status["dependency_installed"]:
        ready, detail = False, "未安装 [rag-local] 依赖"
    elif path:
        ready = bool(status["local_path_exists"])
        detail = "本地模型目录就绪" if ready else "本地模型目录不存在或不完整（不会自动下载）"
    elif _hf_cache_has(settings.bge_model_name):
        ready, detail = True, "模型已在本地缓存"
    elif status["hf_hub_offline"]:
        ready, detail = False, "离线模式且本地缓存缺失（不会自动下载）"
    else:
        ready, detail = False, "本地未缓存：首次加载会从上面的 hf_endpoint 下载"
    status.update(ready=ready, detail=detail)
    return status


def _host(url: str) -> str:
    try:
        return httpx.URL(url).host or url[:60]
    except httpx.InvalidURL:
        return url[:60]


@lru_cache(maxsize=4)
def get_embedding_client(backend: str, bge_model_name: str) -> EmbeddingClient:
    """按 (backend, model) 缓存实例：LocalBGEEmbedding 构造即加载模型，每请求重建不可接受。"""
    if backend == "fake":
        return FakeEmbedding()
    if backend == "bge":
        return LocalBGEEmbedding(bge_model_name)
    raise ValueError(f"未知 embedding backend: {backend}（可选 fake | bge）")


def backend_name(embedder: EmbeddingClient) -> str:
    """向量后端名，记录到知识库版本上：不同后端的向量不可混用检索。"""
    return str(getattr(embedder, "backend", type(embedder).__name__))


def serving_retrieval() -> tuple[EmbeddingClient, float]:
    """在线服务使用的 (embedder, 拒答阈值)。

    聊天检索、管理员上传的后台导入、启动初始化必须用同一个 embedder：
    版本内的向量与查询向量来自同一后端，冒烟检查的结论才对线上成立。
    离线开关（NO_PAID_API=true，默认）固定 FakeEmbedding，与原 /api/chat 行为一致。
    """
    from app.config import get_settings

    settings = get_settings()
    if settings.no_paid_api:
        return FakeEmbedding(), settings.retrieval_score_threshold_fake
    embedder = get_embedding_client(settings.embedding_backend, settings.bge_model_name)
    threshold = (
        settings.retrieval_score_threshold_bge
        if settings.embedding_backend == "bge"
        else settings.retrieval_score_threshold_fake
    )
    return embedder, threshold
