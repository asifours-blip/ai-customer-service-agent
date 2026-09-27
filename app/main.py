"""FastAPI 应用入口：装配路由与统一异常处理。"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import api_router
from app.services.errors import AppError, to_error_dict

# 前端用原生 ES modules：浏览器要求 JS MIME 类型严格正确。
# Windows 注册表可能把 .js 映射为 text/plain，这里显式固定，避免模块脚本被拒绝加载。
mimetypes.add_type("text/javascript", ".js")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Agent + RAG 智能客服与工单自动化平台",
        version=__version__,
        description="受控 Agent · 权限隔离 · 幂等工具 · LLM 评测 · CI",
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

    @app.exception_handler(AppError)
    def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"detail": to_error_dict(exc)})

    app.include_router(api_router)
    return app


app = create_app()
