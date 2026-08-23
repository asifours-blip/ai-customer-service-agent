"""tests_stress/ 独立于正式 tests/（pyproject testpaths=["tests"]），默认不收集、CI 不跑。
复用 tests/conftest.py 的环境准备与 db/client fixtures（真实 Docker PostgreSQL）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.conftest import *  # noqa: F401,F403,E402  （db_engine/db/client/login/auth_headers 及 env 设置）
