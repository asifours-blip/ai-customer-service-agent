# 备份恢复演练

脚本：`scripts/backup.sh`（pg_dump 自定义格式 `-Fc`）、`scripts/restore.sh`（pg_restore `--clean --if-exists`）。
两者都支持「容器名」或「`DATABASE_URL` 直连」两种用法，Windows 下的用法见脚本内注释（Git Bash / WSL 直接跑；
纯 PowerShell 用 `pg_dump`/`pg_restore` 直连宿主机映射端口）。

## 用法

```bash
# 备份（容器名 = 进容器执行 pg_dump；不传容器名则用 DATABASE_URL 直连）
./scripts/backup.sh <容器名> [输出目录，默认 ./backups]

# 恢复（目标库必须已存在；--clean --if-exists 会先清空同名对象再恢复）
./scripts/restore.sh <备份文件> <容器名>
```

## 演练记录（2026-09-28，本机真实执行，非模拟）

### 1. 造状态（`scripts/_drill_seed.py`，一次性演练工具，不是交付物）

在测试库 `csagent-pg-test`（`agent_cs_test`）里清空重建后，通过真实 HTTP 接口（`TestClient` 驱动应用，
不是直接写 SQL）构造：

- 会话 `drill-session-1`：RAG 问答 + 引用（citations）
- 客户对该回答提交反馈（`answer_feedback`，1 条）
- 会话 `drill-pending`：售后申请到"符合条件"但**不确认**，留一条待确认操作（`pending_action_id` 非空）
- 会话 `drill-confirmed`：售后申请（EXCHANGE 类目，避免与种子数据 T10001 的 REPAIR 撞 dedup）并确认，
  生成工单 `T10219`
- 知识库版本：在种子自带 v1（ACTIVE）基础上合并上传一份改动过的保修政策，发布为 v2 → v1 自动转
  `RETIRED`、v2 `ACTIVE`（同一时刻至多一个 ACTIVE，比较交换发布）
- 种子数据自带：`T10002` 已由 `SUPPORT001` 领取（`PROCESSING`）、完整处理记录（`ticket_events`）

验证造出的状态（直接查库）：

```
kb_versions:  1=RETIRED, 2=ACTIVE
conversations: demo-session-001 / drill-session-1 / drill-pending(pending) / drill-confirmed
tickets: T10001 REPAIR/OPEN, T10002 REFUND/PROCESSING, T10219 EXCHANGE/OPEN
answer_feedback: 1 条
```

### 2. 备份

```
$ ./scripts/backup.sh csagent-pg-test ./backups
[backup] 容器内 pg_dump：csagent-pg-test -> ./backups/agent_cs_20260928T140237Z.dump
[backup] 完成：./backups/agent_cs_20260928T140237Z.dump（103189 字节）
```

### 3. 另起临时容器并恢复

```
$ docker run -d --name csagent-restore-test -e POSTGRES_USER=app -e POSTGRES_PASSWORD=app \
    -e POSTGRES_DB=agent_cs_restored -p 55433:5432 pgvector/pgvector:pg16
$ ./scripts/restore.sh ./backups/agent_cs_20260928T140237Z.dump csagent-restore-test
[restore] 容器内 pg_restore：./backups/agent_cs_20260928T140237Z.dump -> csagent-restore-test
[restore] 完成。
```

恢复后直接查库确认：`kb_versions` 状态、`conversations`、`tickets`、`answer_feedback` 行数与备份前一致；
`kb_chunks.embedding` 列类型仍是 `vector(512)`（pgvector 扩展随 dump 正确恢复）。

### 4. 验证（`scripts/_drill_verify.py`，独立进程连恢复后的库 = 模拟应用重启）

```
DATABASE_URL=postgresql+psycopg://app:app@localhost:55433/agent_cs_restored \
  .venv/Scripts/python scripts/_drill_verify.py

[verify] 1/4 引用可取回原文 OK（5 条）
[verify] 2/4 待确认操作确认成功，新工单 {'T10220'}
[verify]     重复确认未新增工单（幂等生效）
[verify] 3/4 只追加触发器（ticket_events）恢复后仍生效
[verify] 4/4 重启后重复确认未新增工单（回答：这个问题我需要转接人工客服为您处理...）
[verify] 全部通过。
```

逐项对应验收要求：

| 要求 | 结果 |
|---|---|
| 引用能取回原文 | `GET /api/traces/{id}/citations` 恢复后仍返回 `found=true` + 原文内容（5 条） |
| 待确认操作仍能确认，复用原幂等键，不重复开单 | 对 `drill-pending` 发送「确认」→ 成功建单 `T10220`；再发一次「确认」→ 未新增工单（`pending_action` 已清空，第二次落回常规路由） |
| 只追加触发器仍然生效 | 对恢复后的库执行 `DELETE FROM ticket_events` 被行级触发器拒绝，报错含 `append-only` |
| 已确认会话重启后再发「确认」不产生第二张工单 | 用独立进程（新 `TestClient`，无进程内状态）连恢复后的库，对已确认的 `drill-confirmed` 再发「确认」，工单数量不变（会话已无待确认操作，落回转人工回答） |

### 5. 清理

```
docker rm -f csagent-restore-test
rm -rf ./backups
```

`csagent-pg-test`（测试库）与本仓库其余容器未受影响；`agent_cs_test` 之后被 `pytest -m integration` 的每用例
`db` fixture 正常清空重建，不依赖本次演练留下的状态。

## 结论

备份 / 恢复流程在本机用真实 Docker Postgres 完整跑通：pg_dump 自定义格式 → 独立临时容器 →
pg_restore → 应用连上验证。四项验收点全部通过，且是通过应用的真实 HTTP 接口（而不是直接读库）驱动验证的。
