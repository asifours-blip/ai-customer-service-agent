"""工具基座：READ_ONLY / SIDE_EFFECT 分类 + 统一结果结构（规格 §7、§13）。

核心安全模型：
- 参数经 Pydantic 校验后才进入业务层
- 权限由后端 service 判定，LLM 无法绕过
- READ_ONLY_TOOL 允许自动重试（≤ MAX_TOOL_RETRIES）；SIDE_EFFECT_TOOL 禁止普通重试，
  只能凭 idempotency_key 幂等重放（审核修订①）
- 任何异常都不把 DB 细节暴露给 LLM：统一映射为 {type, message}
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.services.errors import AppError, to_error_dict


class ToolKind(StrEnum):
    READ_ONLY = "READ_ONLY"
    SIDE_EFFECT = "SIDE_EFFECT"


@dataclass(frozen=True)
class ToolResult:
    success: bool
    data: dict[str, Any] | None
    error: dict[str, str] | None
    tool_name: str
    kind: ToolKind
    idempotent_replay: bool = False  # SIDE_EFFECT 幂等重放时为 True

    @staticmethod
    def ok(tool: BaseTool, data: dict[str, Any], *, replay: bool = False) -> ToolResult:
        return ToolResult(True, data, None, tool.name, tool.kind, replay)

    @staticmethod
    def err(tool: BaseTool, err_type: str, message: str) -> ToolResult:
        return ToolResult(False, None, {"type": err_type, "message": message}, tool.name, tool.kind)


class BaseTool:
    name: ClassVar[str]
    kind: ClassVar[ToolKind]
    args_model: ClassVar[type[BaseModel]]

    def execute(self, db: Session, current_user_id: str, raw_args: dict[str, Any]) -> ToolResult:
        """参数校验 → 业务执行 → 统一错误映射。LLM 只能看到 ToolResult。"""
        try:
            args = self.args_model.model_validate(raw_args)
        except Exception as exc:  # pydantic ValidationError
            return ToolResult.err(self, "VALIDATION_FAILED", f"工具参数不合法: {exc}"[:300])
        try:
            return self._run(db, current_user_id, args)
        except AppError as exc:
            e = to_error_dict(exc)
            return ToolResult.err(self, e["type"], e["message"])
        except Exception:
            # 不向 LLM/用户暴露内部异常细节
            return ToolResult.err(self, "TOOL_EXECUTION_FAILED", "工具执行内部错误，请稍后重试或转人工")

    def _run(self, db: Session, current_user_id: str, args: BaseModel) -> ToolResult:
        raise NotImplementedError


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具重复注册: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def require(self, name: str) -> BaseTool:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"未注册的工具: {name}")
        return tool
