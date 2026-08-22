"""根级测试夹具：在任何 app 导入之前固定测试数据库 URL。

单元测试不触库；集成测试统一使用 agent_cs_test（真实 PostgreSQL）。
"""

import os

TEST_DB_NAME = os.environ.get("AGENT_CS_TEST_DB", "agent_cs_test")
os.environ.setdefault("DATABASE_URL", f"postgresql+psycopg://app:app@localhost:5432/{TEST_DB_NAME}")
os.environ.setdefault("NO_PAID_API", "true")
