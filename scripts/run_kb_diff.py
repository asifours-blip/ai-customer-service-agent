"""知识库版本对比 CLI：同一组评测样本分别在版本 A / B 上跑，输出逐题差异报告。

复用 scripts/run_eval.py 的装配方式（build_agent / select_kb_version / reset_environment / run_all），
不改评测执行器本身，只是跑两遍、对比结果。

用法：
  python scripts/run_kb_diff.py --a 1 --b 2                  # 按版本号对比
  python scripts/run_kb_diff.py --a active --b 3             # 当前生效版本 vs 版本 3
  python scripts/run_kb_diff.py --a 1 --b 2 --category rag   # 只跑某一类样本（更快）

必须设置 EVAL_DATABASE_URL（独立评测库，库名以 _eval/_test 结尾，且不能与 DATABASE_URL 同库）——
与 scripts/run_eval.py 共用同一套安全闸（eval/safety.py）：先校验、把全局会话工厂改绑到评测库，
两个版本各跑一遍都会先清空业务表（知识库表不动，不影响被对比的两个版本本身）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval.kb_diff import diff_results  # noqa: E402
from eval.loader import load_dataset  # noqa: E402
from eval.runner import reset_environment, run_all  # noqa: E402
from scripts.run_eval import bind_eval_database, build_agent, select_kb_version  # noqa: E402


def run_one(kb_spec: str, cases: list[dict]) -> tuple[list[dict], object]:
    from app.services.database import SessionLocal

    version = select_kb_version(kb_spec)
    if version is None:
        raise SystemExit(2)
    agent = build_agent(live=False, kb_version_id=version.id)
    db = SessionLocal()
    try:
        reset_environment(db)
        results = run_all(agent, db, cases)
    finally:
        db.close()
    return results, version


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--a", required=True, help="版本 A：dir | active | 版本号")
    parser.add_argument("--b", required=True, help="版本 B：dir | active | 版本号")
    parser.add_argument("--category", default=None, help="只跑指定类别（默认全部 110 条）")
    args = parser.parse_args()

    if not bind_eval_database():
        return 2  # 会重置业务表：先过评测库守卫，任何数据库操作之前

    cases = load_dataset()
    if args.category:
        cases = [c for c in cases if c["category"] == args.category]
        if not cases:
            print(f"类别 {args.category!r} 没有样本")
            return 2

    print(f"运行 {len(cases)} 个 case @ 版本 A（{args.a}）...")
    results_a, version_a = run_one(args.a, cases)
    print(f"运行 {len(cases)} 个 case @ 版本 B（{args.b}）...")
    results_b, version_b = run_one(args.b, cases)

    diff = diff_results(results_a, results_b)
    report = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "version_a": {"id": version_a.id, "status": version_a.status, "spec": args.a},
        "version_b": {"id": version_b.id, "status": version_b.status, "spec": args.b},
        "sample_count": len(cases),
        **diff,
    }

    out_dir = ROOT / "eval" / "reports" / "generated"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"kb_diff_v{version_a.id}_v{version_b.id}_{stamp}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"差异：{diff['changed_count']}/{diff['total']} 题不同（报告：{out_path}）")
    for c in diff["cases"]:
        if c["changed"]:
            print(f"  - {c['case_id']} [{c['category']}] 变化字段：{c['changed_fields']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
