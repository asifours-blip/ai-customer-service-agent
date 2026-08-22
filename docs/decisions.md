# 架构决策记录（ADR）

> 每个关键决策一行：背景 → 决策 → 理由。与 docs/spec.md 同步维护。

## D-001 技术栈锁定：pgvector + LangGraph
决策来源：v1.1 补丁 §5。pgvector 而非 Qdrant：业务已用 PostgreSQL，多引一个向量服务无净收益；逃生条件=Docker 下 2h 仍不稳（须报告失败原因/已试方案/日志）。LangGraph 而非自写状态机：项目需要 State+条件路由+确认等待+Tool 节点+Trace，Graph 模型契合且有求职识别度；逃生条件=≤3h Spike 阻塞。禁止双实现并存。架构分层：Graph=编排，Service=业务逻辑，Tool=对外操作。

## D-002 Embedding 锁定本地 BAAI/bge-small-zh-v1.5（外部审核修订②）
背景：DeepSeek 官方 API 无通用 /embeddings 端点，"OpenAI-compatible Chat ≠ 必有 Embeddings"。决策：真实 RAG 用本地 BGE（bge-small-zh-v1.5，512 维）；抽象 EmbeddingClient（LocalBGEEmbedding/FakeEmbedding）；CI 用 Fake。依赖影响：sentence-transformers+torch 列为可选依赖组 [rag-local]，CI/Docker 默认不装，本地评测环境安装。

## D-003 测试分层：SQLite 不做 PostgreSQL 替身（修订③）
决策：业务逻辑（Eligibility/Permission/Intent Parser/Cost/Retry Policy）纯单测+Mock/Fake Repository，零 DB 依赖；SQLAlchemy/Alembic/constraint/transaction/pgvector 全部真实 Docker PostgreSQL 集成测试（pytest marker `integration`）。理由：ORM 测试 SQLite 全绿≠PG 行为正确（JSON/UUID/事务语义差异）。

## D-004 幂等：SIDE_EFFECT_TOOL 禁止普通重试（修订①，P0）
背景：create_ticket 写入成功但响应超时→自动重试→重复工单。决策：Tool 分 READ_ONLY_TOOL（Retry≤2）/SIDE_EFFECT_TOOL（禁止无条件重试）；create_ticket 带 idempotency_key（由 pending_action_id 派生）+DB UNIQUE 约束；重复调用返回首张工单。面试价值：能回答"有副作用的工具失败后为什么不能简单 retry"。

## D-005 AgentSessionState 持久化到 PostgreSQL（修订④）
背景：内存 dict 存 session 状态→重启丢 pending_action、多 worker 不共享。决策：扩展 Conversation 表承载全部状态字段（含 pending_action_id/expires_at）。第一版不引 Redis。表述：LangGraph State 是工作流状态模型，业务关键状态最终落 PostgreSQL。

## D-006 评测集 110 条，IDOR 与注入分开（修订⑤）
背景：原 100 条无独立 Permission Case，"Permission Safety=100%"无数据来源。决策：新增 Permission/IDOR 10 条 + Tool Failure 5 条，总 110。Security 自动化测试 ≠ Agent 评测集，两者分开统计。

## D-007 删除 confidence 字段（修订⑥）
背景：模型自报置信度无统计意义，cosine 相似度≠答案正确概率。决策：RAG 输出仅 {answer, sources, abstained, retrieval:{top_score, top_k}}，top_score 明确语义为检索相似度。

## D-008 Key 管理：只走环境变量（修订⑦）
决策：统一 DEEPSEEK_API_KEY 环境变量；不从旧项目自动复制；不打印/不写日志/不进 README；Phase 0~6 NO_PAID_API=true（仅一次手动 Smoke 除外），真实 API 仅 Phase 7。

## D-009 发布 Gate：Secret Scan 后才公开（修订⑧）
背景：.env 即使后删，曾 commit 即留在 git 历史。决策：开发期本地 commit（可选 private remote）；Phase 9 完整测试→gitleaks 全历史扫描→检查历史→才转 public。

## D-010 简化 JWT + pbkdf2
决策：PyJWT HS256；口令哈希用标准库 hashlib.pbkdf2_hmac（迭代 60 万）。理由：简化认证是规格允许项，避免 bcrypt/argon 原生依赖；演示项目不存真实用户口令。

## D-011 gh CLI 本机不可用（2026-08-23 检查）
影响发布路径：D-009 的"private remote 中转"不可用 → 本地完整提交历史 + 用户提供 3 条 push 命令自行建仓上传；Secret Scan 仍在本机全历史执行。若后续安装 gh 可恢复原路径。

## D-012 执行语义：连续执行 + Phase Gate（外部审核）
决策：Phase N →实现→测试→验收→commit→PASS 才进下一 Phase；任一停止条件（测试失败/Docker 验收失败/规格冲突/换锁定技术/成本异常/Secret 泄漏）触发即 STOP 并报告，不绕过。两个强制暂停：Phase 7 盲标、DEEPSEEK_API_KEY 供给。

## D-013 SQLAlchemy UOW 跨 mapper 排序异常：seed 采用显式分层 flush（2026-08-23）
现象：本仓库 schema（User 完整列 × 宽 Conversation × Message 三级链）在 PostgreSQL 上，单次 commit 中无 relationship 的 FK 依赖排序失效——conversations 先于 users 插入导致 FKViolation。已隔离复现（精确模型副本 + postgres 即失败，sqlite 通过；SQLAlchemy 2.0.37/2.0.41/2.0.45/2.0.52 同症；insertmanyvalues_page_size=1 无效），属库级行为而非业务代码错误。
决策：seed_db.py 按依赖层级显式 db.flush()（users+products → orders → tickets → 其余）；运行时 API 每请求仅插入单一聚合，不受影响。后续若引入多聚合同事务写入，同样采用显式分层 flush。值得向上游报告。

## D-014 Docker Hub 直连不可达（2026-08-23）
本机网络环境无法直连 registry-1.docker.io。开发机构建：经 docker.m.daocloud.io 拉取后本地 retag 为官方镜像名（compose 文件保持官方名，CI 不受影响）。gh CLI 未安装：发布路径为本地完整历史 + 用户自行 push（见 D-009/D-011）。
