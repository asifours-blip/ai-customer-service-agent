"""API 路由聚合。"""

from fastapi import APIRouter

from app.api import auth, chat, conversations, kb, orders, support, system, tickets, traces

api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(chat.router, prefix="/chat", tags=["chat"])
api_router.include_router(conversations.router, prefix="/conversations", tags=["conversations"])
api_router.include_router(orders.router, prefix="/orders", tags=["orders"])
api_router.include_router(tickets.router, prefix="/tickets", tags=["tickets"])
api_router.include_router(support.router, prefix="/support", tags=["support"])
api_router.include_router(traces.router, prefix="/traces", tags=["traces"])
api_router.include_router(kb.router, prefix="/kb", tags=["knowledge-base"])
api_router.include_router(system.router, prefix="/system", tags=["system"])
