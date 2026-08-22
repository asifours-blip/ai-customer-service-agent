"""工具执行器：超时控制 + 分类型重试（审核修订①）。

策略矩阵：
- READ_ONLY_TOOL：瞬时错误（TIMEOUT / TOOL_EXECUTION_FAILED）自动重试 ≤ MAX_TOOL_RETRIES
- SIDE_EFFECT_TOOL：禁止自动重试（防重复工单）；超时语义特殊处理——
  写入可能已成功，提示用户查证而非盲目重试
- 确定性错误（NOT_FOUND/PERMISSION_DENIED/VALIDATION_FAILED/DUPLICATE）一律不重试
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.tools.base import BaseTool, ToolKind, ToolResult

_RETRYABLE = {"TOOL_EXECUTION_FAILED"}
_TRANSIENT_BACKOFF_SECONDS = 0.2


class ToolExecutor:
    def __init__(self, timeout_seconds: float | None = None, max_retries: int | None = None) -> None:
        settings = get_settings()
        self.timeout = timeout_seconds if timeout_seconds is not None else settings.tool_timeout_seconds
        self.max_retries = max_retries if max_retries is not None else settings.max_tool_retries
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tool")

    def execute(self, db: Session, tool: BaseTool, current_user_id: str, raw_args: dict[str, Any]) -> ToolResult:
        """带超时的执行 + 按 ToolKind 的重试策略。"""
        if tool.kind is ToolKind.SIDE_EFFECT:
            return self._run_with_timeout(db, tool, current_user_id, raw_args)

        attempts = self.max_retries + 1
        result: ToolResult | None = None
        for attempt in range(attempts):
            result = self._run_with_timeout(db, tool, current_user_id, raw_args)
            if result.success:
                return result
            if result.error and result.error["type"] in _RETRYABLE and attempt < attempts - 1:
                time.sleep(_TRANSIENT_BACKOFF_SECONDS)
                continue
            return result
        assert result is not None
        return result

    def _run_with_timeout(
        self, db: Session, tool: BaseTool, current_user_id: str, raw_args: dict[str, Any]
    ) -> ToolResult:
        future = self._pool.submit(tool.execute, db, current_user_id, raw_args)
        try:
            return future.result(timeout=self.timeout)
        except FutureTimeout:
            future.cancel()
            if tool.kind is ToolKind.SIDE_EFFECT:
                # 写入可能已成功（响应超时≠未写入）：绝不自动重试，引导查证
                return ToolResult.err(
                    tool,
                    "SIDE_EFFECT_TIMEOUT",
                    "操作已提交但响应超时，结果未知。请先查询确认是否已创建，不要重复提交。",
                )
            return ToolResult.err(tool, "TOOL_EXECUTION_FAILED", f"工具执行超时（>{self.timeout}s）")
