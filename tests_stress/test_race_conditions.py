"""并发/竞态压测（本地验证用，不进正式 eval，不进 CI）。

覆盖：
S1 同 idempotency_key 多并发 create_ticket（10 线程 Barrier 对齐）
S2 同会话并发重复"确认"（Agent 层，check_pending 消费竞态）
S3 响应丢失后重试（executor 超时但 INSERT 已 commit）
S4 pending 过期与确认竞争（EXPIRED 分支 vs 有效执行）
S5 跨会话状态污染（B 会话不得执行/泄漏 A 会话的 pending 与实体）
S6 唯一约束竞争恢复路径（跨用户同 key 不共享工单；同用户同 key 不同请求 → 冲突）
S7 SUPPORT 并发状态迁移（行锁：只能一方成功，另一方 INVALID_TRANSITION）
S8 两名 SUPPORT 并发领取同一工单（行锁：只能一方成功，另一方 ALREADY_ASSIGNED）
S9 同一工单并发提交两次反馈（行锁 + 唯一约束：只能一条，另一方 DUPLICATE）
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.service import AgentService
from app.agent.state import make_pending, save_state_to_conversation
from app.api.chat import _agent_service
from app.models.conversation import Conversation
from app.models.ticket import Ticket, TicketEvent, TicketFeedback
from app.services import tickets as ticket_service
from app.tools.ticket import build_registry

U = "U001"
ORDER = "A10001"


def _stress_ticket_count(factory, key_prefix: str = "pa-stress-%") -> int:
    """只数本压测创建的工单（idempotency_key 前缀过滤）——seed 自带 T10001/T10002 不计入。"""
    with factory() as s:
        return len(list(s.scalars(select(Ticket).where(Ticket.idempotency_key.like(key_prefix)))))


# ---------- S1：同 idempotency_key 并发建单 ----------

def test_s1_concurrent_same_key_exactly_one_ticket(db, monkeypatch) -> None:
    registry = build_registry()
    tool = registry.require("create_ticket")
    key = "pa-stress-s1-0001"
    n = 10
    barrier = threading.Barrier(n)
    idempotency_lookup_barrier = threading.Barrier(n)
    allocation_barrier = threading.Barrier(n)
    outcomes: list[dict] = []
    lock = threading.Lock()
    idempotency_lookup_count = 0
    rollback_count = 0
    original_scalar = Session.scalar
    original_rollback = Session.rollback
    original_next_ticket_id = ticket_service.next_ticket_id

    def synchronized_scalar(session, statement, *args, **kwargs):
        nonlocal idempotency_lookup_count
        result = original_scalar(session, statement, *args, **kwargs)
        if "tickets.idempotency_key" in str(statement):
            with lock:
                should_wait = idempotency_lookup_count < n
                if should_wait:
                    idempotency_lookup_count += 1
            if should_wait:
                idempotency_lookup_barrier.wait()
        return result

    def synchronized_next_ticket_id(session):
        ticket_id = original_next_ticket_id(session)
        allocation_barrier.wait()
        return ticket_id

    def counted_rollback(session, *args, **kwargs):
        nonlocal rollback_count
        with lock:
            rollback_count += 1
        return original_rollback(session, *args, **kwargs)

    monkeypatch.setattr(Session, "scalar", synchronized_scalar)
    monkeypatch.setattr(Session, "rollback", counted_rollback)
    monkeypatch.setattr(ticket_service, "next_ticket_id", synchronized_next_ticket_id)

    def worker(i: int) -> None:
        barrier.wait()  # 对齐起跑，最大化 SELECT-then-INSERT 竞态窗口
        with db() as s:
            try:
                r = tool.execute(s, U, {
                    "order_id": ORDER, "category": "REFUND",
                    # 同 key 的重放必须是同一份请求；内容不同属于 IDEMPOTENCY_CONFLICT（见 S6b）
                    "title": "stress-s1", "idempotency_key": key,
                })
                with lock:
                    outcomes.append({
                        "kind": "ok" if r.success else "tool_error",
                        "replay": r.idempotent_replay,
                        "ticket": r.data.get("ticket_id") if r.data else None,
                        "error": r.error,
                    })
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
    # 语义断言：每个调用都必须获得首张工单，而不是把 UNIQUE 竞争伪装成内部错误。
    assert len(outcomes) == n
    assert all(o["kind"] == "ok" for o in outcomes), outcomes
    assert sum(not o["replay"] for o in outcomes if o["kind"] == "ok") == 1
    assert sum(o["replay"] for o in outcomes if o["kind"] == "ok") == n - 1
    tickets = {o["ticket"] for o in outcomes if o["kind"] == "ok"}
    assert len(tickets) == 1
    assert rollback_count == n - 1


def test_s1b_concurrent_different_keys_create_unique_ticket_ids(db, monkeypatch) -> None:
    """十个合法独立请求并发时，ID allocator 不能给出同一个主键。"""
    registry = build_registry()
    tool = registry.require("create_ticket")
    n = 10
    allocation_barrier = threading.Barrier(n)
    outcomes: list[dict] = []
    lock = threading.Lock()
    original_next_ticket_id = ticket_service.next_ticket_id

    def synchronized_next_ticket_id(session):
        ticket_id = original_next_ticket_id(session)
        allocation_barrier.wait()
        return ticket_id

    monkeypatch.setattr(ticket_service, "next_ticket_id", synchronized_next_ticket_id)

    def worker(i: int) -> None:
        with db() as s:
            try:
                result = tool.execute(
                    s,
                    U,
                    {
                        "category": "OTHER",
                        "title": f"stress-different-{i}",
                        "idempotency_key": f"pa-stress-s1b-{i:04d}",
                    },
                )
                with lock:
                    outcomes.append(
                        {
                            "kind": "ok" if result.success else "tool_error",
                            "ticket": result.data.get("ticket_id") if result.data else None,
                            "error": result.error,
                        }
                    )
            except Exception as exc:
                with lock:
                    outcomes.append({"kind": type(exc).__name__, "detail": str(exc)[:120]})

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(worker, range(n)))

    print("\nS1b outcomes:", outcomes)
    assert len(outcomes) == n
    assert all(outcome["kind"] == "ok" for outcome in outcomes), outcomes
    ticket_ids = {outcome["ticket"] for outcome in outcomes}
    assert len(ticket_ids) == n
    assert _stress_ticket_count(db, "pa-stress-s1b-%") == n


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


def test_s2_concurrent_double_confirm_returns_controlled_idempotent_semantics(db) -> None:
    """竞争确认只允许一次副作用，竞争者必须得到受控语义，不能泄露 DB/500 错误。"""
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
                    outcomes.append({"kind": "ok", "answer": str(result.get("answer", ""))})
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
    assert len(outcomes) == n
    assert all(o["kind"] == "ok" for o in outcomes), outcomes
    answers = [o["answer"] for o in outcomes]
    assert sum("已为您创建售后工单" in answer for answer in answers) == 1
    assert sum("已存在（本次为重复确认，未重复创建）" in answer for answer in answers) == n - 1
    assert all("PendingRollbackError" not in answer for answer in answers)
    assert all("内部错误" not in answer and "TOOL_EXECUTION_FAILED" not in answer for answer in answers)


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


# ---------- S6：唯一约束竞争的恢复路径也必须按用户隔离、按指纹判冲突 ----------

def _race_creates(db, monkeypatch, requests: list[tuple[str, dict]]) -> list[dict]:
    """所有请求先都查不到幂等键，分配 ID 后对齐，强制每一方都走到 INSERT/commit。"""
    n = len(requests)
    allocation_barrier = threading.Barrier(n)
    original_next_ticket_id = ticket_service.next_ticket_id

    def synchronized_next_ticket_id(session):
        ticket_id = original_next_ticket_id(session)
        allocation_barrier.wait()
        return ticket_id

    monkeypatch.setattr(ticket_service, "next_ticket_id", synchronized_next_ticket_id)
    outcomes: list[dict] = []
    lock = threading.Lock()

    def worker(req: tuple[str, dict]) -> None:
        user_id, fields = req
        with db() as s:
            try:
                ticket, created = ticket_service.create_ticket(s, user_id=user_id, **fields)
                row = {"user": user_id, "kind": "ok", "ticket": ticket.id, "owner": ticket.user_id,
                       "created": created}
            except Exception as exc:
                row = {"user": user_id, "kind": getattr(exc, "code", type(exc).__name__)}
            with lock:
                outcomes.append(row)

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(worker, requests))
    return outcomes


def test_s6a_concurrent_same_key_different_users_never_share_ticket(db, monkeypatch) -> None:
    fields = {"category": "OTHER", "title": "stress-s6a", "idempotency_key": "pa-stress-s6a-0001"}
    outcomes = _race_creates(db, monkeypatch, [("U001", fields), ("U002", fields)])
    print("\nS6a outcomes:", outcomes)
    assert all(o["kind"] == "ok" for o in outcomes), outcomes
    assert all(o["owner"] == o["user"] for o in outcomes), f"竞争失败方拿到了别人的工单: {outcomes}"
    assert all(o["created"] for o in outcomes), outcomes
    assert len({o["ticket"] for o in outcomes}) == 2
    assert _stress_ticket_count(db, "pa-stress-s6a-%") == 2


def test_s6b_concurrent_same_user_same_key_different_request_conflicts(db, monkeypatch) -> None:
    key = "pa-stress-s6b-0001"
    outcomes = _race_creates(db, monkeypatch, [
        ("U001", {"category": "OTHER", "title": "stress-s6b-left", "idempotency_key": key}),
        ("U001", {"category": "OTHER", "title": "stress-s6b-right", "idempotency_key": key}),
    ])
    print("\nS6b outcomes:", outcomes)
    assert sorted(o["kind"] for o in outcomes) == ["IDEMPOTENCY_CONFLICT", "ok"], outcomes
    assert _stress_ticket_count(db, "pa-stress-s6b-%") == 1


# ---------- S7：SUPPORT 并发状态迁移 ----------

def test_s7_concurrent_transition_only_one_wins(db, monkeypatch) -> None:
    """领取人并发两次把 OPEN 工单推进到 PROCESSING（双击 / 两个标签页）：只能一方成功，另一方 INVALID_TRANSITION。

    阶段 2 起只有领取人能迁移状态，因此先由 SUPPORT001 领取，两个并发请求都以领取人身份发出。
    Barrier 放在「读到状态之后、校验迁移之前」，强制两方都先读再写；
    若读取带行锁，第二方读不到旧状态，Barrier 超时后按串行语义继续。
    """
    from app.services import ticket_state

    with db() as s:
        ticket_service.claim_ticket(s, "T10001", "SUPPORT001")

    n = 2
    read_barrier = threading.Barrier(n, timeout=2)
    original_assert = ticket_state.assert_transition

    def synchronized_assert(current: str, target: str) -> None:
        with suppress(threading.BrokenBarrierError):
            read_barrier.wait()
        original_assert(current, target)

    monkeypatch.setattr(ticket_state, "assert_transition", synchronized_assert)
    outcomes: list[dict] = []
    lock = threading.Lock()

    def worker(_i: int) -> None:
        with db() as s:
            try:
                t = ticket_service.transition_ticket(s, "T10001", "PROCESSING", "SUPPORT001")
                row = {"kind": "ok", "status": t.status}
            except Exception as exc:
                row = {"kind": getattr(exc, "code", type(exc).__name__)}
            with lock:
                outcomes.append(row)

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(worker, range(n)))

    print("\nS7 outcomes:", outcomes)
    assert sorted(o["kind"] for o in outcomes) == ["INVALID_TRANSITION", "ok"], outcomes
    with db() as s:
        ticket = s.get(Ticket, "T10001")
        assert ticket is not None and ticket.status == "PROCESSING"
        changes = s.scalars(
            select(TicketEvent).where(TicketEvent.ticket_id == "T10001", TicketEvent.event_type == "STATUS_CHANGED")
        ).all()
        assert len(changes) == 1  # 失败的一方不留处理记录


# ---------- S8：两名 SUPPORT 并发领取 ----------

def test_s8_concurrent_claim_only_one_wins(db, monkeypatch) -> None:
    """SUPPORT001 与 SUPPORT002 同时领取未指派的 T10001：只能一方成功，另一方 ALREADY_ASSIGNED。

    Barrier 放在「持锁读到 assignee_id 之后、判断能否领取之前」：没有行锁时两方都会读到 NULL 并各自写入
    （后写覆盖前写，两方都返回成功）；有行锁时第二方阻塞在 SELECT ... FOR UPDATE，Barrier 超时后串行继续。
    """
    n = 2
    read_barrier = threading.Barrier(n, timeout=2)
    original = ticket_service.ensure_claimable

    def synchronized(ticket, support_user_id: str) -> None:  # noqa: ANN001
        with suppress(threading.BrokenBarrierError):
            read_barrier.wait()
        original(ticket, support_user_id)

    monkeypatch.setattr(ticket_service, "ensure_claimable", synchronized)
    outcomes: list[dict] = []
    lock = threading.Lock()

    def worker(support_id: str) -> None:
        with db() as s:
            try:
                t = ticket_service.claim_ticket(s, "T10001", support_id)
                row = {"kind": "ok", "support": support_id, "assignee": t.assignee_id}
            except Exception as exc:
                row = {"kind": getattr(exc, "code", type(exc).__name__), "support": support_id}
            with lock:
                outcomes.append(row)

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(worker, ["SUPPORT001", "SUPPORT002"]))

    print("\nS8 outcomes:", outcomes)
    assert sorted(o["kind"] for o in outcomes) == ["ALREADY_ASSIGNED", "ok"], outcomes
    winner = next(o["support"] for o in outcomes if o["kind"] == "ok")
    with db() as s:
        ticket = s.get(Ticket, "T10001")
        assert ticket is not None and ticket.assignee_id == winner
        claims = s.scalars(
            select(TicketEvent).where(TicketEvent.ticket_id == "T10001", TicketEvent.event_type == "CLAIMED")
        ).all()
        assert [c.actor_id for c in claims] == [winner]


# ---------- S9：同一工单并发提交反馈 ----------

def test_s9_concurrent_feedback_only_one_row(db, monkeypatch) -> None:
    """客户在两个标签页同时提交反馈：只能写入一条，另一方 DUPLICATE（409）。"""
    with db() as s:
        ticket_service.claim_ticket(s, "T10001", "SUPPORT001")
        ticket_service.transition_ticket(s, "T10001", "PROCESSING", "SUPPORT001")
        ticket_service.transition_ticket(s, "T10001", "RESOLVED", "SUPPORT001")

    n = 2
    read_barrier = threading.Barrier(n, timeout=2)
    original = ticket_service.get_feedback

    def synchronized(session, ticket_id: str):  # noqa: ANN001, ANN202
        with suppress(threading.BrokenBarrierError):
            read_barrier.wait()
        return original(session, ticket_id)

    monkeypatch.setattr(ticket_service, "get_feedback", synchronized)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(rating: int) -> None:
        with db() as s:
            try:
                ticket_service.submit_feedback(s, "T10001", "U001", rating, "并发提交")
                kind = "ok"
            except Exception as exc:
                kind = getattr(exc, "code", type(exc).__name__)
            with lock:
                outcomes.append(kind)

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(worker, [4, 5]))

    print("\nS9 outcomes:", outcomes)
    assert sorted(outcomes) == ["DUPLICATE", "ok"], outcomes
    with db() as s:
        assert len(s.scalars(select(TicketFeedback).where(TicketFeedback.ticket_id == "T10001")).all()) == 1
