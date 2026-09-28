"""工具执行器：超时控制 + 分类型重试（审核修订①）。

策略矩阵：
- READ_ONLY_TOOL：瞬时错误（TIMEOUT / TOOL_EXECUTION_FAILED）自动重试 ≤ MAX_TOOL_RETRIES
- SIDE_EFFECT_TOOL：禁止自动重试（防重复工单）；超时语义特殊处理——
  写入可能已成功，提示用户查证而非盲目重试
- 确定性错误（NOT_FOUND/PERMISSION_DENIED/VALIDATION_FAILED/DUPLICATE）一律不重试

Session 隔离：future.cancel() 停不掉已在运行的线程，超时后工作线程仍可能继续读写。
因此每次执行都在工作线程里打开独立 Session，由它自己 commit/rollback/close；
调用方的 Session 永远只留在调用线程，超时后也不会被迟到的线程或重试线程触碰。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.services import database
from app.tools.base import BaseTool, ToolKind, ToolResult

_RETRYABLE = {"TOOL_EXECUTION_FAILED"}
_TRANSIENT_BACKOFF_SECONDS = 0.2
SIDE_EFFECT_TIMEOUT_MESSAGE = (
    "操作响应超时，结果未知：工单可能已经创建成功。请先在【我的工单】列表中查看确认；"
    "如确认没有创建，再次回复【确认】即可——重新确认会复用同一个幂等键，不会重复开单。"
)


class ToolExecutor:
    def __init__(
        self,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        settings = get_settings()
        self.timeout = timeout_seconds if timeout_seconds is not None else settings.tool_timeout_seconds
        self.max_retries = max_retries if max_retries is not None else settings.max_tool_retries
        # None → 每次执行时取 database.SessionLocal（与请求级 get_db 同一工厂）
        self._session_factory = session_factory
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tool")

    def execute(self, db: Session, tool: BaseTool, current_user_id: str, raw_args: dict[str, Any]) -> ToolResult:
        """带超时的执行 + 按 ToolKind 的重试策略。

        db 是调用方的请求级 Session，只留在调用线程使用，不会传给工作线程（见模块说明）。
        """
        if tool.kind is ToolKind.SIDE_EFFECT:
            return self._run_with_timeout(tool, current_user_id, raw_args)

        attempts = self.max_retries + 1
        result: ToolResult | None = None
        for attempt in range(attempts):
            result = self._run_with_timeout(tool, current_user_id, raw_args)
            if result.success:
                return result
            if result.error and result.error["type"] in _RETRYABLE and attempt < attempts - 1:
                time.sleep(_TRANSIENT_BACKOFF_SECONDS)
                continue
            return result
        assert result is not None
        return result

    def _run_in_own_session(self, tool: BaseTool, current_user_id: str, raw_args: dict[str, Any]) -> ToolResult:
        """在工作线程内执行：Session 的生命周期完全归本线程所有。"""
        factory = self._session_factory or database.SessionLocal
        session = factory()
        try:
            result = tool.execute(session, current_user_id, raw_args)
            if result.success:
                session.commit()
            else:
                session.rollback()
            return result
        finally:
            session.close()  # 异常路径下 close 也会回滚未完成的事务

    def _run_with_timeout(self, tool: BaseTool, current_user_id: str, raw_args: dict[str, Any]) -> ToolResult:
        future = self._pool.submit(self._run_in_own_session, tool, current_user_id, raw_args)
        try:
            return future.result(timeout=self.timeout)
        except FutureTimeout:
            future.cancel()
            if tool.kind is ToolKind.SIDE_EFFECT:
                # 写入可能已成功（响应超时≠未写入）：绝不自动重试，引导查证；
                # 用户重新确认复用同一 pending_action 派生的幂等键，不会重复开单
                return ToolResult.err(tool, "SIDE_EFFECT_TIMEOUT", SIDE_EFFECT_TIMEOUT_MESSAGE)
            return ToolResult.err(tool, "TOOL_EXECUTION_FAILED", f"工具执行超时（>{self.timeout}s）")
