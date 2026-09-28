"""审核转换的评测用例：开放版本只追加，封版后按 manifest 校验。"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CONVERTED_DIR = Path(__file__).resolve().parent / "datasets" / "converted"
CURRENT_VERSION = "v1"


def _valid_version(version: str) -> int:
    if not re.fullmatch(r"v[1-9]\d*", version):
        raise ValueError(f"非法转换版本：{version!r}")
    return int(version[1:])


def version_path(version: str = CURRENT_VERSION) -> Path:
    _valid_version(version)
    return CONVERTED_DIR / f"{version}.jsonl"


def manifest_path(version: str) -> Path:
    _valid_version(version)
    return CONVERTED_DIR / f"{version}.manifest.json"


def current_version() -> str:
    """最后一个版本若已封存，则选择下一版。"""
    numbers = [_valid_version(p.stem) for p in CONVERTED_DIR.glob("v*.jsonl")] if CONVERTED_DIR.exists() else []
    latest = f"v{max(numbers, default=1)}"
    return f"v{_valid_version(latest) + 1}" if manifest_path(latest).exists() else latest


@contextmanager
def _version_lock() -> Iterator[None]:
    """跨进程互斥：编号、追加、封版不能交错。"""
    CONVERTED_DIR.mkdir(parents=True, exist_ok=True)
    with (CONVERTED_DIR / ".versions.lock").open("a+b") as stream:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)  # type: ignore[attr-defined]
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _verified_manifest(version: str, manifest_raw: bytes) -> bytes:
    try:
        manifest = json.loads(manifest_raw)
        raw = version_path(version).read_bytes()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"转换版本 {version} 的封版文件缺失或无法读取，拒绝加载") from exc
    if (not isinstance(manifest, dict) or manifest.get("version") != version
            or type(manifest.get("case_count")) is not int
            or not isinstance(manifest.get("line_sha256"), list)
            or not all(isinstance(item, str) for item in manifest["line_sha256"])
            or not isinstance(manifest.get("file_sha256"), str)
            or not isinstance(manifest.get("sealed_at"), str)
            or not isinstance(manifest.get("actor"), str) or not manifest["actor"].strip()):
        raise ValueError(f"转换版本 {version} 的 manifest 结构不合法，拒绝加载")
    lines = raw.splitlines(keepends=True)
    if (manifest["case_count"] != len(lines)
            or manifest["line_sha256"] != [_digest(line) for line in lines]
            or manifest["file_sha256"] != _digest(raw)):
        raise ValueError(f"转换版本 {version} 与封版 manifest 的 sha256 不一致，拒绝加载")
    return raw


def _append(case: dict[str, Any], version: str) -> None:
    if manifest_path(version).exists():
        raise ValueError(f"转换版本 {version} 已封版，不能追加")
    with version_path(version).open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(case, ensure_ascii=False) + "\n")


def append_case(case: dict[str, Any], *, version: str | None = None) -> None:
    with _version_lock():
        _append(case, version or current_version())


def append_new_case(case: dict[str, Any]) -> str:
    """选版、编号和追加必须在同一把锁内；返回实际写入的版本。"""
    with _version_lock():
        version = current_version()
        case["case_id"] = next_case_id(version)
        _append(case, version)
        return version


def load_version(version: str = CURRENT_VERSION) -> list[dict[str, Any]]:
    path = version_path(version)
    if manifest_path(version).exists():
        raw = _verified_manifest(version, manifest_path(version).read_bytes())
    elif path.exists():
        raw = path.read_bytes()
    else:
        return []
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]


def next_case_id(version: str | None = None) -> str:
    version = version or current_version()
    return f"fb_{version}_{len(load_version(version)) + 1:03d}"


def seal_version(version: str, actor: str) -> dict[str, Any]:
    """将当前开放版本的字节级摘要原子写入旁侧 manifest。"""
    if not actor.strip():
        raise ValueError("封版操作人不能为空")
    with _version_lock():
        if version != current_version() or not version_path(version).exists():
            raise ValueError(f"{version} 不是有数据的当前开放版本")
        raw = version_path(version).read_bytes()
        lines = raw.splitlines(keepends=True)
        if not lines:
            raise ValueError("空版本不能封版")
        load_version(version)
        manifest = {
            "version": version,
            "case_count": len(lines),
            "line_sha256": [_digest(line) for line in lines],
            "file_sha256": _digest(raw),
            "sealed_at": datetime.now(tz=UTC).isoformat(),
            "actor": actor,
        }
        path = manifest_path(version)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)
        return manifest


def load_sealed_version(version: str) -> tuple[list[dict[str, Any]], str]:
    """评测只接收已封版且摘要匹配的快照。"""
    path = manifest_path(version)
    if not path.exists():
        raise ValueError(f"转换版本 {version} 尚未封版，拒绝评测")
    manifest_raw = path.read_bytes()
    raw = _verified_manifest(version, manifest_raw)
    records = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    return records, _digest(manifest_raw)
