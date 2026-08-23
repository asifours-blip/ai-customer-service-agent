# Security Policy

## Reporting

请通过 GitHub Issue 或仓库所有者联系方式报告安全问题，不要在公开 issue 中粘贴敏感信息。

## Notes

- 不要提交真实 API Key：本仓库仅从环境变量读取 `DEEPSEEK_API_KEY`（见 `.env.example`）
- 所有演示数据（用户/订单/工单）均为模拟数据，不含真实个人信息
- CI 永远离线运行（`NO_PAID_API=true`）；真实评测 workflow 仅手动触发并受成本护栏约束
