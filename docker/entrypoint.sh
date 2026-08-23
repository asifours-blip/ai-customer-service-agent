#!/bin/sh
# Phase 8 初始化链：迁移 → 种子数据 → 知识库摄取 → 起服务（全部幂等，容器重启安全）
set -e

echo "[entrypoint] alembic upgrade head"
alembic upgrade head

echo "[entrypoint] seed_db（幂等：已存在则跳过）"
python scripts/seed_db.py

echo "[entrypoint] ingest_docs（重建式摄取）"
python scripts/ingest_docs.py

echo "[entrypoint] starting uvicorn"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
