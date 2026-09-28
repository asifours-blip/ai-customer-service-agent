"""评测 CLI。

用法：
  python scripts/run_eval.py                 # 离线全量（Fake 链路，零 API 费用）
  python scripts/run_eval.py --live [--force]# 真实评测（需 DEEPSEEK_API_KEY + NO_PAID_API=false）
                                             #   → 同时导出 judge_scores.json + blind_export_live.json（强制暂停点）
  python scripts/run_eval.py --export-blind  # 导出 24 条盲标集（离线答案，仅供熟悉评分标准）
  python scripts/run_eval.py --rejudge       # Judge v2 迭代：重评 live 冻结答案（需 key）
  python scripts/run_eval.py --calibrate CALIBRATION_DIR  # κ 校准
                                             #   （需 blind_labeled.json[来自live] + judge_scores.json）

评测库（必需）：EVAL_DATABASE_URL 指向独立的评测库（库名以 _eval 或 _test 结尾，且不能与 DATABASE_URL 同库），
  评测会清空其中的业务表；未设置或不合规时拒绝运行、不删除任何数据（eval/safety.py）。
  首次使用前先迁移：DATABASE_URL=$EVAL_DATABASE_URL alembic upgrade head

知识库版本（D-021）：--kb-version dir（默认）| active | 版本号
  dir    ：复用与 knowledge_base/ 目录内容、向量后端都一致的已校验版本，没有则导入一个新版本（只导入不发布）
  active ：当前线上生效版本；版本号：指定版本（READY / ACTIVE / RETIRED）
评测前只重置业务数据（用户/订单/工单/会话/Trace），知识库表不动，线上生效版本不受影响。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval.calibration import build_calibration_set, calibrate, export_blind  # noqa: E402
from eval.converted import load_sealed_version  # noqa: E402
from eval.cost import BudgetExceeded, estimate_cost, load_pricing, preflight_guard, reconcile_actual  # noqa: E402
from eval.judge import LLMJudge  # noqa: E402
from eval.loader import load_dataset  # noqa: E402
from eval.metrics import compute_all  # noqa: E402
from eval.report import new_payload, write_reports  # noqa: E402
from eval.runner import reset_environment, run_all  # noqa: E402
from eval.safety import EvalDatabaseRefused, resolve_eval_database  # noqa: E402

CALIB_DIR = ROOT / "eval" / "calibration"


def bind_eval_database() -> bool:
    """校验 EVAL_DATABASE_URL，并把全局会话工厂改绑到评测库（seed、检索、工具调用都走它）。"""
    try:
        eval_url = resolve_eval_database(os.environ)
    except EvalDatabaseRefused as exc:
        print(f"拒绝运行评测：{exc}")
        return False
    from app.services import database

    database.SessionLocal.configure(bind=database._engine_for(eval_url))
    print(f"评测库：{database.SessionLocal.kw['bind'].url.render_as_string(hide_password=True)}")
    return True


def eval_retrieval():
    """评测检索配置：Embedding 尊重 EMBEDDING_BACKEND（离线 bge 本地免费，D-002/D-015），阈值随后端。"""
    from app.config import get_settings
    from app.rag.embedding import get_embedding_client

    settings = get_settings()
    embedder = get_embedding_client(settings.embedding_backend, settings.bge_model_name)
    threshold = (
        settings.retrieval_score_threshold_bge
        if settings.embedding_backend == "bge"
        else settings.retrieval_score_threshold_fake
    )
    return embedder, threshold


def select_kb_version(spec: str):
    """确定被评测的知识库版本；不合法（不存在/状态不对/后端不一致）时返回 None 并打印原因。"""
    from eval.runner import resolve_kb_version

    embedder, threshold = eval_retrieval()
    try:
        version = resolve_kb_version(spec, embedder, threshold)
    except (ValueError, RuntimeError) as exc:
        print(f"知识库版本不可评测：{exc}")
        return None
    print(f"评测知识库版本 v{version.id}（{version.status}，source_hash={version.source_hash[:12]}）")
    return version


def build_agent(live: bool, kb_version_id: int):
    from app.agent.service import AgentService
    from app.llm.client import FakeLLMClient
    from app.rag.answerer import RagService
    from app.tools import build_registry

    if live:
        from app.llm.deepseek import DeepseekClient

        llm = DeepseekClient()
    else:
        llm = FakeLLMClient()  # 离线评测：LLM 走 Fake（零付费）
    embedder, threshold = eval_retrieval()
    # 检索固定在被评测版本上，不跟随线上 ACTIVE 切换
    rag = RagService(embedder, llm, score_threshold=threshold, version_id=kb_version_id)
    return AgentService(llm, rag, build_registry())


def run(mode: str, force: bool, kb_spec: str, converted_version: str | None = None) -> int:
    if converted_version:
        cases, manifest_sha256 = load_sealed_version(converted_version)
    else:
        cases, manifest_sha256 = load_dataset(), None
    chat_calls = sum(len(c.get("turns") or [1]) for c in cases)
    pricing = load_pricing()

    payload = new_payload(mode, {})
    payload["dataset"] = ({"kind": "converted", "version": converted_version, "manifest_sha256": manifest_sha256}
                          if converted_version else {"kind": "fixed", "case_count": 110})
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

    version = select_kb_version(kb_spec)
    if version is None:
        return 2
    agent = build_agent(live=(mode == "live"), kb_version_id=version.id)
    from app.config import get_settings as _gs

    db = SessionLocal()
    try:
        print("重置评测业务数据（truncate 业务表 → seed；知识库表不动）...")
        reset_environment(db)
        print(f"运行 {len(cases)} 个 case（mode={mode}）...")
        results = run_all(agent, db, cases)
    finally:
        db.close()

    metrics = compute_all(results)
    payload["metrics"] = metrics
    # 完整逐 case 结果入报告（可复现红线：指标可由报告独立重算，事后修正无需重跑）
    payload["results"] = results
    payload["environment"] = {
        "llm": "deepseek-v4-flash" if mode == "live" else "fake-llm（确定性）",
        "embedding": _gs().embedding_backend,
        "kb_version": {"id": version.id, "status": version.status, "source_hash": version.source_hash},
        "note": (
            "真实 API"
            if mode == "live"
            else "离线模式：LLM 为确定性 Fake、Embedding 为本地模型（零 API 费用）"
        ),
    }

    if mode == "live":
        # Judge（分层校准集）——必须先于成本对账：judge token 计入 actual
        # 参考要点按 case 类别构造（D-018），多轮 case 给完整对话链
        from app.rag.loader import load_corpus
        from eval.judge import build_reference, judge_question

        docs_by_name = {d.document_name: d for d in load_corpus(ROOT / "knowledge_base")}
        case_by_id = {c["case_id"]: c for c in cases}
        picked = build_calibration_set(results)
        judge = LLMJudge(agent.llm)
        judge_items = []
        judge_prompt = judge_completion = judge_unknown = 0
        for r in picked:
            case = case_by_id[r["case_id"]]
            s = judge.score(
                judge_question(case), r["final"]["answer"], reference=build_reference(case, docs_by_name)
            )
            judge_items.append({"case_id": r["case_id"], **s})
            judge_prompt += int(s.get("judge_prompt_tokens", 0))
            judge_completion += int(s.get("judge_completion_tokens", 0))
            judge_unknown += int(bool(s.get("judge_usage_unknown")))
        (CALIB_DIR / "judge_scores.json").write_text(
            json.dumps({"items": judge_items}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # 盲标集必须与 judge_scores 同一次运行（同一批答案），κ 才有效（D-017）
        blind_live = export_blind(picked, source="live")
        (CALIB_DIR / "blind_export_live.json").write_text(
            json.dumps(blind_live, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("盲标集（live 答案）已导出：eval/calibration/blind_export_live.json")
        print(
            "【强制暂停点】请人工标注该文件的 human_score(0/1/2)，"
            "另存为 blind_labeled.json（保留 source=live），再运行 --calibrate；"
            "判定方法见 eval/calibration/LABELING_GUIDE.md"
        )
        payload["judge_summary"] = {
            "n": len(judge_items),
            "note": "Judge 分数在校准完成且 κ≥0.70 前不作为发布指标",
        }
        # 成本对账：chat 实际 token 来自逐轮记录（AgentTrace 同源），judge 来自上面累计
        prompt_tokens = sum(int(t.get("prompt_tokens", 0) or 0) for r in results for t in r["turns"])
        completion_tokens = sum(
            int(t.get("completion_tokens", 0) or 0) for r in results for t in r["turns"]
        )
        unknown_calls = judge_unknown + sum(
            int(t.get("usage_unknown_calls", 0) or 0) for r in results for t in r["turns"]
        )
        payload["cost"] = reconcile_actual(
            pricing,
            agent_model="deepseek-v4-flash",
            judge_model="deepseek-v4-pro",
            chat_prompt_tokens=prompt_tokens,
            chat_completion_tokens=completion_tokens,
            judge_prompt_tokens=judge_prompt,
            judge_completion_tokens=judge_completion,
            estimate=payload["cost"],
            usage_unknown_calls=unknown_calls,
        )
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


def export_blind_cmd(kb_spec: str) -> int:
    from app.services.database import SessionLocal

    cases = load_dataset()
    version = select_kb_version(kb_spec)
    if version is None:
        return 2
    agent = build_agent(live=False, kb_version_id=version.id)
    db = SessionLocal()
    try:
        reset_environment(db)
        results = run_all(agent, db, cases)
    finally:
        db.close()
    picked = build_calibration_set(results)
    blind = export_blind(picked, source="offline")
    CALIB_DIR.mkdir(parents=True, exist_ok=True)
    out = CALIB_DIR / "blind_export.json"
    out.write_text(json.dumps(blind, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"盲标集已导出：{out}（{len(blind['items'])} 条，离线 FakeLLM 答案，仅供熟悉评分标准）")
    print("κ 校准用的盲标集在 --live 运行时导出为 blind_export_live.json（人工与 Judge 必须评同一批答案，D-017）")
    return 0


def rejudge_cmd() -> int:
    """Judge v2 迭代（D-018）：对 blind_export_live.json 的同一批冻结答案重新评分。

    不重跑 Agent（答案不变 → 人工标签仍有效），只重新调用 Judge。
    """
    from app.llm.deepseek import DeepseekClient
    from app.rag.loader import load_corpus
    from eval.judge import build_reference, judge_question

    blind = json.loads((CALIB_DIR / "blind_export_live.json").read_text(encoding="utf-8"))
    case_by_id = {c["case_id"]: c for c in load_dataset()}
    docs_by_name = {d.document_name: d for d in load_corpus(ROOT / "knowledge_base")}
    judge = LLMJudge(DeepseekClient())
    items: list[dict[str, Any]] = []
    judge_prompt = judge_completion = 0
    for it in blind["items"]:
        case = case_by_id[it["case_id"]]
        s = judge.score(
            judge_question(case), it["answer"], reference=build_reference(case, docs_by_name)
        )
        items.append({"case_id": it["case_id"], **s})
        judge_prompt += int(s.get("judge_prompt_tokens", 0))
        judge_completion += int(s.get("judge_completion_tokens", 0))
    (CALIB_DIR / "judge_scores.json").write_text(
        json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    p = load_pricing().models["deepseek-v4-pro"]
    cost = (
        judge_prompt / 1e6 * p["input_cache_miss_per_million"]
        + judge_completion / 1e6 * p["output_per_million"]
    )
    print(f"rejudge 完成：{len(items)} 条（解析失败 {sum(1 for i in items if i['score'] == -1)} 条）")
    print(f"judge tokens：{judge_prompt}+{judge_completion}，实际 {cost:.4f} USD（逐笔记录于 judge_scores.json）")
    return 0


def calibrate_cmd(calib_dir: Path) -> int:
    labeled = json.loads((calib_dir / "blind_labeled.json").read_text(encoding="utf-8"))
    judge = json.loads((calib_dir / "judge_scores.json").read_text(encoding="utf-8"))
    try:
        report = calibrate(labeled, judge)
    except ValueError as exc:
        print(f"校准拒绝：{exc}")
        return 2
    (calib_dir / "calibration_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["judge_publishable"] else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="真实评测（NO_PAID_API=false + DEEPSEEK_API_KEY）")
    parser.add_argument("--force", action="store_true", help="越过 preflight 软阈值（硬闸不可越）")
    parser.add_argument("--converted-version", help="仅评测已封版的转换数据集，如 v1")
    parser.add_argument("--export-blind", action="store_true")
    parser.add_argument(
        "--rejudge", action="store_true", help="Judge v2 迭代：重评 live 冻结答案（需 key + NO_PAID_API=false）"
    )
    parser.add_argument("--calibrate", type=Path, default=None)
    parser.add_argument(
        "--kb-version", default="dir", help="评测的知识库版本：dir（默认，与 knowledge_base/ 一致）| active | 版本号"
    )
    args = parser.parse_args()

    needs_key = args.live or args.rejudge
    if needs_key and os.environ.get("NO_PAID_API", "true").lower() == "true":
        print("live/rejudge 需要 NO_PAID_API=false 与 DEEPSEEK_API_KEY 环境变量")
        return 2
    if (args.export_blind or not (args.rejudge or args.calibrate)) and not bind_eval_database():
        return 2  # 会重置业务表的命令：先过评测库守卫，任何数据库操作之前
    if args.export_blind:
        return export_blind_cmd(args.kb_version)
    if args.rejudge:
        return rejudge_cmd()
    if args.calibrate:
        return calibrate_cmd(args.calibrate)
    return run(mode="live" if args.live else "offline", force=args.force, kb_spec=args.kb_version,
               converted_version=args.converted_version)


if __name__ == "__main__":
    sys.exit(main())
