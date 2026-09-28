"""知识库版本管理 API（仅 KB_ADMIN）：版本列表 / 详情、上传（后台导入）、发布、回滚。

上传只做同步校验与落库草稿（原文 + 哈希），导入在后台进行（DRAFT → INGESTING → READY/FAILED）；
页面轮询版本列表看进度与失败原因。发布与回滚是比较交换：请求必须带上调用方看到的生效版本号。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.kb.service import Retrieval, active_version, create_draft, ingest_version, merge_with_active, publish
from app.kb.smoke import load_smoke_config
from app.kb.validation import MAX_FILE_BYTES, UploadedFile, validate_documents
from app.models.knowledge import KB_STATUS_ACTIVE, KbAuditLog, KbDocument, KbVersion
from app.models.user import User
from app.rag.embedding import backend_name, serving_retrieval
from app.schemas.api import (
    KbAuditOut,
    KbDocumentOut,
    KbPublishRequest,
    KbVersionDetailOut,
    KbVersionListOut,
    KbVersionOut,
)
from app.security.dependencies import get_kb_admin_user
from app.services import database
from app.services.database import get_db
from app.services.errors import NotFoundError

router = APIRouter()


def _serving_config() -> Retrieval:
    embedder, threshold = serving_retrieval()
    return Retrieval(embedder=embedder, threshold=threshold, smoke=load_smoke_config())


def _ingest_in_background(version_id: int, retrieval: Retrieval) -> None:
    # 运行时取会话工厂（测试会替换 SessionLocal）；导入异常已在 ingest_version 内落为 FAILED
    ingest_version(database.SessionLocal, version_id, retrieval)


@router.get("/versions", response_model=KbVersionListOut)
def list_versions(_: User = Depends(get_kb_admin_user), db: Session = Depends(get_db)) -> KbVersionListOut:
    versions = db.scalars(select(KbVersion).order_by(KbVersion.id.desc())).all()
    active = next((v.id for v in versions if v.status == KB_STATUS_ACTIVE), None)
    return KbVersionListOut(active_version_id=active, versions=[KbVersionOut.model_validate(v) for v in versions])


@router.get("/versions/{version_id}", response_model=KbVersionDetailOut)
def get_version(
    version_id: int, _: User = Depends(get_kb_admin_user), db: Session = Depends(get_db)
) -> KbVersionDetailOut:
    version = db.get(KbVersion, version_id)
    if version is None:
        raise NotFoundError(f"知识库版本不存在: {version_id}")
    out = KbVersionDetailOut.model_validate(version)
    out.checks = version.checks
    out.documents = [
        KbDocumentOut.model_validate(d)
        for d in db.scalars(select(KbDocument).where(KbDocument.version_id == version_id).order_by(KbDocument.path))
    ]
    out.audit = [
        KbAuditOut.model_validate(a)
        for a in db.scalars(select(KbAuditLog).where(KbAuditLog.version_id == version_id).order_by(KbAuditLog.id))
    ]
    return out


@router.post("/versions", response_model=KbVersionOut, status_code=202)
def upload_version(
    background: BackgroundTasks,
    files: list[UploadFile] = File(...),
    mode: Literal["merge", "replace"] = Form("merge"),
    user: User = Depends(get_kb_admin_user),
    db: Session = Depends(get_db),
) -> KbVersionOut:
    """merge：以当前生效版本为底，按 document_id 覆盖或新增；replace：新版本只含本次上传的文件。"""
    retrieval = _serving_config()  # 配置有误时在创建任何数据之前失败
    # 每个文件最多读「上限 + 1」字节：足以判定超限，不会把超大文件整个读进内存
    uploaded = [UploadedFile(f.filename or "", f.file.read(MAX_FILE_BYTES + 1)) for f in files]
    docs = validate_documents(uploaded)
    if mode == "merge":
        base = active_version(db)
        docs = merge_with_active(db, docs)
        note = f"合并上传 {len(uploaded)} 个文件" + (f"（基于 v{base.id}）" if base else "（当前无生效版本）")
    else:
        note = f"完整替换：上传 {len(uploaded)} 个文件"
    version = create_draft(db, docs, source="UPLOAD", actor=user.id, note=note)
    background.add_task(_ingest_in_background, version.id, retrieval)
    return KbVersionOut.model_validate(version)


def _publish(db: Session, version_id: int, body: KbPublishRequest, user: User, *, rollback: bool) -> KbVersionOut:
    embedder, _ = serving_retrieval()
    version = publish(
        db,
        version_id,
        actor=user.id,
        expected_active_version_id=body.expected_active_version_id,
        rollback=rollback,
        serving_backend=backend_name(embedder),
    )
    return KbVersionOut.model_validate(version)


@router.post("/versions/{version_id}/activate", response_model=KbVersionOut)
def activate_version(
    version_id: int,
    body: KbPublishRequest,
    user: User = Depends(get_kb_admin_user),
    db: Session = Depends(get_db),
) -> KbVersionOut:
    """READY → ACTIVE，原 ACTIVE → RETIRED。"""
    return _publish(db, version_id, body, user, rollback=False)


@router.post("/versions/{version_id}/rollback", response_model=KbVersionOut)
def rollback_version(
    version_id: int,
    body: KbPublishRequest,
    user: User = Depends(get_kb_admin_user),
    db: Session = Depends(get_db),
) -> KbVersionOut:
    """RETIRED → ACTIVE，原 ACTIVE → RETIRED。"""
    return _publish(db, version_id, body, user, rollback=True)
