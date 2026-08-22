"""评测模块单测：outcome 派生 / κ 数学 / 成本闸门。纯单测。"""

from __future__ import annotations

import pytest

from eval.calibration import cohen_kappa, weighted_kappa
from eval.cost import BudgetExceeded, estimate_cost, load_pricing, preflight_guard, reconcile_actual
from eval.runner import derive_actual_outcome

# --- outcome 派生 ---


def _turn(answer: str = "", route: str = "RAG", **kw) -> dict:
    return {"answer": answer, "route": route, "abstained": False, "failed_tools": [], "error_type": None, **kw}


def test_outcome_success() -> None:
    assert derive_actual_outcome(_turn("耳机保修 12 个月")) == "SUCCESS"


def test_outcome_refused_by_abstain() -> None:
    assert derive_actual_outcome(_turn("没有足够信息", abstained=True)) == "REFUSED"


def test_outcome_refused_by_human_route() -> None:
    assert derive_actual_outcome(_turn("转接人工", route="HUMAN")) == "REFUSED"


def test_outcome_rag_echo_not_polluted() -> None:
    """知识库文本回显含'转接人工/不存在'字样不得误判（真实踩坑回归）。"""
    t = _turn("知识库内容：…智能客服可以转接人工…签收不存在情况…", route="RAG")
    assert derive_actual_outcome(t) == "SUCCESS"


def test_outcome_blocked_by_guardrail() -> None:
    assert derive_actual_outcome(_turn("x", error_type="INJECTION_FLAGGED:role_hijack")) == "BLOCKED"


def test_outcome_tool_failures() -> None:
    assert derive_actual_outcome(_turn("用户无权访问", route="ORDER_TOOL", failed_tools=["query_order"])) == "BLOCKED"
    assert derive_actual_outcome(_turn("工单已存在", failed_tools=["create_ticket"])) == "DUPLICATE"
    assert derive_actual_outcome(_turn("订单不存在", failed_tools=["query_order"])) == "NOT_FOUND"


# --- Cohen's κ ---


def test_kappa_perfect_agreement() -> None:
    assert cohen_kappa([0, 1, 2, 1], [0, 1, 2, 1]) == 1.0


def test_kappa_known_value() -> None:
    # 手算例：po=0.6, pe=0.5 → κ=0.2
    a = [0, 0, 1, 1, 2]
    b = [0, 1, 1, 2, 2]
    # po=0.2? 逐对：(0,0)✓ (0,1) (1,1)✓ (1,2) (2,2)✓ → po=0.6
    # pe: pa=[.4,.4,.2] pb=[.2,.4,.4] → .08+.16+.08=.32 → κ=(.6-.32)/.68≈.4118
    assert cohen_kappa(a, b) == pytest.approx(0.4118, abs=1e-3)


def test_weighted_kappa_bounds() -> None:
    a, b = [0, 1, 2], [0, 1, 2]
    assert weighted_kappa(a, b) == 1.0
    assert weighted_kappa([0, 1, 2], [2, 1, 0]) < 1.0


# --- Cost Guard ---


def test_preflight_pass_under_soft() -> None:
    pricing = load_pricing()
    est = estimate_cost(pricing, chat_calls=130, agent_model="deepseek-v4-flash", judge_model="deepseek-v4-pro")
    preflight_guard(est, soft_limit=1.00, hard_limit=2.00)  # 不抛


def test_preflight_soft_refused_then_forced() -> None:
    est = {"estimated_cost_usd": 1.5}
    with pytest.raises(BudgetExceeded, match="force"):
        preflight_guard(est, soft_limit=1.00, hard_limit=2.00)
    preflight_guard(est, soft_limit=1.00, hard_limit=2.00, force=True)  # 软闸可越


def test_preflight_hard_never_forced() -> None:
    est = {"estimated_cost_usd": 2.5}
    with pytest.raises(BudgetExceeded, match="硬闸"):
        preflight_guard(est, soft_limit=1.00, hard_limit=2.00, force=True)


def test_reconcile_keeps_delta() -> None:
    pricing = load_pricing()
    est = {"estimated_cost_usd": 0.9}
    out = reconcile_actual(
        pricing,
        agent_model="deepseek-v4-flash",
        judge_model="deepseek-v4-pro",
        chat_prompt_tokens=1_000_000,
        chat_completion_tokens=100_000,
        estimate=est,
    )
    # 实际按 cache-miss 单价：1e6*0.14/1e6 + 1e5*0.28/1e6 = 0.168
    assert out["calculated_actual_cost_usd"] == pytest.approx(0.168, abs=1e-4)
    assert "estimate_vs_actual_delta_usd" in out  # 差值保留不覆盖
    assert out["pricing_snapshot"]["sha256_16"]
