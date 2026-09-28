"""领域异常（统一错误结构，规格 §13）。

Tool 层与 API 层共用；HTTP 状态码在 API 异常处理器映射。
DB 原始异常绝不直接暴露给 LLM / 用户。
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    code = "INTERNAL_ERROR"
    http_status = 500

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)
        self.message = message or self.code

    def extra(self) -> dict[str, Any]:
        """附加到 HTTP 错误体的结构化信息（如上传校验的逐文件错误）；默认无。"""
        return {}


class NotFoundError(AppError):
    code = "NOT_FOUND"
    http_status = 404


class PermissionDeniedError(AppError):
    code = "PERMISSION_DENIED"
    http_status = 403


class AuthenticationError(AppError):
    code = "AUTHENTICATION_FAILED"
    http_status = 401


class ValidationFailedError(AppError):
    code = "VALIDATION_FAILED"
    http_status = 422


class InvalidTransitionError(AppError):
    code = "INVALID_TRANSITION"
    http_status = 409


class DuplicateError(AppError):
    code = "DUPLICATE"
    http_status = 409


class IdempotencyConflictError(AppError):
    """同一用户复用幂等键，但业务内容与首次请求不同：不能当作重放。"""

    code = "IDEMPOTENCY_CONFLICT"
    http_status = 409


class AlreadyAssignedError(AppError):
    """工单已被其他客服领取：领取只能成功一次。"""

    code = "ALREADY_ASSIGNED"
    http_status = 409


class InvalidStateError(AppError):
    """当前工单状态不允许该操作（如 CLOSED 后回复、未解决就反馈）。"""

    code = "INVALID_STATE"
    http_status = 409


class ToolExecutionError(AppError):
    code = "TOOL_EXECUTION_FAILED"
    http_status = 500


def to_error_dict(err: AppError) -> dict[str, str]:
    """统一 {type, message} 错误结构（规格 §13）。"""
    return {"type": err.code, "message": err.message}
