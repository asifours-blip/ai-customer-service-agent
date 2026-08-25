# Ticket 并发幂等：从错误假设到数据库根因

## 问题

`create_ticket` 是会产生数据库副作用的 Tool。它通过 `pending_action`、由 pending action 派生的 `idempotency_key` 与数据库 UNIQUE 防止重复开单；但原有验证主要是串行 replay，不能证明真实 PostgreSQL 并发竞争下的语义。

本次目标是验证：同 key 的至少 10 个并发请求仅创建一张 Ticket；同一 pending action 的双 confirmation 仅执行一次副作用，竞争方得到受控的重复确认语义，而不是 `PendingRollbackError` 或 500。

## 排查而不是先修

最初猜测是 `idempotency_key` UNIQUE 冲突后 Session 没有 rollback。这个方向对真正的幂等竞争是合理的，但同 key × 10 的对照结果是 **1 create + 9 replay**，且最终只有 1 张 Ticket；它并没有稳定复现该假设。

双 confirmation 的竞争方仍出现内部错误。检查 PostgreSQL constraint metadata 后发现，冲突并非 `tickets_idempotency_key_key`，而是 `tickets_pkey`。为了隔离幂等变量，又运行了不同 key × 10 的并发创建：理论上都合法，修复前却只有 1 个成功，其余均为主键冲突。

根因是业务 ID allocator：`next_ticket_id()` 以 `MAX(id) + 1` 生成 `T<number>`。两个事务可以在写入前读到同一个 MAX 并生成相同 ID；事务不是自动互斥锁。

## 最小修复

没有把 `tickets_pkey` 当作 replay，也没有捕获所有 `IntegrityError`。主键冲突不等于同一业务动作，吞掉它会掩盖真实完整性错误。

修复保持 `T<number>` API 和字符串主键不变：PostgreSQL `ticket_id_sequence` 原子分配数字部分，应用层只负责格式化。Alembic migration 会根据已有最大合法 `T<number>` 对齐 sequence；历史 ID 不符合格式时 fail-fast，避免静默猜测数据含义。

ID allocator 修复后，真正的同 key 竞争才会落在 `tickets_idempotency_key_key`。仅当 PostgreSQL metadata 精确识别该 unique constraint 时，代码才会 rollback failed Session、按 idempotency key 查询 winner 并返回 controlled replay；其他 `IntegrityError` 继续传播。

Sequence 可以因 rollback 留下编号空洞。这是为并发下的原子唯一分配付出的正常代价，系统不宣称 gapless numbering。

## 回归证据

| 场景 | 受保护的语义 |
|---|---|
| Same key × 10 | 1 create + 9 replay；仅 1 张 Ticket |
| Different keys × 10 | 10/10 合法创建；Ticket ID 全唯一，无主键竞争 |
| Double confirmation | 一次副作用；另一请求受控 replay；无 `PendingRollbackError` 或 500 |

三项场景连续运行 20 轮，共 60 次均通过后，才作为显式 targeted regression 加入真实 PostgreSQL CI。完整 `tests_stress/` 不进入默认 CI：持续门禁只保留已证明稳定且与风险直接相关的 regression。

## 工程边界

这证明的是并发**正确性**，不是吞吐量 benchmark，也不等同于 exactly-once delivery。当前边界是：Sequence 负责唯一 ID 分配，idempotency UNIQUE 负责同一业务动作防重，`pending_action` 负责用户确认；权限与资格校验仍在确定性服务层，LLM 不承担最终副作用安全边界。
