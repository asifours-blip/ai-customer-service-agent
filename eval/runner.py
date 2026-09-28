"""评测 Runner：选定知识库版本 → 重置业务数据 → 逐 case 执行 Agent → 逐轮结果与最终 outcome。

确定性保证：每个 case 使用固定 session（eval-{case_id}），运行前重置业务数据（truncate → seed），
检索固定在一个知识库版本上（默认：与 knowledge_base/ 目录内容一致的版本），Fake 离线链路重复执行结果可复现。
知识库表不在重置范围内：评测从不删除、覆盖或切换线上生效版本（D-021）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agent.service import AgentService
from app.models.knowledge import KbVersion
from app.rag.embedding import EmbeddingClient

ROOT = Path(__file__).resolve().parents[1]

# 评测重置绝不触碰的知识库表：版本、原文、chunk、审计
KB_TABLES = frozenset({"kb_versions", "kb_documents", "kb_chunks", "kb_audit_log"})
EVALUABLE_STATUSES = ("READY", "ACTIVE", "RETIRED")


def reset_environment(db: Session) -> None:
    """清空业务数据（用户/订单/工单/会话/Trace 等）→ seed。知识库表不动。

    一条 TRUNCATE 覆盖全部业务表：不触发 ticket_events 的只追加行级触发器，也不需要按外键顺序逐表删除。
    """
    from app.models import Base
    from scripts.seed_db import seed

    tables = [t.name for t in Base.metadata.sorted_tables if t.name not in KB_TABLES]
    db.execute(text("TRUNCATE TABLE " + ", ".join(f'"{name}"' for name in tables)))
    db.commit()
    seed()  # 使用 app SessionLocal（与 db 同库）


def resolve_kb_version(spec: str, embedder: EmbeddingClient, threshold: float) -> KbVersion:
    """确定被评测的知识库版本。

    spec = "dir"（默认）：复用内容与向量后端都与 knowledge_base/ 目录一致的已校验版本，没有则导入一个新版本（不发布）；
    spec = "active"：当前生效版本；spec = 数字：指定版本（须为 READY / ACTIVE / RETIRED）。
    版本的向量后端必须与评测 embedder 一致，否则检索域错位，直接拒绝。
    """
    from app.kb.service import Retrieval, active_version, find_or_ingest_directory
    from app.kb.smoke import load_smoke_config
    from app.rag.embedding import backend_name
    from app.services import database

    factory = database.SessionLocal
    if spec == "dir":
        retrieval = Retrieval(embedder=embedder, threshold=threshold, smoke=load_smoke_config())
        return find_or_ingest_directory(factory, ROOT / "knowledge_base", retrieval, actor="eval")
    with factory() as db:
        if spec == "active":
            version = active_version(db)
            if version is None:
                raise ValueError("当前没有生效的知识库版本")
        else:
            if not spec.isdigit():
                raise ValueError(f"--kb-version 只接受 dir | active | 版本号，收到 {spec!r}")
            version = db.get(KbVersion, int(spec))
            if version is None:
                raise ValueError(f"知识库版本 v{spec} 不存在")
    if version.status not in EVALUABLE_STATUSES:
        raise ValueError(f"v{version.id} 状态为 {version.status}，只能评测 {'/'.join(EVALUABLE_STATUSES)} 版本")
    backend = backend_name(embedder)
    if version.embedding_backend and version.embedding_backend != backend:
        raise ValueError(f"v{version.id} 的向量后端是 {version.embedding_backend}，与评测使用的 {backend} 不一致")
    return version


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
                "prompt_tokens": r.get("prompt_tokens", 0) or 0,
                "completion_tokens": r.get("completion_tokens", 0) or 0,
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
