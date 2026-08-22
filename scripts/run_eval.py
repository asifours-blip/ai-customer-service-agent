"""评测 CLI。

用法：
  python scripts/run_eval.py                 # 离线全量（Fake 链路，零 API 费用）
  python scripts/run_eval.py --live [--force]# 真实评测（需 DEEPSEEK_API_KEY + NO_PAID_API=false）
  python scripts/run_eval.py --export-blind  # 导出 24 条盲标集（强制暂停点：等用户标注）
  python scripts/run_eval.py --calibrate CALIBRATION_DIR  # κ 校准（需标注+judge 结果）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval.calibration import build_calibration_set, calibrate, export_blind  # noqa: E402
from eval.cost import BudgetExceeded, estimate_cost, load_pricing, preflight_guard, reconcile_actual  # noqa: E402
from eval.judge import LLMJudge  # noqa: E402
from eval.loader import load_dataset  # noqa: E402
from eval.metrics import compute_all  # noqa: E402
from eval.report import new_payload, write_reports  # noqa: E402
from eval.runner import reset_environment, run_all  # noqa: E402

CALIB_DIR = ROOT / "eval" / "calibration"


def build_agent(live: bool):
    from app.agent.service import AgentService
    from app.config import get_settings
    from app.llm.client import FakeLLMClient
    from app.rag.answerer import RagService
    from app.rag.embedding import get_embedding_client
    from app.tools import build_registry

    settings = get_settings()
    if live:
        from app.llm.deepseek import DeepseekClient

        llm = DeepseekClient()
        embedder = get_embedding_client(settings.embedding_backend, settings.bge_model_name)
        threshold = (
            settings.retrieval_score_threshold_bge
            if settings.embedding_backend == "bge"
            else settings.retrieval_score_threshold_fake
        )
    else:
        llm = FakeLLMClient()
        # 离线评测：LLM 走 Fake（零付费），Embedding 尊重 EMBEDDING_BACKEND（bge 本地免费，D-002）
        embedder = get_embedding_client(settings.embedding_backend, settings.bge_model_name)
        threshold = (
            settings.retrieval_score_threshold_bge
            if settings.embedding_backend == "bge"
            else settings.retrieval_score_threshold_fake
        )
    return AgentService(llm, RagService(embedder, llm, score_threshold=threshold), build_registry())


def run(mode: str, force: bool) -> int:
    cases = load_dataset()
    chat_calls = sum(len(c.get("turns") or [1]) for c in cases)
    pricing = load_pricing()

    payload = new_payload(mode, {})
    if mode == "live":
        est = estimate_cost(
            pricing,
            chat_calls=chat_calls,
            agent_model="deepseek-v4-flash",
            judge_model="deepseek-v4-pro",
        )
        try:
            preflight_guard(
                est,
                soft_limit=1.00,
                hard_limit=2.00,
                force=force,
            )
        except BudgetExceeded as exc:
            print(f"Cost Guard 拒绝执行：{exc}")
            return 2
        payload["cost"] = est
        payload["cost_note"] = "preflight 通过"
        print(f"preflight 通过：预估 {est['estimated_cost_usd']} USD（{chat_calls} 次 chat + 24 次 judge）")

    from app.services.database import SessionLocal

    agent = build_agent(live=(mode == "live"))
    from app.config import get_settings as _gs
    from app.rag.embedding import get_embedding_client as _gec

    db = SessionLocal()
    try:
        print("重置评测环境（truncate → seed → ingest）...")
        reset_environment(db, _gec(_gs().embedding_backend, _gs().bge_model_name))
        print(f"运行 {len(cases)} 个 case（mode={mode}）...")
        results = run_all(agent, db, cases)
    finally:
        db.close()

    metrics = compute_all(results)
    payload["metrics"] = metrics
    payload["environment"] = {
        "llm": "deepseek-v4-flash" if mode == "live" else "fake-llm（确定性）",
        "embedding": _gs().embedding_backend,
        "note": (
            "真实 API"
            if mode == "live"
            else "离线模式：LLM 为确定性 Fake、Embedding 为本地模型（零 API 费用）"
        ),
    }

    if mode == "live":
        prompt_tokens = sum(t.get("prompt_tokens", 0) for r in results for t in r["turns"])
        completion_tokens = sum(t.get("completion_tokens", 0) for r in results for t in r["turns"])
        payload["cost"] = reconcile_actual(
            pricing,
            agent_model="deepseek-v4-flash",
            judge_model="deepseek-v4-pro",
            chat_prompt_tokens=prompt_tokens,
            chat_completion_tokens=completion_tokens,
            estimate=payload["cost"],
        )
        # Judge（分层校准集）
        picked = build_calibration_set(results)
        judge = LLMJudge(agent.llm)
        judge_items = []
        for r in picked:
            s = judge.score(r["final"]["input"], r["final"]["answer"], reference="(见知识库政策)")
            judge_items.append({"case_id": r["case_id"], **s})
        (CALIB_DIR / "judge_scores.json").write_text(
            json.dumps({"items": judge_items}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        payload["judge_summary"] = {
            "n": len(judge_items),
            "note": "Judge 分数在校准完成且 κ≥0.70 前不作为发布指标",
        }
    else:
        payload["cost_note"] = "离线模式：FakeLLM/FakeEmbedding，零 API 费用"

    payload["results_preview"] = [
        {
            "case_id": r["case_id"],
            "expected": r["expected_outcome"],
            "actual": r["actual_outcome"],
            "signal_ok": r["signal_ok"],
        }
        for r in results
    ]
    paths = write_reports(payload)
    print(f"报告：{paths['json']} , {paths['html']}")
    print(json.dumps({k: v for k, v in metrics.items() if k != "outcome_confusion"}, ensure_ascii=False, indent=2))
    return 0


def export_blind_cmd() -> int:
    from app.services.database import SessionLocal

    cases = load_dataset()
    agent = build_agent(live=False)
    db = SessionLocal()
    try:
        reset_environment(db)
        results = run_all(agent, db, cases)
    finally:
        db.close()
    picked = build_calibration_set(results)
    blind = export_blind(picked)
    CALIB_DIR.mkdir(parents=True, exist_ok=True)
    out = CALIB_DIR / "blind_export.json"
    out.write_text(json.dumps(blind, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"盲标集已导出：{out}（{len(blind['items'])} 条）")
    print("【强制暂停点】请人工独立标注 human_score(0/1/2) 后保存为 blind_labeled.json，再运行 --calibrate")
    return 0


def calibrate_cmd(calib_dir: Path) -> int:
    labeled = json.loads((calib_dir / "blind_labeled.json").read_text(encoding="utf-8"))
    judge = json.loads((calib_dir / "judge_scores.json").read_text(encoding="utf-8"))
    report = calibrate(labeled, judge)
    (calib_dir / "calibration_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["judge_publishable"] else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="真实评测（NO_PAID_API=false + DEEPSEEK_API_KEY）")
    parser.add_argument("--force", action="store_true", help="越过 preflight 软阈值（硬闸不可越）")
    parser.add_argument("--export-blind", action="store_true")
    parser.add_argument("--calibrate", type=Path, default=None)
    args = parser.parse_args()

    if args.live and os.environ.get("NO_PAID_API", "true").lower() == "true":
        print("live 需要 NO_PAID_API=false 与 DEEPSEEK_API_KEY 环境变量")
        return 2
    if args.export_blind:
        return export_blind_cmd()
    if args.calibrate:
        return calibrate_cmd(args.calibrate)
    return run(mode="live" if args.live else "offline", force=args.force)


if __name__ == "__main__":
    sys.exit(main())
