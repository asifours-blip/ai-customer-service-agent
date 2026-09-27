"""ToolExecutor 超时后的 Session 隔离：真实 PostgreSQL + Event 门控的假工具（确定性，不靠 sleep 赌时序）。

future.cancel() 停不掉已在运行的工作线程：超时后调用方与迟到的工作线程若共享同一个
非线程安全的 Session，迟到线程的 commit/rollback 会改写调用方的工作单元。
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import pytest
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.conversation import Conversation
from app.models.ticket import Ticket
from app.services import tickets as ticket_service
from app.tools.base import BaseTool, ToolKind, ToolResult
from app.tools.executor import ToolExecutor

pytestmark = pytest.mark.integration

_WAIT = 10  # 秒：只作为防挂死上限，正常路径由 Event 立即放行


class _NoArgs(BaseModel):
    pass


class _GatedTool(BaseTool):
    """进入工具后先登记收到的 Session，再阻塞到 release 被 set，然后执行 action。"""

    name = "gated"
    args_model = _NoArgs

    def __init__(self, kind: ToolKind, action: Callable[[BaseTool, Session, str], ToolResult]) -> None:
        self.kind = kind  # type: ignore[misc]
        self._action = action
        self.release = threading.Event()
        self.started = threading.Semaphore(0)
        self.finished = threading.Semaphore(0)
        self.sessions: list[Session] = []
        self.errors: list[str] = []
        self._lock = threading.Lock()

    def _run(self, db: Session, current_user_id: str, args: BaseModel) -> ToolResult:
        with self._lock:
            self.sessions.append(db)
        self.started.release()
        try:
            assert self.release.wait(_WAIT), "测试未放行工具"
            return self._action(self, db, current_user_id)
        except Exception as exc:
            with self._lock:
                self.errors.append(f"{type(exc).__name__}: {exc}"[:200])
            raise
        finally:
            self.finished.release()


def _late_create_ticket(tool: BaseTool, db: Session, user_id: str) -> ToolResult:
    ticket, created = ticket_service.create_ticket(
        db, user_id=user_id, category="OTHER", title="late-commit", idempotency_key="pa-late-iso-0001"
    )
    return ToolResult.ok(tool, {"ticket_id": ticket.id, "created": created})


def _count_tickets(tool: BaseTool, db: Session, user_id: str) -> ToolResult:
    return ToolResult.ok(tool, {"n": db.scalar(select(func.count()).select_from(Ticket))})


def test_side_effect_late_commit_does_not_touch_caller_unit_of_work(db) -> None:
    tool = _GatedTool(ToolKind.SIDE_EFFECT, _late_create_ticket)
    ex = ToolExecutor(timeout_seconds=0.2)

    with db() as caller:
        result = ex.execute(caller, tool, "U001", {})
        assert result.error is not None and result.error["type"] == "SIDE_EFFECT_TIMEOUT"
        assert tool.started.acquire(timeout=_WAIT)

        # 超时后调用方继续自己的工作单元：一条尚未提交的写入
        caller.add(Conversation(user_id="U001", session_id="caller-uncommitted", title="caller"))
        tool.release.set()  # 迟到的工作线程此刻才真正写库并 commit
        assert tool.finished.acquire(timeout=_WAIT)
        caller.rollback()  # 调用方放弃自己的工作单元

    with db() as s:
        leaked = s.scalar(
            select(func.count()).select_from(Conversation).where(Conversation.session_id == "caller-uncommitted")
        )
        late = s.scalar(
            select(func.count()).select_from(Ticket).where(Ticket.idempotency_key == "pa-late-iso-0001")
        )
    assert tool.errors == []
    assert late == 1, "迟到的写入应在工作线程自己的事务里完成提交"
    assert leaked == 0, "迟到线程的 commit 把调用方已回滚的数据带进了数据库"
    assert tool.sessions[0] is not caller


def test_read_only_retry_after_timeout_never_shares_session(db) -> None:
    tool = _GatedTool(ToolKind.READ_ONLY, _count_tickets)
    ex = ToolExecutor(timeout_seconds=0.2, max_retries=1)

    with db() as caller:
        result = ex.execute(caller, tool, "U001", {})
        assert result.error is not None and result.error["type"] == "TOOL_EXECUTION_FAILED"
        # 首次尝试与重试都已进入工具且仍在运行：此刻有两个并发执行体
        assert tool.started.acquire(timeout=_WAIT) and tool.started.acquire(timeout=_WAIT)
        in_flight = list(tool.sessions)
        tool.release.set()
        assert tool.finished.acquire(timeout=_WAIT) and tool.finished.acquire(timeout=_WAIT)

    print("\nconcurrent attempt errors:", tool.errors)
    assert len(in_flight) == 2
    assert in_flight[0] is not in_flight[1], "并发运行的两次尝试共享了同一个 Session"
    assert all(s is not caller for s in in_flight), "工作线程拿到了调用方的 Session"


def test_caller_session_stays_usable_after_worker_timeout(db) -> None:
    """超时后（工作线程仍在运行 / 已迟到提交）调用方 Session 可继续正常读、写、提交。"""
    tool = _GatedTool(ToolKind.SIDE_EFFECT, _late_create_ticket)
    ex = ToolExecutor(timeout_seconds=0.2)

    with db() as caller:
        result = ex.execute(caller, tool, "U001", {})
        assert result.error is not None and result.error["type"] == "SIDE_EFFECT_TIMEOUT"
        assert tool.started.acquire(timeout=_WAIT)

        # 工作线程仍阻塞在工具内：调用方读写提交不受影响
        assert caller.scalar(select(func.count()).select_from(Ticket).where(Ticket.user_id == "U001")) == 1
        caller.add(Conversation(user_id="U001", session_id="caller-during-worker", title="caller"))
        caller.commit()

        tool.release.set()
        assert tool.finished.acquire(timeout=_WAIT)

        # 迟到提交完成后：调用方仍能看到最新已提交数据，并继续写入
        late = caller.scalar(select(Ticket).where(Ticket.idempotency_key == "pa-late-iso-0001"))
        assert late is not None and late.user_id == "U001"
        caller.add(Conversation(user_id="U001", session_id="caller-after-worker", title="caller"))
        caller.commit()

    with db() as s:
        sessions = set(
            s.scalars(
                select(Conversation.session_id).where(Conversation.session_id.like("caller-%-worker"))
            )
        )
    assert tool.errors == []
    assert sessions == {"caller-during-worker", "caller-after-worker"}
