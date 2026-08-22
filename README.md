# Agent + RAG 智能客服与工单自动化平台

> 受控 Agent · 权限隔离 · 幂等工具 · RAG 引用溯源 · LLM 评测 · CI 离线零付费
>
> **状态：开发中（Phase 0~6 已完成，152 个测试全绿）** —— 受控 Agent 全链路就绪：RAG（引用/拒答）/ 四工具（幂等 create_ticket）/ 意图与实体消解 / 售后资格确定性判定 / CONFIRMATION 流 / 注入防护与 IDOR 拦截 / Trace 复盘 API / 零构建演示台（http://localhost:8000）。剩余：Phase 7 评测 → 8 CI/Docker 验收 → 9 发布。交接文档见 **[docs/HANDOVER.md](docs/HANDOVER.md)**，规格见 [docs/spec.md](docs/spec.md)，决策见 [docs/decisions.md](docs/decisions.md)。

模拟真实企业售后客服场景：用户提问 → 意图识别 → 受控 Agent 决策（RAG 知识问答 / 订单物流工单工具调用 / 售后资格确定性判定 / 转人工）→ 后端权限校验 → 执行 → 带引用的回答或拒答 → 全链路 Trace → 自动化评测（110 条评测集 + LLM Judge + 人工校准）。

## 核心设计原则

- **LLM 不拥有业务权限**：LLM 负责理解、规划和解释；业务代码负责事实、权限、资格和副作用
- **副作用工具幂等**：create_ticket 禁止普通重试，idempotency_key + DB 唯一约束
- **状态持久化**：Agent 会话状态（含待确认动作）落 PostgreSQL，服务重启可恢复
- **CI 永远离线**：FakeProvider/FakeEmbedding，零付费 API；真实评测仅手动触发且带成本硬闸

## 快速开始（Phase 8 完成后生效）

```bash
cp .env.example .env      # 填 DEEPSEEK_API_KEY（仅 live 评测需要）
docker compose up --build # backend :8000 + postgres(pgvector)
```

## 开发

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"        # CI 同款（无 torch）
# 本地真实 embedding 评测：pip install -e ".[dev,rag-local]"
.venv/Scripts/pytest -q -m "not integration and not live"
.venv/Scripts/ruff check . && .venv/Scripts/mypy app
```
