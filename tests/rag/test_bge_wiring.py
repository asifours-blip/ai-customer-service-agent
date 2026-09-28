"""BGE 接线（阶段 4，先写红测试）：用最小的假 SentenceTransformer 替身，不装 torch、不下载模型。

- 未显式配置 HF_ENDPOINT 时不写入该环境变量（默认官方源，不再默认第三方镜像）
- 显式配置才生效；本地模型路径 + 离线模式；本地模型不存在时明确报错、不自动下载
- 维度与知识库版本记录的维度不一致：拒绝检索，提示重建版本
"""

from __future__ import annotations

import importlib.machinery
import os
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest


class _FakeSentenceTransformer:
    """最小替身：记录构造参数；维度可配；encode 返回确定性单位向量。"""

    instances: list[_FakeSentenceTransformer] = []
    dim = 512
    env_at_load: dict[str, str | None] = {}

    def __init__(self, model_name_or_path: str, **kwargs: Any) -> None:
        self.source = model_name_or_path
        self.kwargs = kwargs
        type(self).env_at_load = {k: os.environ.get(k) for k in ("HF_ENDPOINT", "HF_HUB_OFFLINE")}
        type(self).instances.append(self)

    def get_sentence_embedding_dimension(self) -> int:
        return type(self).dim

    def encode(self, texts: list[str], normalize_embeddings: bool = True) -> list[list[float]]:
        return [[1.0] + [0.0] * (type(self).dim - 1) for _ in texts]


@pytest.fixture()
def fake_st(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_FakeSentenceTransformer]]:
    import app.config as config

    module = types.ModuleType("sentence_transformers")
    module.__spec__ = importlib.machinery.ModuleSpec("sentence_transformers", None)
    module.SentenceTransformer = _FakeSentenceTransformer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    _FakeSentenceTransformer.instances = []
    _FakeSentenceTransformer.dim = 512
    # 隔离 .env 与外部环境：测试只看自己设置的变量；HF_* 变量退出时自动还原
    monkeypatch.setattr(config.Settings, "model_config", {"env_file": None, "extra": "ignore"})
    for key in ("HF_ENDPOINT", "HF_HUB_OFFLINE", "BGE_MODEL_PATH"):
        monkeypatch.delenv(key, raising=False)
    config.get_settings.cache_clear()
    yield _FakeSentenceTransformer
    config.get_settings.cache_clear()


def test_hf_endpoint_not_written_by_default(fake_st: type[_FakeSentenceTransformer]) -> None:
    from app.rag.embedding import LocalBGEEmbedding

    emb = LocalBGEEmbedding("BAAI/bge-small-zh-v1.5")
    assert "HF_ENDPOINT" not in os.environ
    assert fake_st.env_at_load["HF_ENDPOINT"] is None  # 加载时也没有被设成镜像
    assert fake_st.instances[0].source == "BAAI/bge-small-zh-v1.5"
    assert emb.dim == 512


def test_hf_endpoint_applied_only_when_explicit(fake_st: type[_FakeSentenceTransformer],
                                                monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """HF_ENDPOINT 写在 .env 里（不在进程环境中）：加载模型前写入进程环境（huggingface_hub 导入时读取）。"""
    import app.config as config
    from app.rag.embedding import LocalBGEEmbedding

    env_file = tmp_path / ".env"
    env_file.write_text("HF_ENDPOINT=https://mirror.internal.example\n", encoding="utf-8")
    monkeypatch.setattr(config.Settings, "model_config", {"env_file": str(env_file), "extra": "ignore"})
    config.get_settings.cache_clear()
    LocalBGEEmbedding("BAAI/bge-small-zh-v1.5")
    assert fake_st.env_at_load["HF_ENDPOINT"] == "https://mirror.internal.example"


def test_local_model_path_offline(fake_st: type[_FakeSentenceTransformer], monkeypatch: pytest.MonkeyPatch,
                                  tmp_path: Path) -> None:
    from app.rag.embedding import LocalBGEEmbedding

    model_dir = tmp_path / "bge-small-zh-v1.5"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (model_dir / "modules.json").write_text("[]", encoding="utf-8")
    monkeypatch.setenv("BGE_MODEL_PATH", str(model_dir))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    LocalBGEEmbedding("BAAI/bge-small-zh-v1.5")
    st = fake_st.instances[0]
    assert st.source == str(model_dir)
    assert st.kwargs.get("local_files_only") is True
    assert fake_st.env_at_load["HF_HUB_OFFLINE"] == "1"
    assert "HF_ENDPOINT" not in os.environ


def test_missing_local_model_path_fails_clearly_without_download(
    fake_st: type[_FakeSentenceTransformer], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.rag.embedding import EmbeddingUnavailableError, LocalBGEEmbedding

    missing = tmp_path / "no-such-model"
    monkeypatch.setenv("BGE_MODEL_PATH", str(missing))
    with pytest.raises(EmbeddingUnavailableError, match="BGE_MODEL_PATH") as info:
        LocalBGEEmbedding("BAAI/bge-small-zh-v1.5")
    assert "不会自动下载" in str(info.value)
    assert fake_st.instances == []  # 根本没去加载（更不会下载）


def test_offline_mode_cache_miss_fails_clearly(fake_st: type[_FakeSentenceTransformer],
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """HF_HUB_OFFLINE 且本地缓存缺失：把底层 OSError 转成明确错误。"""
    from app.rag.embedding import EmbeddingUnavailableError, LocalBGEEmbedding

    def boom(self: Any, model_name_or_path: str, **kwargs: Any) -> None:
        assert kwargs.get("local_files_only") is True
        raise OSError("We couldn't connect to 'https://huggingface.co' ... offline mode")

    monkeypatch.setattr(_FakeSentenceTransformer, "__init__", boom)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    with pytest.raises(EmbeddingUnavailableError, match="离线"):
        LocalBGEEmbedding("BAAI/bge-small-zh-v1.5")


def test_bge_status_reports_missing_local_path(fake_st: type[_FakeSentenceTransformer],
                                               monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import app.config as config
    from app.rag.embedding import bge_status

    monkeypatch.setenv("BGE_MODEL_PATH", str(tmp_path / "absent"))
    status = bge_status(config.get_settings())
    assert status["ready"] is False
    assert status["local_path_set"] is True and status["local_path_exists"] is False
    assert status["hf_endpoint"] == "huggingface.co（官方源）"
    assert fake_st.instances == []  # 只读检查，不加载模型


def test_dimension_mismatch_refuses_retrieval_offline_check() -> None:
    """纯函数：查询维度与版本记录维度不一致 → 拒绝并提示重建版本。"""
    from app.rag.answerer import KbRebuildRequiredError, ensure_dimension_matches

    ensure_dimension_matches(version_id=3, recorded_dim=512, query_dim=512)
    with pytest.raises(KbRebuildRequiredError, match="重建") as info:
        ensure_dimension_matches(version_id=3, recorded_dim=512, query_dim=384)
    assert "v3" in str(info.value) and "384" in str(info.value) and "512" in str(info.value)
