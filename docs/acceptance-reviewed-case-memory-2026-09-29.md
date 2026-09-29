# 物流破损经审核案例：本地验收记录（2026-09-29）

范围：仅 SUPPORT 角色、单组织、物流破损。领取客服从已解决的源工单确认破损类型和**实际发生过的处理步骤**后发布结构化案例；当前工单检索同类来源、相似与差异，并得到供人工审核的处置草案。源工单仍须经过原有 SUPPORT 权限访问。案例不保存客户描述原文；工单号是受限的来源链接，不宣称绝对无个人信息。未审核和已撤回案例不检索；规则版本变化的案例仅作历史参考，不充当现行授权。建议不自动退款、发消息或改订单。

本记录区分已运行的本地命令与待验收项。以下输出是本轮命令的终端结果摘录；没有将测试收集当作通过，也未调用付费模型。

## 已运行

```text
> .venv\Scripts\python.exe -m pytest -q -m 'not integration and not live' -p no:cacheprovider
223 passed, 183 deselected in 22.22s

> .venv\Scripts\ruff.exe check .
All checks passed!

> .venv\Scripts\mypy.exe app
Success: no issues found in 68 source files

> node --check app/static/js/views/support.js
exit code 0

> git diff --check
exit code 0

> .venv\Scripts\python.exe -m alembic heads
e4b7c2d9a301 (head)

> .venv\Scripts\python.exe -m alembic downgrade e4b7c2d9a301:c9f1a3e6b2d5 --sql
Running downgrade e4b7c2d9a301 -> c9f1a3e6b2d5
DROP TABLE damage_cases;

> .venv\Scripts\python.exe -m pytest --collect-only -q tests/integration/test_damage_case_memory.py -p no:cacheprovider
3 tests collected in 0.11s

> .venv\Scripts\python.exe -m pytest --collect-only -q tests_e2e/test_damage_case_memory_e2e.py -p no:cacheprovider
1 test collected in 0.29s
```

`alembic upgrade head --sql` 也以 PostgreSQL 方言生成了 `CREATE TABLE damage_cases` 和两项枚举检查约束；这是离线 DDL 生成，并非数据库迁移实跑。

## 待真实环境验收

- PostgreSQL 集成测试：新增 3 项 API/权限/版本/撤回/无业务副作用测试尚未运行；现有数据库相关回归尚未重跑。
- 真实 Chrome + uvicorn + PostgreSQL E2E：新增 1 项已收集，尚未运行；浏览器会点选源工单的人工审核、当前单检索与来源、撤回，然后留截图。
- Alembic 真实升级/降级/再升级尚未运行。
- 同类案例检索前后是行为对照，不含真人客服可用性打分，不是金融 META 模型复现。

阻塞证据：本轮启动既有 Docker Desktop 后，`C:\Users\汤佳宇\AppData\Local\Docker\backend.error.json` 报 `initializing Secrets Engine`，删除旧 `docker-secrets-engine/engine.sock` 时发生 WinError 1920；Docker 引擎不可用。该 socket 为 2026-09-22 已存在的 0 字节重解析点，本轮没有改动 AppData。任何路径改名修复均待用户精确授权；不得把该环境故障误写成本次功能测试失败。
