"""封存当前转换数据版本：python scripts/seal_converted.py --actor support_agent。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval.converted import current_version, seal_version  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="封版当前开放的转换评测数据")
    parser.add_argument("--actor", required=True, help="操作人账号，用于 manifest 审计")
    args = parser.parse_args()
    version = current_version()
    try:
        manifest = seal_version(version, args.actor)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"已封版 {version}：{manifest['case_count']} 条；下一版 {current_version()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
