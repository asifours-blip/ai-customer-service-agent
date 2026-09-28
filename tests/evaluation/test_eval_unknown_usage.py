"""评测记账与 usage 未知（阶段 4，先写红测试）：usage 缺失 / 结果未知的调用按上限计入，并单独计数。"""

from __future__ import annotations

from typing import Any

from app.llm.client import LLMResponse, LLMUsage
from app.llm.errors import LLMReadTimeoutError, LLMServerError


class _RaisingLLM:
    model_name = "stub"

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def complete(self, system: str, user: str, *, max_tokens: int = 1500, json_mode: bool = False) -> LLMResponse:
        raise self.exc


def test_judge_read_timeout_counts_ceiling() -> None:
    from eval.judge import LLMJudge

    ceiling = LLMUsage.ceiling("s", "u", 1200)
    s = LLMJudge(_RaisingLLM(LLMReadTimeoutError("读超时", usage=ceiling))).score("q", "a", "ref")  # type: ignore[arg-type]
    assert s["score"] == -1 and "READ_TIMEOUT" in s["reason"]
    assert s["judge_prompt_tokens"] == ceiling.prompt_tokens
    assert s["judge_completion_tokens"] == 1200
    assert s["judge_usage_unknown"] is True


def test_judge_unbilled_failure_counts_zero() -> None:
    from eval.judge import LLMJudge

    s = LLMJudge(_RaisingLLM(LLMServerError("5xx", status_code=500))).score("q", "a", "ref")  # type: ignore[arg-type]
    assert s["score"] == -1
    assert s["judge_prompt_tokens"] == 0 and s["judge_usage_unknown"] is False


def test_judge_success_without_usage_flags_unknown() -> None:
    from eval.judge import LLMJudge

    class _NoUsage:
        model_name = "stub"

        def complete(self, system: str, user: str, *, max_tokens: int = 1500, json_mode: bool = False) -> LLMResponse:
            return LLMResponse(content='{"score": 2, "reason": "ok"}', model="stub",
                               usage=LLMUsage.ceiling(system, user, max_tokens))

    s = LLMJudge(_NoUsage()).score("q", "a", "ref")  # type: ignore[arg-type]
    assert s["score"] == 2 and s["judge_usage_unknown"] is True
    assert s["judge_completion_tokens"] == 1200


def test_run_case_carries_unknown_usage_count() -> None:
    from eval.runner import run_case

    class _Agent:
        def handle(self, db: Any, user_id: str, session_id: str, text: str) -> dict[str, Any]:
            return {"answer": "答", "route": "RAG", "trace_id": "tr1", "latency_ms": 5,
                    "prompt_tokens": 900, "completion_tokens": 1500, "usage_unknown_calls": 1,
                    "answer_mode": "MODEL"}

    case = {"case_id": "t1", "category": "rag", "user_id": "U001", "input": "问", "expected_outcome": "SUCCESS"}
    out = run_case(_Agent(), None, case)  # type: ignore[arg-type]
    assert out["turns"][0]["usage_unknown_calls"] == 1
    assert out["turns"][0]["answer_mode"] == "MODEL"


def test_reconcile_reports_unknown_usage_calls() -> None:
    from eval.cost import load_pricing, reconcile_actual

    out = reconcile_actual(
        load_pricing(), agent_model="deepseek-v4-flash", judge_model="deepseek-v4-pro",
        chat_prompt_tokens=1000, chat_completion_tokens=1500, usage_unknown_calls=2,
    )
    assert out["usage_unknown_calls"] == 2
    assert "上限" in out["usage_unknown_note"]
