"""评测模块单测：outcome 派生 / κ 数学 / 成本闸门。纯单测。"""

from __future__ import annotations

import pytest

from eval.calibration import calibrate, cohen_kappa, export_blind, weighted_kappa
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


# --- live 对账修复回归（真实踩坑：actual token 曾恒为 0 / 注入拦截曾被算成错误）---


def _result_row(turns: list[dict]) -> dict:
    return {"case_id": "x", "category": "injection", "turns": turns, "final": turns[-1]}


def test_system_metrics_excludes_guardrail_blocks_from_error_rate() -> None:
    """INJECTION_FLAGGED 是注入用例的期望结果，不计 error_rate，单列 guardrail_blocked。"""
    from eval.metrics import system_metrics

    rows = [
        _result_row([_turn("ok", latency_ms=1)]),
        _result_row([_turn("已拦截", error_type="INJECTION_FLAGGED:role_hijack", latency_ms=1)]),
        _result_row([_turn("boom", error_type="LLM_TIMEOUT", latency_ms=1)]),
    ]
    m = system_metrics(rows)
    assert m["error_rate"] == round(1 / 3, 4)
    assert m["guardrail_blocked"] == 1


def test_judge_records_token_usage() -> None:
    """judge token 必须逐次透传，成本对账才拿得到真实 judge actual。"""
    from app.llm.client import LLMResponse, LLMUsage
    from eval.judge import LLMJudge

    class _StubLLM:
        model_name = "stub"

        def complete(
            self, system: str, user: str, *, max_tokens: int = 1500, json_mode: bool = False
        ) -> LLMResponse:
            _ = system, user, max_tokens, json_mode
            return LLMResponse(
                content='{"score": 2, "reason": "ok"}',
                model="stub",
                usage=LLMUsage(prompt_tokens=11, completion_tokens=7, total_tokens=18),
            )

    s = LLMJudge(_StubLLM()).score("q", "a", "ref")  # type: ignore[arg-type]
    assert s["score"] == 2
    assert s["judge_prompt_tokens"] == 11
    assert s["judge_completion_tokens"] == 7


# --- Judge v2 参考要点构造（D-018：占位符参考导致 κ 校准失败）---


def test_build_reference_category_rules() -> None:
    """注入/越权/拒答场景必须给出"正确行为=拒绝"的领域规则，而不是文档摘要。"""
    from eval.judge import build_reference

    assert "注入" in build_reference({"category": "injection"}, {})
    assert "无权" in build_reference({"category": "idor"}, {})
    assert "无法回答" in build_reference({"category": "abstention"}, {})


def test_build_reference_uses_expected_document() -> None:
    from app.rag.loader import KnowledgeDocument
    from eval.judge import build_reference

    doc = KnowledgeDocument(
        document_id="d1", document_name="保修政策", category="policies",
        policy_version="2026.08", sections=[["整机", "12 个月"], ["电池", "6 个月"]],  # type: ignore[list-item]
    )
    ref = build_reference({"category": "rag", "expected_document": "保修政策"}, {"保修政策": doc})
    assert "《保修政策》" in ref and "12 个月" in ref


def test_build_reference_falls_back_to_seed_facts() -> None:
    from eval.judge import SEED_FACTS, build_reference

    assert build_reference({"category": "order"}, {}) == SEED_FACTS


def test_judge_question_expands_multi_turn() -> None:
    from eval.judge import judge_question

    q = judge_question({"category": "multi_turn", "turns": [{"input": "查 A10001"}, {"input": "它到哪了"}]})
    assert "查 A10001" in q and "它到哪了" in q
    assert judge_question({"category": "rag", "input": "单轮"}) == "单轮"


def test_run_case_records_per_turn_tokens() -> None:
    """逐轮 prompt/completion tokens 必须进 turns（run_eval 汇总 actual 靠它，曾恒为 0）。"""
    from typing import Any

    from eval.runner import run_case

    class _Agent:
        def handle(self, db: Any, user_id: str, session_id: str, text: str) -> dict:
            return {
                "answer": "答", "route": "RAG", "trace_id": "tr1", "latency_ms": 5,
                "prompt_tokens": 12, "completion_tokens": 6,
            }

    case = {
        "case_id": "t1", "category": "rag", "user_id": "U001",
        "input": "问", "expected_outcome": "SUCCESS",
    }
    out = run_case(_Agent(), None, case)  # type: ignore[arg-type]
    assert out["turns"][0]["prompt_tokens"] == 12
    assert out["turns"][0]["completion_tokens"] == 6


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


# --- 盲标导出 / κ 校准（D-017：人工与 Judge 必须评同一批 live 答案）---


def _picked() -> list[dict]:
    return [
        {"case_id": "rag_001", "category": "rag", "final": {"input": "保修多久？", "answer": "12 个月。"}},
        {"case_id": "mt_001", "category": "multi_turn", "final": {"input": "到哪了？", "answer": "已签收。"}},
    ]


def test_export_blind_marks_source_and_hides_judge_info() -> None:
    blind = export_blind(_picked(), source="live")
    assert blind["source"] == "live"
    assert len(blind["items"]) == 2
    # 盲标文件不得携带 judge/期望信息
    for it in blind["items"]:
        assert set(it) == {"item_id", "case_id", "question", "answer"}
    offline = export_blind(_picked(), source="offline")
    assert offline["source"] == "offline"
    assert "blind_export_live" in offline["instructions"]  # 离线导出自带警告


def test_calibrate_rejects_non_live_labels() -> None:
    labeled = {"source": "offline", "items": [{"case_id": "rag_001", "human_score": 2}]}
    judge = {"items": [{"case_id": "rag_001", "score": 2, "reason": "ok"}]}
    with pytest.raises(ValueError, match="live"):
        calibrate(labeled, judge)


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
