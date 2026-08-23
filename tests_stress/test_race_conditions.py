"""并发/竞态压测（本地验证用，不进正式 eval，不进 CI）。

覆盖：
S1 同 idempotency_key 多并发 create_ticket（8 线程 Barrier 对齐）
S2 同会话并发重复"确认"（Agent 层，check_pending 消费竞态）
S3 响应丢失后重试（executor 超时但 INSERT 已 commit）
S4 pending 过期与确认竞争（EXPIRED 分支 vs 有效执行）
S5 跨会话状态污染（B 会话不得执行/泄漏 A 会话的 pending 与实体）
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.agent.service import AgentService
from app.agent.state import make_pending, save_state_to_conversation
from app.api.chat import _agent_service
from app.models.conversation import Conversation
from app.models.ticket import Ticket
from app.services.orders import get_order
from app.tools.ticket import build_registry

U = "U001"
ORDER = "A10001"


def _stress_ticket_count(factory, key_prefix: str = "pa-stress-%") -> int:
    """只数本压测创建的工单（idempotency_key 前缀过滤）——seed 自带 T10001/T10002 不计入。"""
    with factory() as s:
        return len(list(s.scalars(select(Ticket).where(Ticket.idempotency_key.like(key_prefix)))))


# ---------- S1：同 idempotency_key 并发建单 ----------

def test_s1_concurrent_same_key_exactly_one_ticket(db) -> None:
    registry = build_registry()
    tool = registry.require("create_ticket")
    key = "pa-stress-s1-0001"
    n = 8
    barrier = threading.Barrier(n)
    outcomes: list[dict] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait()  # 对齐起跑，最大化 SELECT-then-INSERT 竞态窗口
        with db() as s:
            try:
                r = tool.execute(s, U, {
                    "order_id": ORDER, "category": "REFUND",
                    "title": f"stress-{i}", "idempotency_key": key,
                })
                with lock:
                    outcomes.append({"kind": "ok", "replay": r.idempotent_replay,
                                     "ticket": r.data.get("ticket_id") if r.data else None})
            except Exception as exc:  # 记录原始异常类型（不吞，供报告）
                with lock:
                    outcomes.append({"kind": type(exc).__name__, "detail": str(exc)[:120]})

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(worker, range(n)))

    assert _stress_ticket_count(db) == 1, f"期望恰 1 张工单，实际 {_stress_ticket_count(db)}"
    kinds = {}
    for o in outcomes:
        kinds[o["kind"]] = kinds.get(o["kind"], 0) + 1
    print("\nS1 outcomes:", kinds, outcomes)
    # 语义断言：所有成功路径必须指向同一张工单
    tickets = {o["ticket"] for o in outcomes if o["kind"] == "ok"}
    assert len(tickets) <= 1


# ---------- S2：同会话并发重复确认 ----------

def _seed_pending(factory, session_id: str, *, expires_in: float, pending_id: str) -> None:
    """expires_in>0 未来过期；<0 已过期（直接改写 expires_at 覆盖 make_pending 的 TTL）。"""
    with factory() as s:
        conv = s.scalar(select(Conversation).where(Conversation.session_id == session_id))
        if conv is None:
            conv = Conversation(session_id=session_id, user_id=U)
            s.add(conv)
            s.commit()
        pending = make_pending(
            "CREATE_TICKET",
            {"order_id": ORDER, "category": "REFUND", "title": f"stress-{pending_id}"},
            pending_id=pending_id,
        )
        from app.agent.service import utcnow as svc_utcnow

        pending["expires_at"] = (svc_utcnow() + timedelta(seconds=expires_in)).isoformat()
        save_state_to_conversation(s, conv, active_order_id=ORDER, active_ticket_id=None, pending=pending)


def test_s2_concurrent_double_confirm_at_most_one_ticket(db) -> None:
    """实验结论（2026-08-24 实测）：
    - 数据完整性 ✓：UNIQUE 约束保证恰 1 张工单（败者的 INSERT 被数据库拒绝）；
    - 语义缺口 G-1 ✗：败者线程的 UniqueViolation 未被工具层捕获为 DUPLICATE/replay，
      以 PendingRollbackError 穿透到服务层（表现为"内部异常"话术而非"重复确认"话术）。
      对应审计 16 号：create_ticket 为 SELECT-then-INSERT，无 IntegrityError 兜底。"""
    sid = "stress-s2-session"
    _seed_pending(db, sid, expires_in=600, pending_id="pa-stress-s2-0001")
    svc: AgentService = _agent_service()
    n = 4
    barrier = threading.Barrier(n)
    outcomes: list[dict] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        with db() as s:
            try:
                result = svc.handle(s, U, sid, "确认")
                with lock:
                    outcomes.append({"kind": "ok", "answer": str(result.get("answer", ""))[:80]})
            except Exception as exc:
                with lock:
                    outcomes.append({"kind": type(exc).__name__, "detail": str(exc)})

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(lambda _: worker(), range(n)))

    count = _stress_ticket_count(db)
    print("\nS2 outcomes:", [
        {**o, "detail": o.get("detail", "")[:80]} for o in outcomes
    ])
    assert count == 1, f"并发确认必须恰产生 1 张工单，实际 {count}"
    assert any(o["kind"] == "ok" for o in outcomes), "至少一个确认成功"
    violation = [o for o in outcomes if "UniqueViolation" in o.get("detail", "")]
    replay = [o for o in outcomes if o["kind"] == "ok" and "已存在" in o.get("answer", "")]
    # 允许两种合法败者形态：幂等重放（快路径）或 UniqueViolation（慢竞态，已知缺口 G-1）
    assert len(violation) + len(replay) + 1 == n, "每个败者必须是重放或被 UNIQUE 拒绝，不得产生第二单"


# ---------- S3：响应丢失后重试（超时≠未写入） ----------

def test_s3_timeout_after_commit_retry_same_key_single_ticket(db) -> None:
    from app.tools.base import BaseTool, ToolResult
    from app.tools.executor import ToolExecutor

    registry = build_registry()
    real = registry.require("create_ticket")

    class SlowAfterCommitTool(BaseTool):
        name, kind, args_model = real.name, real.kind, real.args_model

        def _run(self, s, uid, args) -> ToolResult:
            r = real._run(s, uid, args)  # 真实 INSERT+commit 在此完成
            time.sleep(0.5)              # ...然后"响应丢失"（网络/超时）
            return r

    slow = SlowAfterCommitTool()
    ex = ToolExecutor(timeout_seconds=0.1)
    key = "pa-stress-s3-0001"
    args = {"order_id": ORDER, "category": "REFUND", "title": "S3 timeout",
            "idempotency_key": key}

    with db() as s:
        first = ex.execute(s, slow, U, args)   # → SIDE_EFFECT_TIMEOUT（写入已发生）
    time.sleep(0.8)                             # 等后台线程真正跑完 commit
    assert not first.success and first.error and first.error["type"] == "SIDE_EFFECT_TIMEOUT"
    assert _stress_ticket_count(db) == 1, "超时后工单应已存在（响应丢失≠未写入）"

    with db() as s:
        retry = ex.execute(s, real, U, args)   # 调用方携同 key 重试
    assert retry.success and retry.idempotent_replay, "同 key 重试必须命中幂等重放"
    assert _stress_ticket_count(db) == 1


# ---------- S4：pending 过期与确认竞争 ----------

def test_s4a_expired_pending_confirm_does_not_execute(db) -> None:
    sid = "stress-s4a"
    _seed_pending(db, sid, expires_in=-1.0, pending_id="pa-stress-s4a-0001")  # 已过期 1 秒
    svc = _agent_service()
    with db() as s:
        result = svc.handle(s, U, sid, "确认")
    print("\nS4a answer:", result.get("answer"))
    assert _stress_ticket_count(db) == 0, "过期 pending 的确认不得建单"


def test_s4b_valid_pending_confirm_executes_once(db) -> None:
    sid = "stress-s4b"
    _seed_pending(db, sid, expires_in=600, pending_id="pa-stress-s4b-0001")
    svc = _agent_service()
    with db() as s:
        result = svc.handle(s, U, sid, "确认")
    print("\nS4b answer:", result.get("answer"))
    assert _stress_ticket_count(db) == 1


# ---------- S5：跨会话状态污染 ----------

def test_s5_cross_session_no_pending_or_entity_leak(db) -> None:
    sid_a, sid_b = "stress-s5-a", "stress-s5-b"
    _seed_pending(db, sid_a, expires_in=600, pending_id="pa-stress-s5-0001")  # A 有 pending + active_order
    svc = _agent_service()
    with db() as s:
        result = svc.handle(s, U, sid_b, "确认")  # B 会话说"确认"
    answer = str(result.get("answer", ""))
    print("\nS5 answer B:", answer)
    assert _stress_ticket_count(db) == 0, "B 会话的确认不得执行 A 会话的 pending"
    with db() as s:
        conv_a = s.scalar(select(Conversation).where(Conversation.session_id == sid_a))
        assert conv_a is not None
        assert conv_a.pending_action_id == "pa-stress-s5-0001", "B 的操作不应消费/清空 A 的 pending"
