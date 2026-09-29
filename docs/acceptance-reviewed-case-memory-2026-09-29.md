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

阻塞证据：本轮启动既有 Docker Desktop 后，`C:\Users\汤佳宇\AppData\Local\Docker\backend.error.json` 报 `initializing Secrets Engine`，删除旧 `docker-secrets-engine/engine.sock` 时发生 WinError 1920；Docker 引擎不可用。该 socket 为 2026-09-22 已存在的 0 字节重解析点；初次排查时没有改动 AppData。之后按用户精确授权尝试保留改名，结果见下节；不得把该环境故障误写成本次功能测试失败。

## Docker 环境恢复与重启验收补记（20:24）

按用户精确授权、Docker 全退出时，分别把 `%LOCALAPPDATA%\docker-secrets-engine`（仅旧 `engine.sock`）和 `%LOCALAPPDATA%\Docker\run`（仅旧 `dockerInference`）保留改名为同级 `docker-secrets-engine.backup-20260929`、`run.backup-20260929`。没有删除文件、改权限或动 Windows 服务、WSL 数据盘。第一次改名后源路径出现空目录，原因未确认；旧 socket 确实位于备份。

第一次启动返回 `docker version --format '{{.Server.Version}}'` → `29.7.2`，`docker info` → `Containers=18;Running=3`；`docker ps -a` 只读列出 18 个原有容器：

```text
studio-public-showcase-20260928-api-1
studio-public-showcase-20260928-minio-1
studio-public-showcase-20260928-frontend-1
studio-public-showcase-20260928-mock-upstream-1
studio-public-ci-20260928-postgres-1
studio-public-ci-20260928-redis-1
studio-public-ci-20260928-minio-1
studio-public-showcase-20260928-postgres-1
studio-public-showcase-20260928-redis-1
studio-ops-flow-20260927-gateway-1
studio-ops-flow-20260927-api-1
studio-ops-flow-20260927-mock-upstream-1
studio-ops-flow-20260927-postgres-1
studio-ops-flow-20260927-redis-1
studio-ops-flow-20260927-minio-1
wutian-aigc-redis
wutian-aigc-postgres
wutian-aigc-minio
```

`docker volume ls` 只读列出原有匿名卷及 `ai-customer-service-agent_pgdata`、`studio-*`、`wutian_aigc_*` 等命名卷；仅证明资源元数据可见，**没有**逐卷数据读写校验或与完整迁移前基线逐项比对。没有手动创建、停止或删除单个容器/卷；Docker Desktop 正常退出后，原来自动运行的 3 个容器也随引擎停止。

第一次以 Docker 自带 `DockerCli.exe -Shutdown` 正常退出，退出码 0，Docker 进程归零；新 `engine.sock`、`dockerInference`（均 20:22:49 创建的零字节重解析点）仍留在新目录。只做一次正常重启（20:24:30）：`%LOCALAPPDATA%\Docker\backend.error.json` 在 20:24:37 更新，报 `initializing Inference manager`，无法移除新 `run\dockerInference`（WinError 1920；listener 路径语法错误），引擎不可达。向 Docker Desktop 窗口发送标准关闭消息后进程再次归零。**结论：首次启动暂时恢复，正常重启失败；Docker 环境仍不可用。** 保留两处备份及新 socket 原状，不循环挪目录。

原始未改动日志仍在 `%LOCALAPPDATA%\Docker\backend.error.json` 和 `%LOCALAPPDATA%\Docker\log\host\com.docker.backend.exe.log`；后者的 4105、4173、4187 行记录第一次失败改名后 `dockerInference` 阻断，本次重启的对应行可按 20:24:37 时间查找。此文仅是摘录，不冒充完整原始日志。

[Docker 官方发行说明](https://docs.docker.com/desktop/release-notes/) 在 4.89.0 和 4.93.0 均提及 stuck socket 启动修复；[Docker 官方仓库用户报告 #15064](https://github.com/docker/for-win/issues/15064) 指出 4.90 的相关修复在某些不可访问 AF_UNIX 重解析点上仍失败。这些来源不证明本机根因，也不能保证升级后的稳定性。本轮没有更新、重装或重置 Docker。

因此上节 PostgreSQL 集成、真实数据库迁移及 Chrome E2E 仍待执行。
