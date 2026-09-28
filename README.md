# Agent + RAG 智能客服与工单自动化平台

[![CI](https://github.com/asifours-blip/ai-customer-service-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/asifours-blip/ai-customer-service-agent/actions/workflows/ci.yml)

> 受控 Agent · 权限隔离 · 幂等工具 · RAG 引用溯源 · 全链路 Trace · 可复现评测 · CI 离线零付费

模拟真实企业售后客服：用户提问 → 意图识别 → 受控 Agent 决策（RAG 知识问答 / 订单物流工单工具调用 / 售后资格确定性判定 / 转人工）→ 后端权限校验 → 执行 → 带引用的回答或诚实拒答 → 全链路 Trace → 自动化评测（110 条评测集 + LLM Judge + 人工盲标校准）。

**状态：Phase 0~9 全部完成。默认质量门禁以 CI 与 `pytest --collect-only` 为准；真实 PostgreSQL 集成与 Ticket 并发回归在独立 CI job 执行。**

## Repository history

2026-08-23 是首次将已完成模块按功能切片入库的记录，并非线上迭代节奏。2026-08-24 的 PostgreSQL sequence 修复是公开后的真实 Ticket ID 并发缺陷；根因、最小修复与回归证据见 [Ticket concurrency case study](docs/ticket-concurrency-case-study.md)。当前行为以 `main` 和 GitHub Actions 为准。

| Demo 1 · RAG 引用 | Demo 4 · 售后确认+幂等 | Demo 5 · 越权拦截 |
|---|---|---|
| ![RAG](docs/assets/demo1_rag_citation.png) | ![售后](docs/assets/demo4_aftersales_confirm.png) | ![越权](docs/assets/demo5_idor_blocked.png) |

## 核心设计决策与评测结果

**1. 这是什么？** FastAPI + LangGraph + PostgreSQL(pgvector) 的智能客服后端：8 意图受控路由、本地 BGE 检索、4 个权限隔离工具、幂等开单、注入/越权双防线、全链路 Trace、110 条评测集与 CI。

**2. 为什么"受控"？** LLM 不拥有业务权限——它只做理解/规划/解释；事实、权限、资格、副作用全在确定性代码（PermissionService / EligibilityService / 工具层）。Guardrails 是软防线，工具层权限校验是硬防线，两道独立生效。

**3. 副作用怎么防重？** create_ticket 禁止普通重试；确认流生成 pending_action（落库、带过期时间），确认时 REVALIDATE 后以 pending_action_id 派生 idempotency_key + DB UNIQUE——重复确认返回首张工单（demo 实测见 `docs/demo.md` Demo4）。

**4. RAG 怎么做、怎么防编造？** 12 篇中文知识库 → 确定性 chunk → 本地 bge-small-zh-v1.5（512 维 pgvector）→ 拒答阈值 0.52（实测标定：可答 [0.532,0.792] vs 无关 [0.334,0.551]）→ 命中才生成、必带 📎 引用；未过阈值诚实拒答（abstention 10/10）。

**5. 多轮怎么处理？** 实体解析三级（显式单号 > 会话状态 active_* > 猜测，歧义 CLARIFY）；会话状态持久化在 PostgreSQL，服务重启后确认流可恢复。

**6. 安全怎么验证？** 注入集 10/10 拦截（5 类模式含多行/空格容忍）、IDOR 集 10/10 拒绝（查他人订单/工单）、工单状态机禁逆向、JWT 身份与资源属主强绑定——全部自动化在评测集与测试套里，不是人工抽查。

**7. 怎么评测的？** 110 条 9 类（含拒答/注入/越权/工具失败分开统计）；离线（FakeLLM+本地 BGE，零 API 费）与 live（deepseek-v4-flash）双环境；LLM Judge（v4-pro）+ 24 条人工盲标校准集做 κ 校准；成本 preflight 软闸 $1/硬闸 $2 + 逐笔对账。

**8. 评测结果如何？**（live 真实运行，报告快照在 `eval/reports/`，可复现）

| 指标 | 结果 |
|---|---|
| 任务成功 | **109/110 = 99.09%**（唯一失败 rag_010 短查询边界，如实保留） |
| 意图 / 工具选择 / 工具参数 | 98.81% / 100% / 100% |
| 权限安全（IDOR）· 注入拦截 | **100% · 100%** |
| RAG 命中 / 引用正确 / 拒答正确 | 93.33% / 93.33% / 100% |
| 延迟 p50/p95 | 5ms / 3.25s（端到端） |
| live 真实成本 | $0.0082（chat 18.5K + judge 8.4K tokens，逐笔对账） |

**9. 局限是什么？**（诚实清单，详见 `docs/evaluation.md`）
- rag_010"手表戴着游泳"被拒答：BGE 短查询边界，0.52 阈值无无损解；
- **Judge 指标未发布**：κ 校准未达 0.70（三轮迭代后仍 22/24）——根因是人工标签近单一分布下的 κ 悖论 + 1-vs-2 边界案例，过程完整记录在 `docs/decisions.md` D-017/D-018；
- CI/容器的 FakeLLM/FakeEmbedding 只守回归，不代表生成与检索质量；
- 容器演示模式检索为演示级（真实模式用本地 BGE）。
- 知识库冒烟查询集（`kb_smoke_queries.yaml`）只在 fake 后端下实测（期望文档分数均 ≥ 0.27，阈值 0.22）；bge 后端（阈值 0.52）下尚未实测，首次用 bge 导入的版本若因冒烟查询失败，需按实际分数复核题目或阈值（D-021）。

## 快速开始

```bash
cp .env.example .env        # 填 DEEPSEEK_API_KEY（仅 live 评测/真实演示需要；容器离线模式不需要）
docker compose up --build   # backend :8000 + postgres(pgvector)，entrypoint 自动迁移/种子/知识库初始化（已有生效版本则跳过）
# 打开 http://localhost:8000 —— 登录后按角色进入客户端或客服工作台
```

### 客户端与客服工作台

零构建前端（原生 JS + ES modules，由 FastAPI `/static` 提供），hash 路由（如 `#/tickets/T10001`），刷新、直接打开链接、重新登录后都回到同一视图；token 存 sessionStorage，任何接口 401 → 登录页 → 登录后回到原页面。

- **客户**：会话列表与历史、聊天（引用来源 + 售后资格判定依据 + Trace）、待确认操作卡片（确认 / 取消）、我的订单、我的工单（处理记录时间线、补充回复、解决后评价一次）。
- **客服**：工单队列（未指派 / 我的 / 全部 + 状态筛选）、领取、时间线、回复、状态推进（仅领取人）。

- **知识库管理员**：版本列表与状态、上传 .md（逐文件校验）、后台导入进度与失败原因、发布、回滚、版本详情（校验结果 / 文档 / 操作记录）。

前端按钮只是体验层，后端每个接口独立鉴权（角色、资源属主、领取人、工单状态）。规则见 `docs/decisions.md` D-019 / D-020 / D-021。

### 知识库版本管理（D-021）

- 状态：`DRAFT → INGESTING → READY | FAILED`，`READY → ACTIVE`（发布），原 `ACTIVE → RETIRED`；回滚 = `RETIRED → ACTIVE`。检索只查 ACTIVE 版本。
- 同一时刻至多一个 ACTIVE：数据库部分唯一索引兜底；发布/回滚在单事务内完成，并以请求携带的 `expected_active_version_id` 做比较交换，并发发布只有一个成功（另一方 409）。
- 上传后后台导入，READY 之前自动检查 chunk 数量、向量维度，并跑 `kb_smoke_queries.yaml` 冒烟查询（每题必须命中指定文档且过拒答阈值）；任一不过即 FAILED 并写明原因，不影响当前生效版本。
- 进程重启：创建已超过宽限期（`KB_RECOVER_GRACE_MINUTES`，默认 10 分钟）且没有存活连接持有导入锁的 DRAFT/INGESTING 版本，在启动时被标为 FAILED。
- 上传方式：合并（以当前生效版本的原文为底，按 document_id 覆盖或新增）或完整替换；当前生效版本是迁移归档、没有保存原文时只允许完整替换。
- 回答引用记录 `(version_id, chunk_id)`；旧版本只退役不删除，`GET /api/traces/{trace_id}/citations` 与聊天里的「查看引用原文」始终能取回当时的原文。
- 启动：只有没有任何 ACTIVE 版本时才把 `knowledge_base/` 导入为初始版本并生效（`scripts/bootstrap_kb.py`），否则什么都不做。

种子测试账号（仅本地演示 / 测试库使用，定义在 `scripts/seed_db.py`），口令均为 `demo123`：

| 用户名 | 角色 | 说明 |
|---|---|---|
| `demo_customer` | 客户 U001 | 主演示用户（A10001 签收 3 天，可退款） |
| `second_customer` | 客户 U002 | 越权测试用的「别人」 |
| `support_agent` | 客服 SUPPORT001 | 已领取 T10002 |
| `support_agent2` | 客服 SUPPORT002 | 第二名客服（领取竞争） |
| `kb_admin` | 知识库管理员 KBADMIN001 | 上传 / 发布 / 回滚知识库版本（`#/kb`） |

## 开发

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1

python -m pip install -e ".[dev]"              # CI 同款（无 torch）
python -m pip install -e ".[dev,rag-local]"    # 需要本地 BGE 时
python -m pytest -q -m "not integration and not live"   # 离线单测（秒级）
docker compose up -d db && python -m pytest -q -m integration  # 真实 PG 集成
python -m ruff check . && python -m mypy app eval
```

### 端到端测试（真实浏览器，默认不进 CI 快速 job）

`tests_e2e/` 用 uvicorn 子进程启动真实应用、连真实 PostgreSQL 测试库，Playwright（Chromium）驱动页面。**每个用例都会清空并重建测试库数据**，所以必须同时设置 `DATABASE_URL` 与 `TEST_DATABASE_URL` 且指向同一个测试库，否则直接退出。

```bash
python -m pip install -e ".[dev,e2e]"
python -m playwright install chromium          # 浏览器下载到用户缓存目录，不装系统级软件
export DATABASE_URL=postgresql+psycopg://app:app@localhost:5432/agent_cs_test
export TEST_DATABASE_URL=$DATABASE_URL
python -m pytest tests_e2e -v                  # 加 --headed 可看浏览器操作
```

### 真实模型接入（NO_PAID_API=false）

离线与真实模式是同一套代码，只替换 LLM 客户端。`NO_PAID_API=false` 时启动即校验 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`（公网须 https）、`MODEL_NAME`、非默认 `JWT_SECRET`，缺一项拒绝启动。模型调用失败按类别（未配置 / 鉴权 / 限流 / 5xx / 超时 / 连接 / 中断 / 坏响应 / 截断 / 过滤）返回用户可读提示，不回退到模板答案；只重试 429、5xx、连接失败与连接超时，读超时与响应中断不重试并按上限计费。每条回答标注回答方式（模型生成 / 离线回显 / 模板回复），Trace 记录每次模型调用。管理员可在 `GET /api/system/config` 查看配置状态（只显示 key 是否已设置）。BGE 默认从官方源加载；需要镜像时显式设置 `HF_ENDPOINT`，也可用 `BGE_MODEL_PATH` + `HF_HUB_OFFLINE=1` 纯本地加载。详见 `docs/decisions.md` D-022。

## 评测复现

```bash
# 评测会清空业务表：必须指定独立评测库（库名以 _eval 或 _test 结尾、且不同于 DATABASE_URL），否则拒绝运行
export EVAL_DATABASE_URL=postgresql+psycopg://app:app@localhost:5432/agent_cs_eval
DATABASE_URL=$EVAL_DATABASE_URL alembic upgrade head                   # 评测库首次使用前迁移（库需先建好）
EMBEDDING_BACKEND=bge python scripts/run_eval.py                        # 离线全量（零 API 费）
python scripts/run_eval.py --kb-version active                          # 评测当前生效版本（也可填版本号；默认 dir＝与 knowledge_base/ 一致的版本）
EMBEDDING_BACKEND=bge NO_PAID_API=false python scripts/run_eval.py --live   # 真实评测（成本护栏内）
python scripts/run_eval.py --calibrate eval/calibration                 # κ 校准
```

## 文档

| 文档 | 内容 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | 架构分层、技术选型、部署 |
| [docs/agent-workflow.md](docs/agent-workflow.md) | 工作流、意图/实体/确认流、幂等、双防线 |
| [docs/evaluation.md](docs/evaluation.md) | 评测方法论、全部真实数字、κ 校准史、成本对账 |
| [docs/demo.md](docs/demo.md) | 六个 Demo 操作手册 + 截图 + 答疑要点 |
| [docs/ticket-concurrency-case-study.md](docs/ticket-concurrency-case-study.md) | PostgreSQL 并发下定位 Ticket ID 竞态、最小修复与 CI 回归 |
| [docs/decisions.md](docs/decisions.md) | D-001 ~ D-021 全部工程决策（含踩坑与理由） |
| [docs/spec.md](docs/spec.md) | 三方合并规格（唯一事实源） |

## 红线（本仓库的工程纪律）

所有指标数字可由仓库产物复现，禁止伪造；不删测试换绿、不隐藏失败；API Key 只走环境变量（未过全历史 Secret Scan 不公开）；任何改动三绿（ruff/mypy/pytest）后才提交；决策必须记录 `docs/decisions.md`。
