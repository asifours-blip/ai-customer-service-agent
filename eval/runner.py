"""评测 Runner：重置环境 → 逐 case 执行 Agent → 逐轮结果与最终 outcome。

确定性保证：每个 case 使用固定 session（eval-{case_id}），运行前全量重置
（truncate → seed → ingest），Fake 离线链路重复执行结果可复现。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.agent.service import AgentService

ROOT = Path(__file__).resolve().parents[1]


def reset_environment(db: Session, embedder: Any | None = None) -> None:
    """truncate 全部表 → seed → 知识库摄取。评测确定性前提。

    embedder 必须与被测 Agent 一致（离线 fake 或本地 bge），否则检索域错位。
    """
    from app.models import Base
    from app.rag import FakeEmbedding, chunk_corpus, load_corpus, rebuild_index
    from app.rag.embedding import EmbeddingClient
    from scripts.seed_db import seed

    emb: EmbeddingClient = embedder or FakeEmbedding()

    for table in reversed(Base.metadata.sorted_tables):
        db.execute(table.delete())
    db.commit()
    seed()  # 使用 app SessionLocal（与 db 同库）
    rebuild_index(db, chunk_corpus(load_corpus(ROOT / "knowledge_base")), emb)


def _failed_tools(turn: dict[str, Any]) -> list[str]:
    return [str(c.get("tool", "?")) for c in turn.get("tool_calls", []) if not c.get("ok", True)]


_BLOCK_WORDS = ("无权",)
_DUP_WORDS = ("已存在", "无需重复")
_NOTFOUND_WORDS = ("不存在", "暂无物流")


def derive_actual_outcome(turn: dict[str, Any]) -> str:
    """从单轮结果派生实际 outcome（判定顺序即优先级，纯函数可单测）。

    关键词判定（BLOCKED/DUPLICATE/NOT_FOUND）仅在存在真实失败的工具调用时生效——
    FakeLLM 会回显检索上下文，RAG 路径的答案文本可能偶然包含这些词。
    """
    answer = turn["answer"]
    route = turn["route"]
    if str(turn.get("error_type") or "").startswith("INJECTION_FLAGGED"):
        return "BLOCKED"
    if turn.get("failed_tools"):
        if any(w in answer for w in _BLOCK_WORDS):
            return "BLOCKED"
        if any(w in answer for w in _DUP_WORDS):
            return "DUPLICATE"
        if any(w in answer for w in _NOTFOUND_WORDS):
            return "NOT_FOUND"
    if turn.get("abstained"):
        return "REFUSED"
    if route == "HUMAN":
        # 仅按路由判定：知识库文本本身含"转接人工/人工客服"字样，回显会污染关键词判定
        return "REFUSED"
    if route == "CLARIFY":
        return "CLARIFY"
    return "SUCCESS"


def check_signal(answer: str, signals: list[str]) -> bool:
    return all(s in answer for s in signals)


def run_case(agent: AgentService, db: Session, case: dict[str, Any]) -> dict[str, Any]:
    """执行单个 case（支持多轮），返回逐轮结果 + 派生 outcome。"""
    if case.get("turns"):
        turns = case["turns"]
    else:
        single: dict[str, str] = {"input": case["input"]}
        turns = [single]
    # 单轮 case：顶层 expected_intent 传播到该轮
    if "expected_intent" in case and "expected_intent" not in turns[0]:
        turns[0]["expected_intent"] = case["expected_intent"]
    session_id = f"eval-{case['case_id']}"
    turn_results: list[dict[str, Any]] = []
    for turn in turns:
        r = agent.handle(db, case["user_id"], session_id, turn["input"])
        turn_results.append(
            {
                "input": turn["input"],
                "expected_intent": turn.get("expected_intent"),
                "answer": r["answer"],
                "route": r["route"],
                "intent": r.get("intent"),
                "error_type": r.get("error_type"),
                "tool_calls": r.get("tool_calls", []),
                "failed_tools": _failed_tools(r),
                "sources": r.get("sources", []),
                "abstained": bool(r.get("abstained")),
                "trace_id": r["trace_id"],
                "latency_ms": r.get("latency_ms", 0),
            }
        )
    final = turn_results[-1]
    return {
        "case_id": case["case_id"],
        "category": case["category"],
        "user_id": case["user_id"],
        "expected_outcome": case["expected_outcome"],
        "expected_intent": case.get("expected_intent"),
        "expected_tools": case.get("expected_tools", []),
        "expected_signal": case.get("expected_signal", []),
        "expected_document": case.get("expected_document"),
        "turns": turn_results,
        "final": final,
        "tools_called": [c["tool"] for t in turn_results for c in t["tool_calls"]],
        "actual_outcome": derive_actual_outcome(final),
        "signal_ok": check_signal(final["answer"], case.get("expected_signal", [])),
        "ran_at": datetime.now(tz=UTC).isoformat(),
    }


def run_all(agent: AgentService, db: Session, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [run_case(agent, db, c) for c in cases]
