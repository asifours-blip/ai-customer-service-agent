#!/bin/sh
# 初始化链：迁移 → 种子数据 → 知识库初始化 → 起服务（全部幂等，容器重启安全）
set -e

echo "[entrypoint] alembic upgrade head"
alembic upgrade head

echo "[entrypoint] seed_db（幂等：已存在则跳过）"
python scripts/seed_db.py

# 不再每次重建知识库：只有没有任何 ACTIVE 版本时才把 knowledge_base/ 导入为初始版本并生效（D-021）
echo "[entrypoint] bootstrap_kb（已有生效版本则跳过）"
python scripts/bootstrap_kb.py

echo "[entrypoint] starting uvicorn"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
