"""知识库摄取：load → chunk → embed → pgvector 全量重建。

用法：python scripts/ingest_docs.py
EMBEDDING_BACKEND=fake（默认，CI/离线）| bge（本地真实嵌入，需 [rag-local]）
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.rag import (  # noqa: E402
    FakeEmbedding,
    chunk_corpus,
    get_embedding_client,
    load_corpus,
    rebuild_index,
    search,
)
from app.services.database import SessionLocal  # noqa: E402


def main() -> int:
    settings = get_settings()
    corpus_root = ROOT / "knowledge_base"
    docs = load_corpus(corpus_root)
    chunks = chunk_corpus(docs)
    embedder = get_embedding_client(settings.embedding_backend, settings.bge_model_name)

    db = SessionLocal()
    try:
        n = rebuild_index(db, chunks, embedder)
        print(f"ingest: {len(docs)} 篇文档 → {n} 个 chunk → backend={settings.embedding_backend}")
        # 冒烟：验证检索可用（固定用 Fake 向量，与重建无关，仅证明列可查）
        qv = FakeEmbedding().embed_query("耳机 保修 多久")
        for h in search(db, qv, top_k=3):
            print(f"  {h.score:.3f}  {h.document_name} · {h.section}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
