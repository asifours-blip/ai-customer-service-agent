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

## 当时待真实环境验收（2026-09-29；后续结果见末节）

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

因此截至 2026-09-29 20:24，PostgreSQL 集成、真实数据库迁移及 Chrome E2E 仍待执行；后续结果见下节。

## 官方升级后补齐真实环境验收（2026-09-30）

用户批准从 Docker Desktop 4.86.0 做官方就地升级。冷态保留备份于 `D:\DockerUpgradeBackup-20260929`：`docker_data.vhdx`（11,521,753,088 B，SHA-256 `4786C7652C82D7E27268B93FD877C16DE9EEF351EE3D3EAB4DDDBB46966276E9`）、`ext4.vhdx`（100,663,296 B，SHA-256 `24076EA54BB4F2A90C91CC8FF81178944A964430C429A7AF23203BB0CD8A68F3`）与 `settings-store.json`（299 B，SHA-256 `95E2F54A19919FF3CF3259E840982BB80E0BBF9F2294F5B32B64A4C0FF2C2FE2`）；复制时逐文件源/目标哈希匹配。这是文件级备份证据，不声称恢复演练通过。两处 2026-09-29 socket 目录备份仍保留，`D:\DockerData` 数据位置未变。

官方安装器 627,791,792 B 的 SHA-256 与 Docker 发布值 `c139124c9cf71477dc565c3c0ea5a18f90b93d68ebe9aaa848a065960416c0bc` 匹配，Authenticode 为 `Valid`、发布者 `Docker Inc`；`install --user --quiet` 退出码 0，安装版本 `4.93.0.240920`。原始记录：`D:\DockerUpgradeBackup-20260929\installer-verification.json`、`install-result.json`、`installer-stdout.log`、`installer-stderr.log`。首次启动及正常 `DockerCli.exe -Shutdown` 后再启动，Engine 均为 `29.8.1`；两次只读枚举的原有 18 容器名、40 卷名一致，重启后运行中容器 3 个；完整清单在 `post-upgrade-first-start.json` 与 `post-upgrade-restart.json`。这证明此次启动稳定及资源元数据可见，不等于逐卷数据完整性校验。Docker Desktop 保持运行，供另一独立验收使用。

在本轮专用 `csagent-pg-test` 容器、`127.0.0.1:55432/agent_cs_test` 上，真实 Alembic `upgrade head → downgrade c9f1a3e6b2d5 → upgrade head` 均退出 0，`damage_cases` 表实查依次为 `t/f/t`。完整输出见 `D:\DockerUpgradeBackup-20260929\customer-tests\alembic-upgrade-1.log`、`alembic-downgrade.log`、`alembic-upgrade-2.log`，状态与退出码见 `migration-result.json`。新增案例集成用例 `3 passed, 1 warning`，原始输出 `damage-case-integration.log`、退出码 `damage-case-integration-result.json`。相关工作台数据库回归 `20 passed, 6 warnings`，原始输出 `workbench-db-regression.log`、退出码 `workbench-db-regression-result.json`。警告均为现有 Alembic `path_separator` 弃用提示。

真实 Chrome + uvicorn + PostgreSQL 的案例发布→当前工单检索/来源→撤回链 `1 passed, 1 warning`，原始输出 `damage-case-e2e-final.log`、退出码/截图清单 `damage-case-e2e-final-result.json`，截图 `case-memory-ui.png`（80,085 B）。第一次 E2E 使用 `localhost` 时 PostgreSQL 连接/启动超过夹具 30 秒健康检查；改 `127.0.0.1` 后进入 UI，发现测试定位器同时匹配两条 `alert`，遂限定审批提示文本，并扩大截图视口以完整留证。两次未通过的原始输出仍保留为 `damage-case-e2e.log`、`damage-case-e2e-ipv4.log`；修正后的两次通过输出也保留为 `damage-case-e2e-locator.log`、`damage-case-e2e-final.log`。这些是测试环境与定位器问题，不把失败尝试隐去，也不算业务功能失败。

本次验证的是规则检索、人工核验后的历史处置记录和确定性草案；没有调用付费模型，也没有真人客服可用性评分或真实退款/消息/订单副作用。截图与测试使用合成工单。文档上节的“待验收”仅记录 9 月 29 日当时状态，以上真实运行结果将其关闭。
