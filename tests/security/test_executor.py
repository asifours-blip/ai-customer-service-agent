"""ToolExecutor：分类型重试 / 超时 / 确定性错误不重试。纯单测（Fake 工具）。"""

from __future__ import annotations

from app.tools.base import BaseTool, ToolKind, ToolResult
from app.tools.executor import ToolExecutor


class _ScriptedTool(BaseTool):
    """按脚本返回结果的假工具，记录调用次数。"""

    name = "scripted"
    kind = ToolKind.READ_ONLY

    def __init__(self, results: list[ToolResult], delay: float = 0.0) -> None:
        from pydantic import BaseModel

        class _Args(BaseModel):
            x: int = 0

        self.args_model = _Args  # type: ignore[misc]
        self.results = list(results)
        self.calls = 0
        self.delay = delay

    def _run(self, db, current_user_id, args) -> ToolResult:  # noqa: ANN001
        import time

        self.calls += 1  # 先计数再等待：超时也计入调用次数
        if self.delay:
            time.sleep(self.delay)
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]


def _ok() -> ToolResult:
    return ToolResult(True, {"v": 1}, None, "scripted", ToolKind.READ_ONLY)


def _transient() -> ToolResult:
    err = {"type": "TOOL_EXECUTION_FAILED", "message": "瞬时故障"}
    return ToolResult(False, None, err, "scripted", ToolKind.READ_ONLY)


def test_read_only_retries_transient_then_succeeds() -> None:
    tool = _ScriptedTool([_transient(), _ok()])
    ex = ToolExecutor(timeout_seconds=2, max_retries=2)
    r = ex.execute(None, tool, "U001", {"x": 1})  # type: ignore[arg-type]
    assert r.success
    assert tool.calls == 2


def test_read_only_gives_up_after_max_retries() -> None:
    tool = _ScriptedTool([_transient()])
    ex = ToolExecutor(timeout_seconds=2, max_retries=2)
    r = ex.execute(None, tool, "U001", {"x": 1})  # type: ignore[arg-type]
    assert not r.success
    assert tool.calls == 3  # 1 + 2 重试


def test_deterministic_errors_not_retried() -> None:
    denied = ToolResult(False, None, {"type": "PERMISSION_DENIED", "message": "无权"}, "scripted", ToolKind.READ_ONLY)
    tool = _ScriptedTool([denied])
    ex = ToolExecutor(timeout_seconds=2, max_retries=2)
    r = ex.execute(None, tool, "U001", {"x": 1})  # type: ignore[arg-type]
    assert not r.success
    assert tool.calls == 1  # 确定性错误立即返回


class _SideEffectTool(_ScriptedTool):
    name = "create_x"
    kind = ToolKind.SIDE_EFFECT


def test_side_effect_never_auto_retries() -> None:
    tool = _SideEffectTool([_transient()])
    ex = ToolExecutor(timeout_seconds=2, max_retries=2)
    r = ex.execute(None, tool, "U001", {"x": 1})  # type: ignore[arg-type]
    assert not r.success
    assert tool.calls == 1  # 关键断言：SIDE_EFFECT 一次即止


def test_side_effect_timeout_semantics() -> None:
    tool = _SideEffectTool([_ok()], delay=1.5)
    ex = ToolExecutor(timeout_seconds=0.2, max_retries=2)
    r = ex.execute(None, tool, "U001", {"x": 1})  # type: ignore[arg-type]
    assert not r.success
    assert r.error["type"] == "SIDE_EFFECT_TIMEOUT"
    assert "不要重复提交" in r.error["message"]
    assert tool.calls == 1


def test_read_only_timeout_retried() -> None:
    tool = _ScriptedTool([_ok()], delay=1.5)
    tool.delay = 0.0  # 只让第一次慢？改脚本：直接用超时一次后成功
    tool.delay = 1.5

    class _SlowThenFast(_ScriptedTool):
        def _run(self, db, current_user_id, args):  # noqa: ANN001
            import time

            self.calls += 1
            if self.calls == 1:
                time.sleep(1.5)
            return _ok()

    stf = _SlowThenFast([_ok()])
    ex = ToolExecutor(timeout_seconds=0.2, max_retries=2)
    r = ex.execute(None, stf, "U001", {"x": 1})  # type: ignore[arg-type]
    assert r.success  # 超时算瞬时错误，READ_ONLY 重试后成功
    assert stf.calls == 2
