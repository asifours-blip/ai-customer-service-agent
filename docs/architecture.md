# 系统架构

> 本文说明组成与分层。决策细节见 [设计决策](decisions.md)，完整运行命令与历史验证见 [详细参考](reference.md)。

## 一句话

受控 LangGraph Agent + 本地 RAG + 权限隔离的工具层 + 幂等副作用 + 全链路 Trace + 可复现评测，跑在 FastAPI + PostgreSQL(pgvector) 上。

## 技术栈（锁定项见 D-001）

| 层 | 选型 | 说明 |
|---|---|---|
| Web | FastAPI + Pydantic | REST：auth / chat / orders / tickets / support/* / trace |
| ORM | SQLAlchemy 2.0 + Alembic | 迁移在 `migrations/`，pgvector 列为 `vector(512)` |
| 存储 | PostgreSQL 16 + pgvector | Docker Compose 一键起（含健康检查与初始化链） |
| Agent | LangGraph | 8 意图受控路由，`MAX_AGENT_STEPS=8` |
| LLM | DeepSeek（OpenAI 兼容） | agent=deepseek-v4-flash；评测 judge=deepseek-v4-pro；离线用确定性 FakeLLM |
| Embedding | BAAI/bge-small-zh-v1.5（本地） | 512 维；CI 用零依赖 FakeEmbedding（bigram hash） |
| 测试 | pytest（分层）+ ruff + mypy(strict) | 纯单测与真实 PG 集成、指定并发回归；当前范围见 [CI](../.github/workflows/ci.yml)，早期文档的 183 项是旧计数，不代表当前套件 |
| CI | GitHub Actions | ci.yml 全离线零付费；live-eval.yml 仅手动触发 |

## 模块分层

```
app/
├── api/            REST 路由（chat、orders、tickets、support 工单状态机、trace 复盘）
├── agent/          LangGraph 工作流（router 意图规则、graph 编排、service 会话服务）
├── rag/            loader → chunker → embedding(Fake/BGE) → pgvector 检索 → answerer(引用+拒答)
├── tools/          工具层：READ_ONLY(query_order/query_logistics/query_ticket) 与
│                   SIDE_EFFECT(create_ticket，幂等键+DB UNIQUE)
├── services/       业务事实与规则：PermissionService、EligibilityService(policy/rules.yaml)、
│                   database、errors
├── security/       guardrails 提示注入检测（软防线；硬防线在工具层权限校验）
├── llm/            LLMClient 协议 + FakeLLMClient + DeepseekClient（NO_PAID_API 策略闸、错误分类与重试，D-022）
├── kb/             知识库版本：上传校验 / 后台导入与校验 / 发布·回滚（比较交换）/ 重启恢复 / 引用追溯（D-021）
├── models/         User/Product/Order/Logistics/Ticket/TicketReply/KbVersion/KbDocument/KbChunk/KbAuditLog/
│                   Conversation(AgentSessionState 持久化字段)/Message/AgentTrace
└── static/         零构建前端（原生 ES modules + hash 路由）：登录页、客户端、客服工作台
eval/               数据集(9 类 110 条) / runner / metrics / judge / calibration / cost / report
knowledge_base/     12 篇中文 Markdown（front-matter 含 document_id/policy_version）
policy/             rules.yaml —— 退款 7 天/换货 15 天/保修 12 个月×30 天，机器可读规则源
scripts/            seed_db / bootstrap_kb（仅无生效版本时初始化知识库）/ run_eval(CLI)
```

## 关键架构原则

1. **LLM 不拥有业务权限**（D 系核心红线）：LLM 只做理解/规划/解释；事实、权限、资格、副作用全部在确定性业务代码（PermissionService / EligibilityService / 工具层）。Guardrails 只是软防线，工具层硬校验才是安全边界。
2. **状态落库**：AgentSessionState（active_order/ticket/product、pending_action 及其 id/expires_at）持久化在 Conversation 表，服务重启后确认流可恢复。
3. **副作用幂等**：create_ticket 由 pending_action_id 派生 idempotency_key；数据库 UNIQUE 约束 `(user_id, idempotency_key)`，正常命中与冲突恢复均校验请求指纹。同一用户的相同请求返回原工单，内容不同返回 409；不同用户的同名键互不影响（见 [回归](../tests/integration/test_ticket_idempotency_scope.py)）。
4. **两层 LLM 替身**：Fake（CI/离线，零付费、确定性）与真实 DeepSeek（live 评测/演示）共用 LLMClient 协议；Embedding 同理（Fake/BGE），由环境变量切换。
5. **可复现**：所有指标数字可由仓库产物重算——评测报告内嵌全部逐 case 结果，报告快照提交入库。

## 部署与运行

```bash
docker compose up --build      # db(pgvector) + backend：entrypoint 依次执行
                               # alembic upgrade head → seed_db(幂等) → bootstrap_kb(已有生效版本则跳过) → uvicorn
```

- 容器默认离线模式（NO_PAID_API=true + FakeEmbedding，不装 torch）——零成本可起，检索质量为演示级。
- 本地真实模式：`.env` 配 `DEEPSEEK_API_KEY`，`EMBEDDING_BACKEND=bge NO_PAID_API=false uvicorn app.main:app`。
- CI：push 自动跑 ci.yml（离线单测 + 真实 PG 集成）；真实评测只在 `live-eval.yml` 手动触发（成本护栏见 `docs/evaluation.md`）。

## 数据模型要点

- **Ticket**：状态机 OPEN→PROCESSING→RESOLVED→CLOSED；领取、状态迁移和回复锁定同一工单行，客服写操作要求当前领取人。`(user_id, idempotency_key)` UNIQUE，`request_fingerprint` 区分合法重放与内容冲突。
- **AgentTrace**：每次请求记录 query/intent/route/retrieval(top_score/top_k)/tool 调用+幂等键/tokens/latency/error_type/final_answer/policy_version 与 business_rule_version——`trace_id` 可完整复盘（演示台右侧面板直连）。
- **Conversation**：含会话状态持久化字段（见原则 2）。
