"""评测库守卫：评测会 TRUNCATE 全部业务表，必须跑在独立的库上。

目标库只读自 EVAL_DATABASE_URL；以下情况一律拒绝（不连接、不删除任何数据）：
- 未设置 EVAL_DATABASE_URL；
- 与应用库 DATABASE_URL 是同一个库（比较规范化后的 host、port、dbname：主机不分大小写，
  localhost / 127.0.0.1 / ::1 视为同一主机，缺省端口按 5432，与驱动名和用户名无关）；
- 库名不以 _eval 或 _test 结尾。
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy.engine import URL, make_url

EVAL_DB_SUFFIXES = ("_eval", "_test")
_LOOPBACK = {"localhost", "127.0.0.1", "::1", "[::1]"}


class EvalDatabaseRefused(RuntimeError):
    """评测目标库不安全：拒绝运行。"""


def database_key(url: str | URL) -> tuple[str, int, str]:
    u = make_url(url) if isinstance(url, str) else url
    host = (u.host or "localhost").lower()
    return ("localhost" if host in _LOOPBACK else host, int(u.port or 5432), u.database or "")


def _label(key: tuple[str, int, str]) -> str:
    return f"{key[0]}:{key[1]}/{key[2]}"


def resolve_eval_database(env: Mapping[str, str]) -> str:
    """校验并返回评测库 URL；不安全则抛 EvalDatabaseRefused（说明原因）。"""
    eval_url = (env.get("EVAL_DATABASE_URL") or "").strip()
    if not eval_url:
        raise EvalDatabaseRefused(
            "未设置 EVAL_DATABASE_URL：评测会清空业务表，必须显式指定一个独立的评测库（库名以 _eval 或 _test 结尾）"
        )
    app_url = env.get("DATABASE_URL") or _default_app_url()
    eval_key, app_key = database_key(eval_url), database_key(app_url)
    if eval_key == app_key:
        raise EvalDatabaseRefused(
            f"EVAL_DATABASE_URL 与 DATABASE_URL 指向同一个库（{_label(eval_key)}）：评测不能在应用库上清空业务表"
        )
    if not eval_key[2].endswith(EVAL_DB_SUFFIXES):
        raise EvalDatabaseRefused(
            f"评测库名 {eval_key[2]!r} 不以 _eval 或 _test 结尾：为防误删，只允许在专用评测/测试库上运行"
        )
    return eval_url


def ensure_eval_target(bound_url: str | URL, env: Mapping[str, str]) -> None:
    """重置前的纵深防御：会话实际连接的库必须就是通过校验的 EVAL_DATABASE_URL。"""
    allowed = database_key(resolve_eval_database(env))
    actual = database_key(bound_url)
    if actual != allowed:
        raise EvalDatabaseRefused(
            f"当前会话连接的是 {_label(actual)}，不是 EVAL_DATABASE_URL（{_label(allowed)}）：拒绝重置"
        )


def _default_app_url() -> str:
    # 应用未通过环境变量设置 DATABASE_URL 时，按应用实际会用的配置（含 .env 与默认值）比较
    from app.config import Settings

    return Settings().database_url
