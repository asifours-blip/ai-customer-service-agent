"""FastAPI 应用入口：装配路由与统一异常处理。"""

from __future__ import annotations

import logging
import mimetypes
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # 进程启动：上一个进程崩溃/重启残留的 DRAFT/INGESTING 版本（没有存活连接持有导入锁）→ FAILED
    from app.kb.service import recover_interrupted
    from app.services import database

    recovered = recover_interrupted(database.SessionLocal)
    if recovered:
        logger.warning("重启恢复：知识库版本 %s 导入未完成，已标记 FAILED", recovered)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        lifespan=lifespan,
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
        return JSONResponse(status_code=exc.http_status, content={"detail": {**to_error_dict(exc), **exc.extra()}})

    app.include_router(api_router)
    return app


app = create_app()
