#!/bin/sh
# 数据库恢复：pg_restore 从 backup.sh 产出的自定义格式（-Fc）文件恢复到目标库。
# 目标库必须先存在（本脚本不建库/建容器），恢复前清空目标库中的同名对象（--clean --if-exists）。
#
# 用法：
#   ./scripts/restore.sh <备份文件> <容器名或空=用 DATABASE_URL>
#
# 例（演练用临时容器，见 docs/backup-restore-drill.md）：
#   ./scripts/restore.sh ./backups/agent_cs_20260928T120000Z.dump csagent-restore-test
#   DATABASE_URL=postgresql://app:app@localhost:55432/agent_cs_test ./scripts/restore.sh ./backups/xxx.dump
#
# Windows：
#   - Git Bash / WSL：直接跑本脚本
#   - PowerShell（不进容器，直连宿主机映射端口）：
#       $env:PGPASSWORD = "app"
#       pg_restore -h localhost -p <port> -U app -d <dbname> --clean --if-exists backup.dump
set -eu

BACKUP_FILE="${1:?用法: restore.sh <备份文件> [容器名]}"
CONTAINER="${2:-}"

if [ ! -f "$BACKUP_FILE" ]; then
    echo "[restore] 备份文件不存在: $BACKUP_FILE" >&2
    exit 1
fi

if [ -n "$CONTAINER" ]; then
    echo "[restore] 容器内 pg_restore：$BACKUP_FILE -> $CONTAINER"
    DB_NAME=$(docker exec "$CONTAINER" printenv POSTGRES_DB 2>/dev/null || echo agent_cs)
    docker exec -i "$CONTAINER" pg_restore -U app -d "$DB_NAME" --clean --if-exists --no-owner < "$BACKUP_FILE"
else
    : "${DATABASE_URL:?未指定容器名时必须设置 DATABASE_URL（postgresql://user:pass@host:port/dbname）}"
    echo "[restore] 直连 DATABASE_URL -> $BACKUP_FILE"
    pg_restore -d "$DATABASE_URL" --clean --if-exists --no-owner "$BACKUP_FILE"
fi

echo "[restore] 完成。建议接下来跑一次 alembic check，确认恢复后的 schema 与代码里的迁移一致。"
