"""知识库上传校验（纯函数，零 DB 依赖）。

逐文件检查：文件名（防路径穿越）→ 扩展名 → 大小 → UTF-8 → front matter → 正文；
再做整体检查：文件数、总大小、重名、document_id 重复。
任一不通过即整体拒绝（HTTP 400），错误逐条指出是哪个文件、哪项不合格——不做「部分成功」。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from app.rag.loader import REQUIRED_FIELDS, parse_document, parse_front_matter
from app.services.errors import AppError

MAX_FILES = 50
MAX_FILE_BYTES = 200 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024

# 只允许「纯文件名」：不含任何目录成分；首字符不能是点（拒绝隐藏文件与 ..）
_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_DOCUMENT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CATEGORY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class UploadedFile:
    filename: str
    data: bytes  # 调用方最多读取 MAX_FILE_BYTES + 1 字节，超出即可判定超限


@dataclass(frozen=True)
class ValidDocument:
    path: str  # category/文件名：由 front matter 的 category 与纯文件名拼成，不采信客户端路径
    document_id: str
    document_name: str
    content: str
    content_hash: str
    size_bytes: int


@dataclass(frozen=True)
class FileError:
    file: str
    check: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"file": self.file, "check": self.check, "message": self.message}


class UploadRejected(AppError):
    """上传校验不通过：400，errors 逐条列出文件与不合格项。"""

    code = "KB_UPLOAD_INVALID"
    http_status = 400

    def __init__(self, errors: list[FileError]) -> None:
        summary = "；".join(f"{e.file}: {e.message}" for e in errors[:3])
        more = f"（另有 {len(errors) - 3} 项）" if len(errors) > 3 else ""
        super().__init__(f"上传校验未通过：{summary}{more}")
        self.errors = errors

    def extra(self) -> dict[str, Any]:
        return {"errors": [e.as_dict() for e in self.errors]}


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def source_hash(docs: list[ValidDocument]) -> str:
    """版本来源哈希：与上传顺序无关，只由 (path, 文档内容哈希) 集合决定。"""
    lines = sorted(f"{d.path}:{d.content_hash}" for d in docs)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def check_filename(name: str) -> FileError | None:
    if not name:
        return FileError("(未命名)", "filename", "文件名为空")
    if any(sep in name for sep in ("/", "\\", "\x00", ":")) or ".." in name:
        return FileError(name, "path", "文件名不能包含路径成分（/、\\、..、盘符），只接受纯文件名")
    if not name.endswith(".md"):
        return FileError(name, "extension", "只接受 .md 文件")
    if not _FILENAME.match(name):
        return FileError(name, "filename", "文件名只能由字母、数字、点、下划线、连字符组成，且不能以点开头")
    return None


def _validate_one(f: UploadedFile, *, enforce_limits: bool) -> ValidDocument | FileError:
    name_error = check_filename(f.filename)
    if name_error:
        return name_error
    name = f.filename
    if not f.data:
        return FileError(name, "size", "文件为空")
    if enforce_limits and len(f.data) > MAX_FILE_BYTES:
        return FileError(name, "size", f"超过单文件上限 {MAX_FILE_BYTES // 1024} KB")
    try:
        text = f.data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return FileError(name, "encoding", f"不是有效的 UTF-8 编码（第 {exc.start} 字节起无法解码）")
    text = text.removeprefix("\ufeff")  # 容忍 UTF-8 BOM
    text = text.replace("\r\n", "\n")

    fields, _ = parse_front_matter(text)
    if fields is None:
        return FileError(name, "front_matter", "缺少 front matter（文件开头需要用 --- 包围的元数据块）")
    missing = [k for k in REQUIRED_FIELDS if not fields.get(k)]
    if missing:
        return FileError(name, "front_matter", f"front matter 缺少必需字段：{', '.join(missing)}")
    if not _DOCUMENT_ID.match(fields["document_id"]):
        return FileError(name, "front_matter", "document_id 只能由小写字母、数字、下划线、连字符组成（≤64）")
    if not _CATEGORY.match(fields["category"]):
        return FileError(name, "front_matter", "category 只能由小写字母、数字、下划线、连字符组成（≤32）")
    if len(fields["document_name"]) > 128:
        return FileError(name, "front_matter", "document_name 超过 128 个字符")

    doc = parse_document(text, name)
    if not any(body.strip() for _, body in doc.sections):
        return FileError(name, "content", "正文为空，切分不出任何段落")
    return ValidDocument(
        path=f"{fields['category']}/{name}",
        document_id=fields["document_id"],
        document_name=fields["document_name"],
        content=text,
        content_hash=content_hash(text),
        size_bytes=len(text.encode("utf-8")),
    )


def validate_documents(files: list[UploadedFile], *, enforce_limits: bool = True) -> list[ValidDocument]:
    """全部通过才返回；否则抛 UploadRejected（包含所有不合格项）。

    enforce_limits=False 仅用于仓库内受信任的 knowledge_base/ 目录导入（不受上传数量/大小限制）。
    """
    errors: list[FileError] = []
    if not files:
        raise UploadRejected([FileError("*", "count", "至少上传 1 个 .md 文件")])
    if enforce_limits:
        if len(files) > MAX_FILES:
            errors.append(FileError("*", "count", f"文件数 {len(files)} 超过上限 {MAX_FILES}"))
        total = sum(len(f.data) for f in files)
        if total > MAX_TOTAL_BYTES:
            errors.append(FileError("*", "total_size", f"总大小超过上限 {MAX_TOTAL_BYTES // 1024 // 1024} MB"))

    docs: list[ValidDocument] = []
    seen_names: dict[str, str] = {}
    for f in files:
        key = f.filename.lower()
        if key in seen_names:
            errors.append(FileError(f.filename, "duplicate", "同一次上传中文件名重复"))
            continue
        seen_names[key] = f.filename
        result = _validate_one(f, enforce_limits=enforce_limits)
        if isinstance(result, FileError):
            errors.append(result)
        else:
            docs.append(result)

    by_id: dict[str, str] = {}
    for d in docs:
        filename = d.path.split("/", 1)[1]
        if d.document_id in by_id:
            errors.append(
                FileError(filename, "duplicate", f"document_id {d.document_id} 与 {by_id[d.document_id]} 重复")
            )
        else:
            by_id[d.document_id] = filename
    if errors:
        raise UploadRejected(errors)
    return docs
