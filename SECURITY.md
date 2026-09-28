# Security Policy

## Reporting

请不要通过公开 GitHub Issue 报告安全问题。请使用本仓库的 [private vulnerability reporting form](https://github.com/asifours-blip/ai-customer-service-agent/security/advisories/new)。

## Notes

- 不要提交真实 API Key：本仓库仅从环境变量读取 `DEEPSEEK_API_KEY`（见 `.env.example`）
- 所有演示数据（用户/订单/工单）均为模拟数据，不含真实个人信息
- CI 永远离线运行（`NO_PAID_API=true`）；真实评测 workflow 仅手动触发并受成本护栏约束

## 密钥管理约定

- 所有密钥（`DEEPSEEK_API_KEY`、`JWT_SECRET` 等）只来自环境变量或 `.env` / secret 文件，代码里没有、也不允许硬编码任何真实值；`.env.example` 只列可选项与默认值，敏感项一律留空。
- 不打进镜像：`Dockerfile` 不 `COPY .env` 或任何含密钥的文件；镜像里只有代码，运行时才通过 `docker-compose.yml` 的 `environment:` 或宿主机 `.env` 注入。CI 的 live-eval workflow 用 `${{ secrets.DEEPSEEK_API_KEY }}`，`JWT_SECRET` 每次运行随机生成，两者都不落盘到仓库或工作流文件里。
- 不写进日志：`DEEPSEEK_API_KEY` 在配置对象里是 `pydantic.SecretStr`，被打印 / `repr` / 异常信息带出时只显示 `**********`（见 `app/config.py`）；`JWT_SECRET` 不出现在任何日志或 Trace 记录中。
- 轮换：泄露或怀疑泄露时直接在密钥来源处（DeepSeek 控制台 / GitHub Actions Secrets）作废旧值、签发新值，再更新 `.env` 或 CI secret；本仓库不保存历史密钥。
