# 演示手册（六个 Demo）

> 环境：本地真实模式（`DEEPSEEK_API_KEY` + `EMBEDDING_BACKEND=bge`），登录身份 **客户 U001（demo_customer）**。截图为 2026-08-23 真实运行捕获，存于 `docs/assets/`。截图为阶段 1 的演示台界面；阶段 2 起改为登录页 + 客户端：以 `demo_customer / demo123` 登录后在「咨询」页发送下文各 Demo 的消息（前四条有快捷示例按钮），Demo4 可直接点击确认卡片上的「确认创建」，也可以回复【确认】——两者走同一确认路径与同一幂等键。

## Demo 1 — 知识库问答 + 引用（RAG）

点击 `Demo1 知识库+引用`（发送"这个耳机支持多久保修？"）。

预期：答案"整机保修 12 个月，充电盒保修 12 个月"，附 📎 引用《保修政策》《AirMusic Pro 产品说明》等来源；右侧 Trace 面板显示本次 trace_id、route=RAG、retrieval top_score。

![Demo1](assets/demo1_rag_citation.png)

## Demo 2 — 订单工具（权限内查询）

点击 `Demo2 订单工具`。

预期：走 query_order（READ_ONLY 工具）返回订单事实（状态/金额/时间）；Trace 中可见工具调用与参数。

## Demo 3 — 多轮指代（上下文实体解析）

先点 `Demo3 多轮(先发这条)`（查 A10001），等回答后再点 `Demo3 续·指代`（"那它大概什么时候到？"）。

预期：第二轮"它"无显式实体 → 从会话状态取 active_order_id=A10002 对应上下文 → 返回该订单物流；体现"显式状态 > 对话推断 > 猜测"的实体解析层级与状态落库。

![Demo3](assets/demo3_multiturn_coref.png)

## Demo 4 — 售后闭环（资格判定 + 确认 + 幂等）

点 `Demo4 售后闭环` → 系统核对订单与政策（EligibilityService 确定性判定，签收 3 天在退款 7 天窗口内）→ 回复"符合申请条件，回复【确认】即可" → 点 `确认（配合 Demo4）`。

预期（截图即实际结果）：确认触发 REVALIDATE + create_ticket；由于此前 live 评测已为该订单创建过 REFUND 工单 `T10003`（idempotency_key 由 pending_action_id 派生，可查 tickets 表验证），幂等键命中 DB UNIQUE，返回"该订单已存在同类进行中的工单: T10003，无需重复申请"——**这正是幂等设计的意义：重复确认/重试绝不产生第二张工单**。

![Demo4](assets/demo4_aftersales_confirm.png)

## Demo 5 — 越权拦截（IDOR 防护）

点 `Demo5 越权拦截`（查询他人订单 A20001，属 U002）。

预期："查询订单失败：用户无权访问该资源"。硬防线在工具层（`order.user_id == JWT 当前用户`），与话术无关；IDOR 评测集 10/10。

![Demo5](assets/demo5_idor_blocked.png)

## Demo 6 — 注入防护（Guardrails）

点 `Demo6 注入防护`（注入指令）。

预期：拒答并说明能力边界，Trace 记 `INJECTION_FLAGGED:*`；注入评测集 10/10。

![Demo6](assets/demo6_injection_guard.png)

## Trace 面板（全链路复盘）

任一消息发送后，右侧面板展示该次请求的 trace_id / intent / route / retrieval / 工具调用与幂等键 / tokens / latency / error_type / policy_version；`GET /api/trace/{trace_id}` 可回放完整链路。

![Trace](assets/trace_panel.png)

## 设计答疑要点

- **副作用工具为何不能简单 retry**：READ_ONLY 重试无害；create_ticket 盲目重试会重复开单——幂等键 + DB UNIQUE 让重试语义变为"返回首张工单"。
- **状态为何落库**：LangGraph State 是工作流内存态；pending_action（含过期时间）必须落 PostgreSQL，服务重启后确认流才可恢复。
- **IDOR 与注入的区别**：注入是"诱导系统做不该做的事"（软防线拦话术）；IDOR 是"合法接口访问不属于自己的资源"（硬防线拦事实）。
- **为什么离线和 live 数字几乎一样**：99.09% 任务成功由确定性链路（意图规则/工具/权限/资格/拒答阈值）决定，LLM 生成质量影响的 Judge 指标未发布（κ 未达标，见 `docs/evaluation.md`）。
