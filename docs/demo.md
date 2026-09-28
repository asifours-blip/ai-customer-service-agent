# 演示手册：登录、售后与客服闭环

以下图片由 `scripts/capture_screenshots.py` 在本地 `csagent-pg-test` 测试库重置到种子状态后，以 Playwright 操作真实浏览器采集。运行配置为 `NO_PAID_API=true`、`EMBEDDING_BACKEND=fake`；截图里的「离线回显」和「模板回复」不代表真实模型生成效果。脚本只接受 `localhost:55432/agent_cs_test`，运行前同时设置 `DATABASE_URL`、`TEST_DATABASE_URL` 为该地址。

## 1. 登录与知识库引用

用 `demo_customer / demo123` 登录。聊天页点击「这个耳机支持多久保修？」后，回答显示引用来源与知识库版本，「本轮依据」显示路由、回答方式和 Trace。

![登录页](assets/login.png)

![客户聊天：引用来源与本轮依据](assets/chat_citation.png)

## 2. 售后资格与确认卡片

发送「A10001 买的耳机用了三天坏了，可以退款吗」。规则层核对订单、签收天数与退款期限；聊天里显示售后资格判定，右侧「本轮依据」显示 `answer_mode` 的中文标识。待确认卡片要求客户明确确认后才开单。

![客户聊天：售后资格](assets/chat_eligibility.png)

![待确认卡片](assets/pending_confirmation.png)

点击「确认创建」，在新工单详情中查看 `CREATED` 事件和时间线；确认动作使用 pending action 的幂等键，重复请求不会产生第二张工单。

![客户工单详情与时间线](assets/customer_ticket_timeline.png)

## 3. 客服处理

用 `support_agent / demo123` 登录，进入未指派队列，领取刚创建的工单；回复客户并依次推进到「处理中」「已解决」。详情页保留领取、回复与状态变更记录。

![客服队列](assets/support_queue.png)

![客服工单详情：回复与已解决](assets/support_ticket_resolved.png)

## 4. 知识库版本与权限

用 `kb_admin / demo123` 进入知识库管理页，可以看到版本列表及 ACTIVE 状态。`demo_customer` 直接打开属于 `second_customer` 的 `T10002` 链接时，页面显示无权查看，接口返回 403，工单内容不泄漏。

![知识库版本列表](assets/kb_versions.png)

![客户越权访问被拒](assets/customer_idor_denied.png)

## 复现与边界

在本地确认 `csagent-pg-test` 正运行，并在项目 `.venv` 安装 Playwright 后执行。浏览器需已安装 Playwright Chromium（`.\.venv\Scripts\python.exe -m playwright install chromium`）；若该缓存缺失，脚本会尝试通过 Playwright 的 `chrome` channel 使用本机已安装的 Chrome。以下 PowerShell 命令可直接复制：

```powershell
$env:DATABASE_URL = 'postgresql+psycopg://app:app@localhost:55432/agent_cs_test'
$env:TEST_DATABASE_URL = $env:DATABASE_URL
$env:NO_PAID_API = 'true'
$env:EMBEDDING_BACKEND = 'fake'
.\.venv\Scripts\python.exe scripts/capture_screenshots.py
```

脚本会清空并重建这个测试库的业务数据和知识库版本；结束时关闭它启动的 uvicorn。真实模型效果、历史 live 评测数字和成本证据见 [evaluation.md](evaluation.md)，不能从这些离线截图推断。
