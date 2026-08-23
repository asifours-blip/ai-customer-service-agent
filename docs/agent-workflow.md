# Agent 工作流（LangGraph 受控编排）

> 本文回答"一次用户消息进来后系统怎么走、每一步谁做主"。代码入口：`app/agent/graph.py`（编排）、`app/agent/router.py`（意图）、`app/agent/service.py`（会话服务与持久化）。

## 设计立场

**受控 Agent，不是自由 Agent。** 意图分类、实体解析、权限、资格判定、副作用全部是确定性代码；LLM 只在三个位置出现：意图兜底、RAG 答案生成、售后解释生成。步数上限 `MAX_AGENT_STEPS=8`，工具重试策略分类型（见"工具层"）。

## 一次请求的主路径

```
用户消息
  → guardrails 注入检测（软防线；命中 → INJECTION_FLAGGED，直接返回）
  → router 意图分类（规则优先级 + 词表；8 类）
  → RESOLVE_ENTITY（显式订单/工单号 > 对话状态 active_* > 猜测；歧义 → CLARIFY 追问）
  → 按意图分流：
      PRODUCT/POLICY_QA → RAG 检索 → 命中：LLM 生成带引用答案
                                    → 未过阈值：诚实拒答（abstained）
      ORDER/LOGISTICS/TICKET_QUERY → READ_ONLY 工具 → 权限校验 → 事实回答
      AFTER_SALES → 查订单 → 权限 → 政策 RAG → EligibilityService(确定性)
                  → 符合：生成 pending_action → 请求确认
                  → 不符合：说明政策原因（引用 policy_version）
      CHITCHAT/UNKNOWN → 兜底话术 / 转人工
  → 结果 + AgentTrace 落库（trace_id 返回前端）
```

## 意图规则（D-016）

优先级：**TICKET → PRODUCT → POLICY → AFTER_SALES → LOGISTICS → ORDER → CHITCHAT**。

- PRODUCT 先于 POLICY：防止"耳机的续航是多长时间"被政策词（多长时间/流程）吞掉。
- AFTER_SALES 词表覆盖动作短语：`要求.{0,3}(退|换|修)|维修|售后|退换|申请...`（缺"要求维修"曾致真实失败，已回归测试守门）。
- 改词表必须全量回归 + 重跑评测（`tests/agent/test_agent_units.py` 参数化意图表是守门员）。

## 实体解析（多轮核心）

三级来源：**显式状态 > 对话推断 > 猜测**。

- "帮我查订单 A10002" → active_order_id=A10002 写入会话状态（Conversation 表，重启可恢复）。
- "它的物流单号是多少" → 无显式实体 → 取 active_order_id → query_logistics(A10002)。
- 歧义（"我那个订单"且历史多个订单）→ CLARIFY 追问而不是猜。

## 售后确认流（CONFIRMATION + 幂等）

```
用户："A10001 的耳机坏了，要求维修/退款"
  → 查订单（权限：必须本人）→ 政策 RAG → EligibilityService 读 policy/rules.yaml：
        退款 7 天 / 换货 15 天 / 保修 12 个月（按订单状态与签收时间确定性判定）
  → 符合 → pending_action{type, payload, id, expires_at} 落库
         → 回复"已核对……符合申请条件，回复【确认】即可"
  → 用户"确认"
         → REVALIDATE（重查权限+资格，防确认窗口内状态变化）
         → create_ticket(idempotency_key = 派生自 pending_action_id)
         → DB UNIQUE(idempotency_key)：重复确认返回首张工单，不重复创建
  → 用户"不确认"/换话题 → pending_action 取消/重置
```

**真实实例**（2026-08-23 live 演示）：live 评测的 tik_006 已为 A10001 创建 REFUND 工单 `T10003`（idempotency_key 由 pending_action_id 派生，值见 tickets 表）；随后演示台再次走确认流，工具层命中 UNIQUE 约束，返回"该订单已存在同类进行中的工单: T10003，无需重复申请"——幂等不是理论，是当场生效的防线。截图见 `docs/demo.md` Demo4。

## 工具层：按副作用分类（D 系修订①）

| 类别 | 工具 | 重试策略 | 失败语义 |
|---|---|---|---|
| READ_ONLY | query_order / query_logistics / query_ticket | 瞬态错误自动重试 ≤2 | NOT_FOUND（查无此单如实说明）/ BLOCKED（越权拒绝） |
| SIDE_EFFECT | create_ticket | **禁止无条件重试**；只走幂等键 | DUPLICATE（命中幂等键返回首张工单） |

每个工具：Pydantic 参数校验 → 权限后端判定 → 超时 → 统一 `{success, data, error}` 返回；六类异常均有自动化用例。

## 安全双防线

1. **软防线**（`app/security/guardrails.py`）：5 类注入模式（指令覆盖/角色劫持/系统提示泄露/SQL 注入/数据外传，多行正则+空格容忍）。命中即拒答并记 `INJECTION_FLAGGED:*`。
2. **硬防线**（工具层）：所有查询工具强制 `order.user_id == 当前登录用户`（来自 JWT，不来自对话）；LLM 说什么都拿不到别人家的数据。IDOR 评测集 10/10 拦截。

两道防线独立生效：绕过软防线（措辞花样）也过不了硬防线（权限事实）。

## 可靠性

- LLM 超时/失败：READ_ONLY 路径降级为结构化事实回答（不依赖生成）；RAG 路径诚实报错，不编造。
- 人工兜底：无法处理/用户明确要求 → HUMAN 路由（转接话术 + 可建工单记录）。
- 全链路 Trace：见 `docs/architecture.md` 数据模型节，演示台右侧面板实时展示。
