"""FastAPI 应用入口：装配路由与统一异常处理。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.api import api_router
from app.services.errors import AppError, to_error_dict


def create_app() -> FastAPI:
    app = FastAPI(
        title="Agent + RAG 智能客服与工单自动化平台",
        version=__version__,
        description="受控 Agent · 权限隔离 · 幂等工具 · LLM 评测 · CI",
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.exception_handler(AppError)
    def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"detail": to_error_dict(exc)})

    app.include_router(api_router)
    return app


app = create_app()
