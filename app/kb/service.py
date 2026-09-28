"""知识库版本生命周期：创建草稿 → 后台导入与校验 → 发布 / 回滚 → 重启恢复。

并发与原子性（D-021）：
- 导入：每个版本导入期间持有事务级 advisory lock (KB_INGEST_LOCK, version_id)。进程崩溃 → 连接断开 →
  锁自动释放；重启恢复只把「创建已超过宽限期 且 拿得到锁」的 DRAFT/INGESTING 标为 FAILED：
  不误伤其他存活进程正在导入的版本，也不误伤别的实例刚上传、后台任务还没来得及取锁的草稿。
- 发布/回滚：单事务内完成「旧 ACTIVE → RETIRED、目标 → ACTIVE、写审计」。事务先取全局 advisory lock 串行化，
  再做比较交换（调用方给出它看到的生效版本 expected_active_version_id，不一致即 409），
  所以并发发布只有一个成功；部分唯一索引 uq_kb_versions_single_active 在数据库层兜底「至多一个 ACTIVE」。
- 导入只写新版本自己的行，从不删除或修改其他版本：导入失败不影响当前 ACTIVE。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.kb.smoke import SmokeConfig
from app.kb.validation import FileError, UploadedFile, UploadRejected, ValidDocument, source_hash, validate_documents
from app.models.base import utcnow
from app.models.knowledge import (
    EMBEDDING_DIM,
    KB_STATUS_ACTIVE,
    KB_STATUS_DRAFT,
    KB_STATUS_FAILED,
    KB_STATUS_INGESTING,
    KB_STATUS_READY,
    KB_STATUS_RETIRED,
    KbAuditLog,
    KbChunk,
    KbDocument,
    KbVersion,
)
from app.rag.chunker import chunk_document
from app.rag.embedding import EmbeddingClient, backend_name
from app.rag.loader import parse_document
from app.rag.store import search
from app.services.errors import AppError, NotFoundError

logger = logging.getLogger(__name__)

SYSTEM_ACTOR = "system"
KB_INGEST_LOCK = 72_001  # advisory lock 命名空间：(KB_INGEST_LOCK, version_id)
KB_PUBLISH_LOCK = 72_002  # advisory lock 命名空间：(KB_PUBLISH_LOCK, 0) 串行化所有发布/回滚

SessionFactory = Callable[[], Session]


class KbConflictError(AppError):
    code = "KB_CONFLICT"
    http_status = 409


class IngestCheckFailed(Exception):
    def __init__(self, reason: str, checks: dict[str, Any]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.checks = checks


@dataclass(frozen=True)
class Retrieval:
    """导入校验所用的检索配置：必须与线上检索同一个 embedder 与拒答阈值。"""

    embedder: EmbeddingClient
    threshold: float
    smoke: SmokeConfig


def _audit(
    db: Session,
    version_id: int,
    action: str,
    actor: str,
    from_status: str | None,
    to_status: str | None,
    detail: dict[str, Any] | None = None,
) -> None:
    db.add(
        KbAuditLog(
            version_id=version_id,
            action=action,
            actor=actor,
            from_status=from_status,
            to_status=to_status,
            detail=detail,
        )
    )


def _try_lock(db: Session, namespace: int, key: int) -> bool:
    return bool(
        db.scalar(
            text("SELECT pg_try_advisory_xact_lock(CAST(:ns AS integer), CAST(:key AS integer))"),
            {"ns": namespace, "key": key},
        )
    )


def active_version(db: Session) -> KbVersion | None:
    return db.scalar(select(KbVersion).where(KbVersion.status == KB_STATUS_ACTIVE))


# ---------------------------------------------------------------- 创建草稿


def directory_documents(root: Path) -> list[ValidDocument]:
    """仓库内 knowledge_base/ 目录 → 与上传同一套校验（不受上传数量/大小限制）。"""
    files = [UploadedFile(p.name, p.read_bytes()) for p in sorted(root.rglob("*.md"))]
    return validate_documents(files, enforce_limits=False)


def merge_with_active(db: Session, uploaded: list[ValidDocument]) -> list[ValidDocument]:
    """合并模式：以当前 ACTIVE 版本的文档为底，按 document_id 覆盖或新增。"""
    active = active_version(db)
    if active is None:
        return uploaded
    replaced = {d.document_id for d in uploaded}
    base = [
        ValidDocument(
            path=d.path,
            document_id=d.document_id,
            document_name=d.document_name,
            content=d.content,
            content_hash=d.content_hash,
            size_bytes=d.size_bytes,
        )
        for d in db.scalars(select(KbDocument).where(KbDocument.version_id == active.id).order_by(KbDocument.path))
        if d.document_id not in replaced
    ]
    taken = {d.path: d.document_id for d in base}
    errors = [
        FileError(d.path.split("/", 1)[1], "path", f"路径 {d.path} 已被当前生效版本的文档 {taken[d.path]} 占用")
        for d in uploaded
        if d.path in taken
    ]
    if errors:
        raise UploadRejected(errors)
    return base + uploaded


def create_draft(
    db: Session, docs: list[ValidDocument], *, source: str, actor: str, note: str | None = None
) -> KbVersion:
    """保存文档原文与哈希，创建 DRAFT 版本（此时不可检索，也不可发布）。"""
    version = KbVersion(
        status=KB_STATUS_DRAFT,
        source=source,
        source_hash=source_hash(docs),
        note=note,
        created_by=actor,
        doc_count=len(docs),
        progress_total=len(docs),
    )
    db.add(version)
    db.flush()
    db.add_all(
        KbDocument(
            version_id=version.id,
            path=d.path,
            document_id=d.document_id,
            document_name=d.document_name,
            content=d.content,
            content_hash=d.content_hash,
            size_bytes=d.size_bytes,
        )
        for d in docs
    )
    _audit(db, version.id, "CREATED", actor, None, KB_STATUS_DRAFT, {"source": source, "doc_count": len(docs)})
    db.commit()
    return version


# ---------------------------------------------------------------- 导入与校验


def _smoke_checks(db: Session, version_id: int, retrieval: Retrieval) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for q in retrieval.smoke.queries:
        hits = search(db, retrieval.embedder.embed_query(q.query), retrieval.smoke.top_k, version_id=version_id)
        expected = [h for h in hits if h.document_id == q.expect_document_id]
        best = max((h.score for h in expected), default=None)
        results.append(
            {
                "query": q.query,
                "expect_document_id": q.expect_document_id,
                "passed": best is not None and best >= retrieval.threshold,
                "best_score": best,
                "top": [{"document_id": h.document_id, "score": h.score} for h in hits],
            }
        )
    return {
        "passed": all(r["passed"] for r in results),
        "top_k": retrieval.smoke.top_k,
        "threshold": retrieval.threshold,
        "results": results,
    }


def _describe_failures(checks: dict[str, Any]) -> str:
    reasons: list[str] = []
    cc = checks.get("chunk_count")
    if cc and not cc["passed"]:
        reasons.append(f"chunk 数量 {cc['value']} 少于要求的 {cc['min']}（每篇文档至少 1 个）")
    smoke = checks.get("smoke")
    if smoke and not smoke["passed"]:
        for r in smoke["results"]:
            if r["passed"]:
                continue
            top = "、".join(f"{t['document_id']}({t['score']:.3f})" for t in r["top"]) or "无结果"
            reasons.append(
                f"冒烟查询「{r['query']}」未命中 {r['expect_document_id']}"
                f"（要求 top{smoke['top_k']} 且分数 ≥ {smoke['threshold']}；实际：{top}）"
            )
    return "；".join(reasons)


def _mark_failed(factory: SessionFactory, version_id: int, reason: str, checks: dict[str, Any] | None) -> None:
    with factory() as db:
        v = db.get(KbVersion, version_id, with_for_update=True)
        if v is None or v.status != KB_STATUS_INGESTING:
            return
        now = utcnow()
        v.status, v.failure_reason, v.checks = KB_STATUS_FAILED, reason[:2000], checks
        v.finished_at = v.updated_at = now
        _audit(
            db, version_id, "INGEST_FAILED", SYSTEM_ACTOR, KB_STATUS_INGESTING, KB_STATUS_FAILED,
            {"reason": reason[:500]},
        )
        db.commit()


def _ingest_locked(factory: SessionFactory, version_id: int, retrieval: Retrieval) -> str:
    with factory() as db:
        v = db.get(KbVersion, version_id, with_for_update=True)
        if v is None:
            raise NotFoundError(f"知识库版本不存在: {version_id}")
        if v.status != KB_STATUS_DRAFT:
            return v.status  # 已导入 / 已被重启恢复标记失败：不重复处理
        v.status, v.progress_done, v.updated_at = KB_STATUS_INGESTING, 0, utcnow()
        v.embedding_backend = backend_name(retrieval.embedder)
        _audit(db, version_id, "INGEST_STARTED", SYSTEM_ACTOR, KB_STATUS_DRAFT, KB_STATUS_INGESTING)
        db.commit()

    checks: dict[str, Any] = {}
    try:
        with factory() as db:
            docs = list(
                db.scalars(select(KbDocument).where(KbDocument.version_id == version_id).order_by(KbDocument.path))
            )
            dim = int(getattr(retrieval.embedder, "dim", 0))
            rows: list[KbChunk] = []
            for i, d in enumerate(docs, start=1):
                chunks = chunk_document(parse_document(d.content, d.path))
                vectors = retrieval.embedder.embed_documents([c.content for c in chunks])
                bad = next((v for v in vectors if len(v) != EMBEDDING_DIM or not all(map(math.isfinite, v))), None)
                if dim != EMBEDDING_DIM or bad is not None:
                    actual = len(bad) if bad is not None else dim
                    checks["embedding_dim"] = {"passed": False, "expected": EMBEDDING_DIM, "actual": actual}
                    raise IngestCheckFailed(
                        f"向量维度校验失败：{d.path} 得到 {actual} 维（或含非有限值），要求 {EMBEDDING_DIM} 维",
                        checks,
                    )
                rows.extend(
                    KbChunk(
                        version_id=version_id,
                        chunk_id=c.chunk_id,
                        document_id=c.document_id,
                        document_name=c.document_name,
                        section=c.section,
                        content=c.content,
                        metadata_=dict(c.metadata),
                        embedding=vec,
                    )
                    for c, vec in zip(chunks, vectors, strict=True)
                )
                # 进度单独提交：管理页面轮询可见；chunk 行留到最后与校验同一事务写入
                db.execute(
                    update(KbVersion).where(KbVersion.id == version_id).values(progress_done=i, updated_at=utcnow())
                )
                db.commit()
            checks["embedding_dim"] = {"passed": True, "expected": EMBEDDING_DIM, "actual": EMBEDDING_DIM}

            db.add_all(rows)
            db.flush()  # 同一事务内可见，用于冒烟检索；未提交前其他会话看不到
            min_chunks = max(1, len(docs))
            checks["chunk_count"] = {"passed": len(rows) >= min_chunks, "value": len(rows), "min": min_chunks}
            checks["smoke"] = _smoke_checks(db, version_id, retrieval)
            if not (checks["chunk_count"]["passed"] and checks["smoke"]["passed"]):
                db.rollback()  # 校验不过：本版本 chunk 不落库
                raise IngestCheckFailed(_describe_failures(checks), checks)

            v = db.get(KbVersion, version_id, with_for_update=True)
            assert v is not None
            now = utcnow()
            v.status, v.chunk_count, v.checks = KB_STATUS_READY, len(rows), checks
            v.finished_at = v.updated_at = now
            _audit(
                db, version_id, "INGEST_READY", SYSTEM_ACTOR, KB_STATUS_INGESTING, KB_STATUS_READY,
                {"chunks": len(rows)},
            )
            db.commit()
            return KB_STATUS_READY
    except IngestCheckFailed as exc:
        _mark_failed(factory, version_id, exc.reason, exc.checks)
    except Exception as exc:  # embedding / 解析 / 数据库异常：一律判失败并记录原因，不影响 ACTIVE
        logger.exception("知识库版本 %s 导入异常", version_id)
        _mark_failed(factory, version_id, f"导入异常 {type(exc).__name__}: {exc}", checks or None)
    return KB_STATUS_FAILED


def ingest_version(factory: SessionFactory, version_id: int, retrieval: Retrieval) -> str:
    """把 DRAFT 版本导入为 READY / FAILED，返回最终状态。可在后台线程运行。"""
    lock_session = factory()
    try:
        if not _try_lock(lock_session, KB_INGEST_LOCK, version_id):
            return KB_STATUS_INGESTING  # 另一个进程正在导入该版本
        return _ingest_locked(factory, version_id, retrieval)
    finally:
        lock_session.close()  # 回滚持锁事务 → 释放 advisory lock


def recover_interrupted(factory: SessionFactory, *, grace: timedelta | None = None) -> list[int]:
    """启动时调用：DRAFT/INGESTING、创建早于宽限期、且没有任何存活连接持有导入锁的版本 → FAILED。

    grace 默认取配置 KB_RECOVER_GRACE_MINUTES（10 分钟）。
    """
    if grace is None:
        from app.config import get_settings

        grace = timedelta(minutes=get_settings().kb_recover_grace_minutes)
    cutoff = utcnow() - grace
    recovered: list[int] = []
    with factory() as db:
        ids = db.scalars(
            select(KbVersion.id)
            .where(
                KbVersion.status.in_((KB_STATUS_DRAFT, KB_STATUS_INGESTING)),
                KbVersion.created_at < cutoff,
            )
            .order_by(KbVersion.id)
        ).all()
        for vid in ids:
            if not _try_lock(db, KB_INGEST_LOCK, vid):
                continue  # 有存活进程正在导入
            v = db.get(KbVersion, vid, with_for_update=True, populate_existing=True)
            if v is None or v.status not in (KB_STATUS_DRAFT, KB_STATUS_INGESTING):
                continue
            previous = v.status
            now = utcnow()
            v.status = KB_STATUS_FAILED
            v.failure_reason = f"服务重启时该版本仍处于 {previous}，导入未完成，已标记失败；请重新上传"
            v.finished_at = v.updated_at = now
            _audit(db, vid, "RECOVERED_AFTER_RESTART", SYSTEM_ACTOR, previous, KB_STATUS_FAILED)
            recovered.append(vid)
        db.commit()
    return recovered


# ---------------------------------------------------------------- 发布与回滚


def publish(
    db: Session,
    version_id: int,
    *,
    actor: str,
    expected_active_version_id: int | None,
    rollback: bool = False,
    serving_backend: str | None = None,
) -> KbVersion:
    """READY → ACTIVE（发布）或 RETIRED → ACTIVE（回滚），原 ACTIVE → RETIRED；单事务原子完成。"""
    required = KB_STATUS_RETIRED if rollback else KB_STATUS_READY
    action = "回滚到" if rollback else "发布"
    try:
        db.execute(text("SELECT pg_advisory_xact_lock(CAST(:ns AS integer), 0)"), {"ns": KB_PUBLISH_LOCK})
        target = db.get(KbVersion, version_id, with_for_update=True, populate_existing=True)
        if target is None:
            raise NotFoundError(f"知识库版本不存在: {version_id}")
        if target.status != required:
            raise KbConflictError(f"v{version_id} 当前状态为 {target.status}，只有 {required} 版本可以{action}")
        current = db.scalar(
            select(KbVersion).where(KbVersion.status == KB_STATUS_ACTIVE).with_for_update().execution_options(
                populate_existing=True
            )
        )
        current_id = current.id if current else None
        if current_id != expected_active_version_id:
            raise KbConflictError(
                f"生效版本已变化：操作基于 {_label(expected_active_version_id)}，"
                f"当前为 {_label(current_id)}；请刷新后重试"
            )
        if serving_backend and target.embedding_backend and target.embedding_backend != serving_backend:
            raise KbConflictError(
                f"v{version_id} 的向量由 {target.embedding_backend} 生成，"
                f"与当前服务的 {serving_backend} 不一致，不能{action}"
            )
        now = utcnow()
        if current is not None:
            current.status, current.updated_at = KB_STATUS_RETIRED, now
            _audit(db, current.id, "RETIRED", actor, KB_STATUS_ACTIVE, KB_STATUS_RETIRED, {"replaced_by": version_id})
            db.flush()  # 先退役再生效：部分唯一索引逐语句检查
        target.status, target.activated_at, target.updated_at = KB_STATUS_ACTIVE, now, now
        _audit(
            db,
            version_id,
            "ROLLED_BACK" if rollback else "ACTIVATED",
            actor,
            required,
            KB_STATUS_ACTIVE,
            {"previous_active_version_id": current_id},
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise KbConflictError("并发发布冲突：已有其他版本生效，请刷新后重试") from exc
    except Exception:
        db.rollback()
        raise
    return target


def _label(version_id: int | None) -> str:
    return f"v{version_id}" if version_id is not None else "「无生效版本」"


# ---------------------------------------------------------------- 目录导入（启动初始化 / 评测）


def create_and_ingest(
    factory: SessionFactory, docs: list[ValidDocument], retrieval: Retrieval, *, source: str, actor: str, note: str
) -> KbVersion:
    with factory() as db:
        vid = create_draft(db, docs, source=source, actor=actor, note=note).id
    ingest_version(factory, vid, retrieval)
    with factory() as db:
        v = db.get(KbVersion, vid)
        assert v is not None
        return v


def bootstrap_from_directory(factory: SessionFactory, root: Path, retrieval: Retrieval) -> tuple[str, KbVersion]:
    """没有 ACTIVE 版本时把目录导入为初始版本并生效；已有 ACTIVE 则什么都不做。

    返回 (结果, 版本)：SKIPPED（已有生效版本）/ ACTIVATED / RACED（并发启动时别的进程先生效了）。
    """
    with factory() as db:
        current = active_version(db)
        if current is not None:
            return "SKIPPED", current
    version = create_and_ingest(
        factory,
        directory_documents(root),
        retrieval,
        source="BOOTSTRAP",
        actor=SYSTEM_ACTOR,
        note=f"启动初始化：导入 {root.name}/ 目录",
    )
    if version.status != KB_STATUS_READY:
        raise RuntimeError(f"初始知识库版本 v{version.id} 导入失败：{version.failure_reason}")
    with factory() as db:
        try:
            return "ACTIVATED", publish(db, version.id, actor=SYSTEM_ACTOR, expected_active_version_id=None)
        except KbConflictError:
            return "RACED", version


def find_or_ingest_directory(
    factory: SessionFactory, root: Path, retrieval: Retrieval, *, actor: str, source: str = "EVAL"
) -> KbVersion:
    """评测用：复用内容与向量后端都相同、已校验通过的版本；没有则导入一个新版本（不发布）。"""
    docs = directory_documents(root)
    digest = source_hash(docs)
    backend = backend_name(retrieval.embedder)
    with factory() as db:
        existing = db.scalar(
            select(KbVersion)
            .where(
                KbVersion.source_hash == digest,
                KbVersion.embedding_backend == backend,
                KbVersion.status.in_((KB_STATUS_READY, KB_STATUS_ACTIVE, KB_STATUS_RETIRED)),
            )
            .order_by(KbVersion.id.desc())
            .limit(1)
        )
        if existing is not None:
            return existing
    version = create_and_ingest(
        factory, docs, retrieval, source=source, actor=actor, note=f"按 {root.name}/ 目录导入（{source}）"
    )
    if version.status != KB_STATUS_READY:
        raise RuntimeError(f"知识库版本 v{version.id} 导入失败：{version.failure_reason}")
    return version


# ---------------------------------------------------------------- 引用追溯


def resolve_citations(db: Session, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 (version_id, chunk_id) 取回回答当时引用的原文；版本 RETIRED 后仍可查。"""
    out: list[dict[str, Any]] = []
    for s in sources:
        raw_vid = s.get("version_id")
        base = {
            "document": s.get("document"),
            "section": s.get("section"),
            "chunk_id": s.get("chunk_id"),
            "version_id": int(raw_vid) if raw_vid not in (None, "") else None,
        }
        if base["version_id"] is None:
            # 版本化之前的旧回答没有记录版本：不按 chunk_id 猜测，诚实返回无法追溯
            out.append({**base, "found": False, "reason": "该引用早于知识库版本化，未记录版本号"})
            continue
        row = db.execute(
            select(KbChunk.content, KbVersion.status)
            .join(KbVersion, KbVersion.id == KbChunk.version_id)
            .where(KbChunk.version_id == base["version_id"], KbChunk.chunk_id == s.get("chunk_id"))
        ).first()
        if row is None:
            out.append({**base, "found": False, "reason": "该版本中找不到这个 chunk"})
        else:
            out.append({**base, "found": True, "content": row.content, "version_status": row.status})
    return out

