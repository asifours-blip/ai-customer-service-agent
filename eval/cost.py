"""Cost Guard：preflight 估价 + 预算闸门 + 真实成本对账（补丁 §9/§10）。

- MAX_SINGLE_LIVE_EVAL_COST_USD=1.00（preflight，--force 可越）
- HARD_EVAL_COST_LIMIT_USD=2.00（硬闸，任何情况不可越）
- estimated 与 actual 并存，差值保留不覆盖
- usage 未知的调用（接口没返回 usage、读超时、响应中断）不按 0 计：客户端给出上限
  （输入 ≤ UTF-8 字节数 + 模板开销，输出 ≤ max_tokens，见 app.llm.client.LLMUsage.ceiling），
  传进来的 token 已含这些上限值；usage_unknown_calls 单独报告条数，actual 因此是偏保守的上界
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PRICING_PATH = Path(__file__).resolve().parents[1] / "config" / "model_pricing.yaml"

# 估算假设（保守上界，live 前用于 preflight；实际以 API usage 为准）
ASSUMED_PROMPT_TOKENS = 900
ASSUMED_COMPLETION_TOKENS = 400
ASSUMED_JUDGE_PROMPT = 600
ASSUMED_JUDGE_COMPLETION = 150
CALIBRATION_N = 24


class BudgetExceeded(Exception):
    def __init__(self, message: str, *, hard: bool = False) -> None:
        super().__init__(message)
        self.hard = hard


@dataclass(frozen=True)
class Pricing:
    provider: str
    checked_at: str
    models: dict[str, dict[str, float]]
    snapshot_sha256: str


def load_pricing(path: Path | None = None) -> Pricing:
    p = path or PRICING_PATH
    raw = p.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    return Pricing(
        provider=str(data["provider"]),
        checked_at=str(data["checked_at"]),
        models={k: dict(v) for k, v in data["models"].items()},
        snapshot_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
    )


def estimate_cost(
    pricing: Pricing,
    *,
    chat_calls: int,
    judge_calls: int = CALIBRATION_N,
    agent_model: str,
    judge_model: str,
) -> dict[str, Any]:
    agent = pricing.models[agent_model]
    judge = pricing.models[judge_model]
    agent_cost = (
        chat_calls * ASSUMED_PROMPT_TOKENS / 1e6 * agent["input_cache_miss_per_million"]
        + chat_calls * ASSUMED_COMPLETION_TOKENS / 1e6 * agent["output_per_million"]
    )
    judge_cost = (
        judge_calls * ASSUMED_JUDGE_PROMPT / 1e6 * judge["input_cache_miss_per_million"]
        + judge_calls * ASSUMED_JUDGE_COMPLETION / 1e6 * judge["output_per_million"]
    )
    return {
        "chat_calls": chat_calls,
        "judge_calls": judge_calls,
        "agent_model": agent_model,
        "judge_model": judge_model,
        "estimated_cost_usd": round(agent_cost + judge_cost, 4),
        "assumptions": {
            "prompt_tokens_per_call": ASSUMED_PROMPT_TOKENS,
            "completion_tokens_per_call": ASSUMED_COMPLETION_TOKENS,
            "judge_prompt_tokens": ASSUMED_JUDGE_PROMPT,
            "judge_completion_tokens": ASSUMED_JUDGE_COMPLETION,
        },
    }


def preflight_guard(
    estimate: dict[str, Any],
    *,
    soft_limit: float,
    hard_limit: float,
    force: bool = False,
) -> None:
    cost = float(estimate["estimated_cost_usd"])
    if cost > hard_limit:
        raise BudgetExceeded(
            f"预估 {cost:.4f} USD 超过硬闸 {hard_limit:.2f} USD：拒绝执行（--force 也不可越）", hard=True
        )
    if cost > soft_limit and not force:
        msg = (
            f"预估 {cost:.4f} USD 超过预检阈值 {soft_limit:.2f} USD：默认拒绝"
            f"（--force 可越过，硬闸 {hard_limit:.2f} 仍生效）"
        )
        raise BudgetExceeded(msg)


def reconcile_actual(
    pricing: Pricing,
    *,
    agent_model: str,
    judge_model: str,
    chat_prompt_tokens: int,
    chat_completion_tokens: int,
    judge_prompt_tokens: int = 0,
    judge_completion_tokens: int = 0,
    estimate: dict[str, Any] | None = None,
    usage_unknown_calls: int = 0,
) -> dict[str, Any]:
    agent = pricing.models[agent_model]
    judge = pricing.models[judge_model]
    agent_cost = (
        chat_prompt_tokens / 1e6 * agent["input_cache_miss_per_million"]
        + chat_completion_tokens / 1e6 * agent["output_per_million"]
    )
    judge_cost = (
        judge_prompt_tokens / 1e6 * judge["input_cache_miss_per_million"]
        + judge_completion_tokens / 1e6 * judge["output_per_million"]
    )
    actual = round(agent_cost + judge_cost, 4)
    out: dict[str, Any] = {
        "actual_token_usage": {
            "chat_prompt": chat_prompt_tokens,
            "chat_completion": chat_completion_tokens,
            "judge_prompt": judge_prompt_tokens,
            "judge_completion": judge_completion_tokens,
        },
        "calculated_actual_cost_usd": actual,
        "usage_unknown_calls": usage_unknown_calls,
        "usage_unknown_note": (
            f"{usage_unknown_calls} 次调用 usage 未知（未返回 / 读超时 / 响应中断），已按上限计入"
            if usage_unknown_calls
            else "全部调用都有接口返回的 usage；按上限计入的调用为 0"
        ),
        "pricing_snapshot": {
            "provider": pricing.provider,
            "checked_at": pricing.checked_at,
            "sha256_16": pricing.snapshot_sha256,
        },
    }
    if estimate is not None:
        est = float(estimate["estimated_cost_usd"])
        out["estimated_cost_usd"] = est
        out["estimate_vs_actual_delta_usd"] = round(actual - est, 4)  # 差值保留不覆盖
    return out
