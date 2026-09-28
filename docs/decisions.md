# 架构决策记录（ADR）

> 每个关键决策一行：背景 → 决策 → 理由。与 docs/spec.md 同步维护。

## D-001 技术栈锁定：pgvector + LangGraph
决策来源：v1.1 补丁 §5。pgvector 而非 Qdrant：业务已用 PostgreSQL，多引一个向量服务无净收益；逃生条件=Docker 下 2h 仍不稳（须报告失败原因/已试方案/日志）。LangGraph 而非自写状态机：项目需要 State+条件路由+确认等待+Tool 节点+Trace，Graph 模型契合且生态成熟；逃生条件=≤3h Spike 阻塞。禁止双实现并存。架构分层：Graph=编排，Service=业务逻辑，Tool=对外操作。

## D-002 Embedding 锁定本地 BAAI/bge-small-zh-v1.5（外部审核修订②）
背景：DeepSeek 官方 API 无通用 /embeddings 端点，"OpenAI-compatible Chat ≠ 必有 Embeddings"。决策：真实 RAG 用本地 BGE（bge-small-zh-v1.5，512 维）；抽象 EmbeddingClient（LocalBGEEmbedding/FakeEmbedding）；CI 用 Fake。依赖影响：sentence-transformers+torch 列为可选依赖组 [rag-local]，CI/Docker 默认不装，本地评测环境安装。

## D-003 测试分层：SQLite 不做 PostgreSQL 替身（修订③）
决策：业务逻辑（Eligibility/Permission/Intent Parser/Cost/Retry Policy）纯单测+Mock/Fake Repository，零 DB 依赖；SQLAlchemy/Alembic/constraint/transaction/pgvector 全部真实 Docker PostgreSQL 集成测试（pytest marker `integration`）。理由：ORM 测试 SQLite 全绿≠PG 行为正确（JSON/UUID/事务语义差异）。

## D-004 幂等：SIDE_EFFECT_TOOL 禁止普通重试（修订①，P0）
背景：create_ticket 写入成功但响应超时→自动重试→重复工单。决策：Tool 分 READ_ONLY_TOOL（Retry≤2）/SIDE_EFFECT_TOOL（禁止无条件重试）；create_ticket 带 idempotency_key（由 pending_action_id 派生）+DB UNIQUE 约束；重复调用返回首张工单。工程意义：明确回答"有副作用的工具失败后为什么不能简单 retry"。

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

## D-015 离线评测组合与阈值实测（2026-08-23，Phase 7）
决策：离线评测 = FakeLLM（确定性生成，零付费）+ 本地 BGE（真实语义检索，免费），即 `EMBEDDING_BACKEND=bge` 跑 `scripts/run_eval.py`。纯 Fake 链路（CI 单测）仅做回归，不代表检索质量。
依据：BGE-small-zh 对本知识库实测分数分布——可答查询 top1 ∈ [0.532, 0.792]，无关查询 ∈ [0.334, 0.551]，重叠带 0.02；拒答阈值取 0.52（可答下界之下、无关上界之上不存在无损解，诚实接受边缘误判；唯一牺牲 rag_010"手表戴着游泳"被拒答）。
教训（已加回归测试）：FakeLLM 的 answer 为整段 prompt 回显（含知识库文本），任何基于答案关键词的 outcome 判定都会被回显污染——derive_actual_outcome 的关键词分支必须以 failed_tools 非空为前置条件，HUMAN 只看 route 不看文本。

## D-016 意图规则词表与优先级（2026-08-23，Phase 7）
router.py 词表优先级：TICKET → PRODUCT → POLICY → AFTER_SALES → LOGISTICS → ORDER → CHITCHAT。PRODUCT 先于 POLICY：防止"续航是多长时间"被政策词吞掉；POLICY 的政策词与产品词不重叠。AFTER_SALES 必须覆盖 要求退/换/修、申请、维修、售后、退换 等动作短语（缺"要求维修"曾致 tf_005/demo4 失败）。改词表必须全量回归 + 重跑评测。

## D-017 盲标集必须与 Judge 评同一批 live 答案（2026-08-23，Phase 7）
背景：κ 校准的本意是"人工 vs Judge 对**同一批答案**打分的一致性"。若人工标离线 FakeLLM 答案（整段 prompt 回显，不可读）而 Judge 评 live 答案，两者对象不同，κ 无意义；用户实测标第 1 条即无法判断（不知道拒答对不对、事实去哪查）。
决策：`--live` 运行同时导出 `blind_export_live.json`（与 judge_scores.json 同一次运行）；`--calibrate` 只接受 source="live" 的 blind_labeled.json（代码硬校验）。离线 `blind_export.json` 降级为"熟悉评分流程"用途。新增 `eval/calibration/LABELING_GUIDE.md`：知识库 12 篇清单、订单/物流/工单事实表（seed 恒定）、拒答判定法（事实源有而拒=0 分 / 确实没有而拒=2 分）、越权与注入判定（顺从=0 / 拒绝=2）、多轮指代上下文。强制暂停点①顺延至 live 运行导出之后。

## D-018 Judge 校准：v1→v3 迭代史与 κ 悖论（2026-08-23，Phase 7）
**v1（缺陷）**：参考要点是占位符"（见知识库政策）"，且无领域规则 → Judge 把正确的越权拒绝/拒答判 0 分；3/24 JSON 解析失败（未开 json_mode）。人工 24 条（23×2 + 1×0）vs Judge：Agreement 0.81、加权 κ≈-0.08 → FAIL。
**v2（修复真实缺陷）**：按 case 类别构造参考要点（注入/越权/拒答场景写明"正确行为=拒绝"；rag 给整篇 expected_document；其余给种子事实表）；多轮给完整对话链；json_mode；max_tokens 300→1200（v4-pro 是推理模型，reasoning 计入 completion，300 会在输出 JSON 前耗尽 → 4/24 空 content，实测复现）。结果 22/24，κ=0.478。分歧仅剩 mt_001/ord_001 两处 1-vs-2 宽严边界。
**v3（原则性澄清后停止）**：明确"评分只看事实正确性与完整性，格式（DELIVERED/P001）不扣分；理由必须忠于原文"。ord_001 达成一致，但 mt_003 反向翻为 1 分 → 仍 22/24，κ=0.314。**判定为过拟合信号，停止迭代**。
**κ 悖论分析**：人工标签 23×2+1×0 的近单一分布下，无权 κ≥0.70 数学上要求 24/24 完全一致（单条 1 分之隔 → κ≈0.65）；mt_001/mt_003 属不同人工标注者之间也未必一致的边界案例。结论：该 gate 在当前校准集上不可诚实达成；按规格执行"κ<0.70 不发布 Judge 指标"，确定性指标（任务成功/权限/注入/RAG 命中等）不依赖 Judge，不受影响。迭代全过程与三轮 judge_scores 保留在 git 历史与 calibration_report 中。

## D-019 工单领取与指派规则（2026-09-28，阶段 2 工作台）
领取：`POST /api/support/tickets/{id}/claim` 在 `SELECT ... FOR UPDATE` 行锁内判断 `assignee_id`，两名客服并发领取只有一方成功，另一方 409 `ALREADY_ASSIGNED`；本人重复领取幂等返回、不重复记事件。领取只指派不改状态，状态由领取人显式推进。可领取条件为「未指派且未 CLOSED」——新流程下只有 OPEN 会处于未指派，放宽到非 CLOSED 是为了让迁移前遗留的 PROCESSING/RESOLVED 未指派工单仍有人能接手。
指派人鉴权：状态迁移与客服回复都要求 `assignee_id == 当前客服`（未领取/他人领取 → 403），CLOSED 后双方都不能回复（409 `INVALID_STATE`）。
**不支持转交**：客服之间自行转交会让责任边界模糊，且需要额外的「被转交方是否接受」语义；当前没有主管角色，暂不开放。若后续需要，建议新增 SUPPORT_LEAD 角色的 reassign 接口（同样走行锁 + 事件记录），而不是让领取人自行释放。
反馈：RESOLVED/CLOSED 后客户本人可评分一次（1–5 + 评论），重复 409 `DUPLICATE`，不支持修改（评测集需要不可变样本）；行锁串行化 + `ticket_feedback_ticket_id_key` 唯一约束兜底。
处理记录：`ticket_events` 只追加，与业务写入同事务提交；DB 触发器拒绝 UPDATE/DELETE（测试清库用 TRUNCATE，不触发行级触发器）。客户视图隐藏内部字段：`assignee_id`、客服回复的 `author_id`、客服事件的 `actor_id`。

## D-020 确认卡片复用 graph 确认路径（2026-09-28，阶段 2 工作台）
原确认入口只有聊天发「确认」（`detect_confirmation` 正则）。新增 `POST /api/chat/confirm {session_id, pending_action_id, decision}` 供前端卡片使用，但**不另写执行路径**：API 只把 decision（YES/NO）和卡片上的 pending id 放进同一张图的初始状态；`check_pending` 先校验该 id 仍是会话当前 pending 且未过期（否则 STALE：不执行、不清除当前 pending），再与文本确认一样进入 `execute_confirmed`（REVALIDATE 权限与资格 → `create_ticket` 以 `pending_action.id` 为幂等键）。会话历史中记录为「确认」/「取消」，与手输一致；卡片与聊天两种入口可以混用，幂等键相同。

## D-021 知识库版本管理：草稿 / 发布 / 回滚与引用追溯（2026-09-28，阶段 3）
背景：原流程每次启动 `delete(KbChunk)` 后全量重建；chunk_id 全局唯一、无版本字段。核查结论：删除与插入同一事务，embedding 异常时不会提交，所以「失败即清空」只在语料为空时成立；但文档一改旧回答的引用就找不到当时原文、没有草稿与发布之分、评测重置会清掉线上知识库，这些问题确实存在。
数据模型：`kb_versions`（状态 CHECK、来源哈希、创建人、向量后端、进度、校验结果、失败原因）、`kb_documents`（每版本原文 + sha256）、`kb_audit_log`（只追加：与 ticket_events 同样用行级触发器拒绝 UPDATE/DELETE，阶段 3 审阅后追加，迁移 a6c3e8b2d417）；`kb_chunks.version_id` 非空，唯一键 `(version_id, chunk_id)`。迁移把现有 chunk 归入自动创建的 v1（ACTIVE，source=MIGRATION）；旧流程没存原文，v1 无文档行，来源哈希取 chunk 内容哈希。**空知识库不建 v1**，否则启动初始化会因「已有生效版本」而跳过。降级只能保留 ACTIVE 版本的 chunk（结构所限，有损）。
单一 ACTIVE：部分唯一索引 `uq_kb_versions_single_active`（`WHERE status='ACTIVE'`）在数据库层兜底。发布/回滚：单事务内先取全局 advisory lock 串行化，再比较调用方给出的 `expected_active_version_id` 与实际生效版本（不一致 409），然后「旧 ACTIVE → RETIRED（flush）→ 目标 → ACTIVE → 审计」一次提交。选择比较交换而不是「后到者覆盖」：只串行化的话，稍晚到的并发请求会在不知情的情况下覆盖前一个发布；比较交换下两个基于同一生效版本的发布恰好一个成功，与到达时间无关。
导入：上传同步校验（只收 .md、单文件 200 KB / 50 个 / 总 2 MB、UTF-8、必需 front matter、纯文件名防路径穿越、document_id 重复），任一不过整体 400 并逐条列出文件与检查项；通过后落草稿，BackgroundTasks 后台导入。导入期间持有事务级 advisory lock `(72001, version_id)`；chunk 行与校验同事务写入，校验不过回滚，失败版本不留 chunk。合并上传以当前 ACTIVE 的文档原文为底；迁移归档的 v1 没有原文，合并会得到「只有本次上传文件」的版本，因此后端对这种情况返回 400（check=mode），管理页禁用合并并提示只能完整替换。READY 前检查 chunk 数量（每篇至少 1）、向量维度（512 且为有限值）与冒烟查询（`kb_smoke_queries.yaml`，期望文档须在 top3 且分数过线上拒答阈值）。导入只写新版本，从不改动 ACTIVE。
重启恢复：应用启动（lifespan）时，对创建早于宽限期（`KB_RECOVER_GRACE_MINUTES`，默认 10 分钟）的 DRAFT/INGESTING 版本逐个 `pg_try_advisory_xact_lock`，拿得到锁（没有存活连接在导入）才标 FAILED；导入进程被杀后连接断开、锁自动释放。宽限期（阶段 3 审阅后追加）消除了多实例下「别的实例刚提交 DRAFT、后台任务尚未取锁」被误标的窗口；代价是真正中断的版本最多要等宽限期过后的下一次启动才被标记（期间只是停在 DRAFT/INGESTING，不可检索也不可发布，不影响线上）。
检索与引用：`store.search` 默认只 join ACTIVE 版本；引用来源增加 `version_id`，`GET /api/traces/{id}/citations` 按 `(version_id, chunk_id)` 取回原文（版本 RETIRED 仍可查）；版本化之前的旧 trace 没有版本号，接口如实返回「无法追溯」，不按 chunk_id 猜。
向量后端：聊天检索、后台导入、启动初始化统一用 `serving_retrieval()`（离线开关下固定 FakeEmbedding），版本记录 `embedding_backend`，发布时与当前服务不一致则拒绝，评测时与评测 embedder 不一致也拒绝。
启动与评测：entrypoint 改为 `bootstrap_kb.py`，仅在没有 ACTIVE 时导入 `knowledge_base/` 并生效（失败则容器启动失败）。`run_eval.py --kb-version dir|active|N`：dir 复用内容哈希与后端都一致的已校验版本、没有则导入新版本但不发布；评测检索固定在所选版本。评测重置只 TRUNCATE 业务表，知识库四张表不在范围内（顺带修复：原逐表 DELETE 会被 ticket_events 的只追加触发器拒绝）。
评测库守卫（阶段 3 审阅后追加）：评测会清空业务表，目标库只读自 `EVAL_DATABASE_URL`；未设置、与 `DATABASE_URL` 同库（规范化 host/port/dbname 后比较：主机不分大小写、localhost≡127.0.0.1≡::1、缺省端口 5432，与驱动名和用户名无关）、或库名不以 `_eval`/`_test` 结尾，一律拒绝运行且不连接任何库；CLI 通过后把全局会话工厂改绑到评测库。`reset_environment` 自身再校验一次「会话实际连接的库 = 通过守卫的评测库」，绕过 CLI 直接调用也删不到应用库。
权限：新角色 KB_ADMIN（种子 `kb_admin`），`/api/kb/*` 全部仅限该角色，客户与客服 403。

## D-022 外部接口准备：错误分类、重试、usage 未知与配置状态（2026-09-28，阶段 4）
核查现状：离线模式下意图分类是规则（`RuleBasedIntentClassifier`，线上从未接 LLM 分类器）；订单/物流/工单/售后/澄清/转人工/拒答全是确定性模板；只有 RAG 生成与寒暄经过 LLM，而离线 LLM 是 `FakeLLMClient`——把 user prompt（RAG 下即检索到的知识库原文）截断回显，并非生成。此前这些回答在界面上与模型回答没有区别。`NO_PAID_API=false` 但缺 key 时，`DeepseekClient.complete()` 抛 `ValidationFailedError`，被 `AgentService` 的通用兜底吞成「系统内部出现异常」（HTTP 200），不发请求但也不说明原因。
决策：
- **同一套代码**：真实模式只替换 LLM 客户端，Agent 图、RagService、Trace 落库完全共用；`DeepseekClient` 通过注入 `http_client`/`sleep` 用本地替身测试，不另写「真实模式流程」。
- **异常层级**（`app/llm/errors.py`）：未配置 / 鉴权 401·403 / 限流 429 / 服务端 5xx / 其他 4xx / 连接超时（含 TLS 握手）/ 连接失败 / 读超时 / 响应中断 / 坏响应（非 JSON、缺字段、类型不对、未知 finish_reason）/ 截断（length）/ 过滤（content_filter）。上下文含状态码、请求 id、Retry-After、attempts、usage；消息由客户端拼写，**不回显上游响应体**（服务商 401 文案常带部分 key），异常 `from None` 不链接 httpx 原始异常；key 以 `SecretStr` 保存，配置对象被打印也只显示 `**********`。
- **重试**：只重试 429 / 5xx / 连接失败 / 连接超时（请求未被处理，重发不重复计费）；指数退避 `LLM_RETRY_BASE_SECONDS·2^(n-1)`（封顶 `LLM_RETRY_MAX_SECONDS`），有 Retry-After 至少等它，超过 `LLM_RETRY_AFTER_MAX_SECONDS` 直接失败（不阻塞请求线程）；次数 `LLM_MAX_RETRIES`。读超时、响应中断、写到一半断开**不重试**：请求可能已被处理并计费。
- **usage 未知按上限计**：`LLMUsage.status` = reported / unknown / none。接口没返回 usage、读超时、响应中断、200 但无法解析时记 unknown，token 取上限：输入 ≤ UTF-8 字节数 + 32（字节级 BPE 的 token 数不超过字节数）、输出 ≤ max_tokens。Trace 的 prompt/completion_tokens 改为各次调用记录之和（计费口径），评测 `reconcile_actual` 沿用 token 求和并单独报告 `usage_unknown_calls`；judge 调用失败也按同一规则记账。顺带修正：售后节点为取政策而调用的 RAG 生成此前不计入 Trace token，现按调用记录计入。
- **缺配置不回退**：`NO_PAID_API=false` 时 `get_settings()` 一次列出全部缺口（key、base_url 合法且公网必须 https、MODEL_NAME、非默认 JWT_SECRET）并拒绝启动；运行期客户端仍自检，缺配置抛 `LLMNotConfiguredError`、0 请求。聊天接口对模型失败 / 知识库需重建返回 503（`detail.type`=MODEL_NOT_CONFIGURED / MODEL_CALL_FAILED / KB_REBUILD_REQUIRED + `category` + `trace_id`，message 为用户可读提示），不改用模板或回显答案；该轮消息与 Trace 照常落库（answer_mode=ERROR）。不经过模型的工具路由不受影响。
- **回答方式标识**：离线的回显与模板是既定设计，保留，但每条回答带 `answer_mode`（MODEL / OFFLINE_ECHO / TEMPLATE / ERROR），存入 Trace，前端逐条显示，刷新后从 Trace 取回。
- **配置状态**：`GET /api/system/config`（仅 KB_ADMIN，项目里唯一的管理员角色）与启动日志共用 `config_status()`：模型名、base_url 主机、key 是否已设置、NO_PAID_API、实际生效/配置的 embedding 后端、BGE 是否就绪（只读检查，不加载模型、不联网）。
- **BGE 接线**：删除默认 `HF_ENDPOINT=hf-mirror.com`（第三方镜像的供应链风险），仅当 `HF_ENDPOINT` 显式配置（含 .env）时在导入 huggingface_hub 前写入进程环境；`BGE_MODEL_PATH` 指定本地目录时只从该目录加载（`local_files_only`），目录不存在或不完整明确报错、不下载；`HF_HUB_OFFLINE` 时缓存缺失同样明确报错。检索前比对查询向量维度与版本导入时记录的维度（`checks.embedding_dim`，旧版本以列宽 512 为准），不一致抛 `KB_REBUILD_REQUIRED` 拒绝检索。
Trace：`agent_traces` 新增 `llm_calls`（JSON，每次调用的 outcome、耗时、attempts/retries、usage、状态码、请求 id）与 `answer_mode`（迁移 b7d2f4e9c813，旧记录为 NULL，不回填）。
