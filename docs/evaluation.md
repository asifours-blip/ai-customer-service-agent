# 评测体系

> 本文回答"怎么知道系统好不好、数字从哪来、哪些数字可信到什么程度"。所有数字可由仓库产物复现：报告快照在 `eval/reports/`，逐 case 结果内嵌在报告 JSON 中，数据集在 `eval/datasets/`。

## 数据集：110 条，9 类

| 类别 | 数量 | 考什么 |
|---|---|---|
| rag（商品/政策问答） | 30 | 检索命中、引用正确、答案信号 |
| order | 15 | 订单工具选择/参数/事实 |
| logistics | 10 | 物流查询与多轮指代 |
| ticket | 10 | 工单查询与售后闭环 |
| multi_turn | 10 | 上下文实体解析（"它"指代） |
| abstention | 10 | 知识库外问题必须拒答（不许编造） |
| injection | 10 | 提示注入必须拦截（独立统计） |
| idor（越权） | 10 | 查他人资源必须拒绝（独立统计） |
| tool_failure | 5 | 查无此单/重复创建等失败语义 |

每条含 `expected_intent/expected_tools/expected_outcome/expected_signal/expected_document`；outcome 枚举 SUCCESS/REFUSED/BLOCKED/CLARIFY/NOT_FOUND/DUPLICATE。

## 指标与结果（两个环境，均为真实运行产物）

| 指标 | 离线（FakeLLM+本地 BGE） | live（deepseek-v4-flash + BGE） |
|---|---|---|
| 任务成功 | 109/110 = **99.09%** | 109/110 = **99.09%** |
| 意图准确 | 83/84 = 98.81% | 83/84 = 98.81% |
| 工具选择 / 参数 | 49/49、56/56 = 100% | 49/49、57/57 = 100% |
| 权限安全（IDOR 集） | 10/10 = **100%** | 10/10 = **100%** |
| 注入拦截 | 10/10 = **100%** | 10/10 = **100%** |
| RAG 命中 / 引用正确 | 28/30 = 93.33% | 28/30 = 93.33% |
| 拒答正确（abstention） | 10/10 = 100% | 10/10 = 100% |
| 延迟 p50/p95 | — | 5ms / 3.25s（端到端，含编排与 DB） |
| 系统错误率 | 0（注入拦截单列 guardrail_blocked=10，不计错误） | 0（同左） |

**唯一失败**：rag_010"手表能戴着游泳吗"——BGE 短查询检索到错误 chunk 被拒答。这是 0.52 拒答阈值下的真实边界（见下），诚实保留，不修数据集凑数字。

## Embedding 与拒答阈值（D-002/D-015）

- 本地 BAAI/bge-small-zh-v1.5（免费，脱离付费 API）；CI 用 FakeEmbedding（bigram hash，只做回归不代表检索质量）。
- 拒答阈值实测标定：可答查询 top1 ∈ [0.532, 0.792]，无关查询 ∈ [0.334, 0.551]，重叠带 0.02 不存在无损解 → 取 **0.52**（可答下界之下），代价即 rag_010。

## Judge 与 κ 校准（D-017/D-018，如实记录）

- Judge = deepseek-v4-pro，rubric 0/1/2，按 case 类别给真实参考要点（越权/注入/拒答场景写明"正确行为=拒绝"），仅 live 运行。
- **人工盲标**：独立人工标注 24 条分层抽样 live 答案，构成人工盲标校准集（事实核查依据 `eval/calibration/LABELING_GUIDE.md`：知识库清单+种子事实表）。人工与 Judge 评同一批答案（D-017）。
- **三轮迭代史**：v1 占位符参考 → 把正确拒绝判 0（FAIL）；v2 真实参考+json_mode+max_tokens 1200（v4-pro 推理计入 completion，300 上限曾产生 4/24 空 content，实测定位）→ 22/24，κ=0.478；v3 事实优先于格式的边界澄清 → 仍 22/24，κ=0.314，分歧项轮换 = 过拟合信号，**停止迭代**。
- **结论：κ 未达 0.70，Judge 分数不发布**（规格红线）。根因是 κ 悖论：人工标签 23×2+1×0 的近单一分布下，无权 κ≥0.70 数学上要求 24/24 全对；剩余分歧（mt_001/mt_003）属不同人工标注者之间也未必一致的 1-vs-2 边界。确定性指标不受影响。全过程与三轮评分保留在 git 历史。

## 成本护栏与对账（真实数字）

- preflight 软闸 $1（`--force` 可越）/ 硬闸 $2（不可越）；价格表 `config/model_pricing.yaml`（快照 sha256 记录在报告）。
- live 实际消耗：chat 15,379+3,127 tokens + judge 8,411 tokens = **$0.0082**（逐笔来自 usage 记录，预估 $0.0387 偏保守，差值如实保留）。Judge v2/v3 迭代另耗 $0.0089+$0.0112，均逐笔记录于 judge_scores.json。
- 曾有三处对账缺陷（token 汇总恒 0 / 对账先于 Judge / 注入误计错误率），已修复并加回归测试（`15d7348`）。

## 复现命令

```bash
docker compose up -d db
# 评测会清空业务表：必须指定独立评测库（库名以 _eval 或 _test 结尾、且不同于 DATABASE_URL），否则拒绝运行
export EVAL_DATABASE_URL=postgresql+psycopg://app:app@localhost:5432/agent_cs_eval
DATABASE_URL=$EVAL_DATABASE_URL alembic upgrade head                   # 评测库首次使用前迁移（库需先建好）
EMBEDDING_BACKEND=bge python scripts/run_eval.py              # 离线全量（零 API 费）
EMBEDDING_BACKEND=bge NO_PAID_API=false python scripts/run_eval.py --live   # 真实评测（需 .env key）
python scripts/run_eval.py --calibrate eval/calibration       # κ 校准
```

CI：push 自动跑离线单测 + 真实 PG 集成（`.github/workflows/ci.yml`）；真实评测仅 `live-eval.yml` 手动触发。

## 知识库版本对比

`scripts/run_kb_diff.py` 复用同一套执行器（`build_agent` / `select_kb_version` / `reset_environment` / `run_all`），
让同一组评测样本分别在知识库版本 A、B 上各跑一遍，按 `case_id` 对齐后逐题比较 outcome / 是否拒答 / 引用文档集合 /
回答正文，输出到 `eval/reports/generated/kb_diff_vA_vB_<时间戳>.json`（未变化的题目也在报告里，只是 `changed=false`）。

```bash
export DATABASE_URL=postgresql+psycopg://app:app@localhost:5432/agent_cs        # 仅用于安全闸比较，不会被连接
export EVAL_DATABASE_URL=postgresql+psycopg://app:app@localhost:5432/agent_cs_eval
python scripts/run_kb_diff.py --a 1 --b 2                   # 版本 1 vs 版本 2（READY/ACTIVE/RETIRED 均可）
python scripts/run_kb_diff.py --a active --b 3 --category rag   # 只跑 rag 类、更快
```

本地曾用两个真实知识库版本（在保修政策文档末尾追加一段说明生成 v2）跑过一次全量对比：30 条 rag 样本里
1 条（`rag_025`「维修一般需要多长时间？」）从 v1 的 SUCCESS 变成 v2 的 REFUSED——追加内容改变了该文档的
分块/向量，导致检索分数掉到拒答阈值以下。纯函数部分（差异比较逻辑）见 `tests/evaluation/test_kb_diff.py`。

## 诚实的局限清单

1. rag_010 短查询边界（上述）。
2. FakeLLM（CI/离线）答案是 prompt 回显，不代表生成质量；离线指标代表"检索+编排+工具"链路，生成质量以 live 运行为准。
3. BGE 阈值重叠带 0.02：0.52 是权衡解，不是无损解。
4. Judge 指标因 κ 未达标不发布（如上）；如需发布需扩大校准集并引入更多 0/1 分层样本。
5. 容器演示模式用 FakeEmbedding，检索质量为演示级（本地真实模式用 BGE）。
