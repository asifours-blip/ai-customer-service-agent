#!/bin/sh
# 数据库备份：pg_dump 自定义格式（-Fc），可选压缩、支持并行恢复、pg_restore 能挑表恢复。
#
# 用法（本机 / CI，bash 或 sh 均可）：
#   ./scripts/backup.sh <容器名或空=用 DATABASE_URL> <输出目录，默认 ./backups>
#
# 例：
#   ./scripts/backup.sh csagent-pg-test ./backups
#   DATABASE_URL=postgresql://app:app@localhost:55432/agent_cs_test ./scripts/backup.sh
#
# Windows：
#   - Git Bash / WSL：直接跑本脚本
#   - PowerShell（不进容器，直连宿主机映射端口）：
#       $env:PGPASSWORD = "app"
#       pg_dump -h localhost -p 55432 -U app -Fc -f backup.dump agent_cs_test
#     （需要本机装了 PostgreSQL 客户端；也可以用 `docker exec <容器> pg_dump ...` 避免装客户端）
set -eu

CONTAINER="${1:-}"
OUT_DIR="${2:-./backups}"
STAMP=$(date -u +"%Y%m%dT%H%M%SZ")
mkdir -p "$OUT_DIR"
OUT_FILE="$OUT_DIR/agent_cs_${STAMP}.dump"

if [ -n "$CONTAINER" ]; then
    echo "[backup] 容器内 pg_dump：$CONTAINER -> $OUT_FILE"
    # 容器内变量 POSTGRES_USER/POSTGRES_DB 由 docker-compose.yml 固定为 app/agent_cs（测试库同名但库名不同）
    DB_NAME=$(docker exec "$CONTAINER" printenv POSTGRES_DB 2>/dev/null || echo agent_cs)
    docker exec "$CONTAINER" pg_dump -U app -Fc -d "$DB_NAME" > "$OUT_FILE"
else
    : "${DATABASE_URL:?未指定容器名时必须设置 DATABASE_URL（postgresql://user:pass@host:port/dbname）}"
    echo "[backup] 直连 DATABASE_URL -> $OUT_FILE"
    pg_dump -Fc -d "$DATABASE_URL" > "$OUT_FILE"
fi

echo "[backup] 完成：$OUT_FILE（$(wc -c < "$OUT_FILE") 字节）"
