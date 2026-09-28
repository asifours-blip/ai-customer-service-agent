"""知识库启动初始化：只在「没有任何 ACTIVE 版本」时把 knowledge_base/ 目录导入为初始版本并生效。

已有生效版本 → 什么都不做（不重建、不覆盖，管理员发布/回滚的结果在重启后保持不变）。
初始版本导入走与管理员上传相同的校验与冒烟检查；失败则退出码 1（容器启动失败，问题不会被静默吞掉）。
向量后端与在线检索一致（serving_retrieval）：离线开关打开时为 FakeEmbedding。

用法：python scripts/bootstrap_kb.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.kb.service import Retrieval, bootstrap_from_directory  # noqa: E402
from app.kb.smoke import load_smoke_config  # noqa: E402
from app.rag.embedding import backend_name, serving_retrieval  # noqa: E402
from app.services.database import SessionLocal  # noqa: E402


def main() -> int:
    embedder, threshold = serving_retrieval()
    retrieval = Retrieval(embedder=embedder, threshold=threshold, smoke=load_smoke_config())
    try:
        result, version = bootstrap_from_directory(SessionLocal, ROOT / "knowledge_base", retrieval)
    except (RuntimeError, ValueError) as exc:
        print(f"bootstrap_kb: 失败：{exc}")
        return 1
    if result == "SKIPPED":
        print(f"bootstrap_kb: 已有生效版本 v{version.id}，跳过（不重建）")
    elif result == "RACED":
        print(f"bootstrap_kb: 其他进程已先完成初始化；本次导入的 v{version.id} 保持 READY，未发布")
    else:
        print(
            f"bootstrap_kb: 初始版本 v{version.id} 已生效（{version.doc_count} 篇文档 / "
            f"{version.chunk_count} 个 chunk / backend={backend_name(embedder)}）"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
