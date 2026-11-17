# -*- coding: utf-8 -*-
"""E37 · 第 13 组路由：/api/composer（标书正向编制骨架 V1 / V2 方案库、蓝图回放、初稿下载与级联删除）。"""
from __future__ import annotations

from pathlib import Path
import shutil
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from backend.auth import AuthUser, get_current_user, record_audit
from backend.database import PROJECT_ROOT, get_db
from backend.models import (
    BidCheckTask,
    BidComposeDraft,
    BidFinding,
    BidRuleConfig,
    CompanyCertificate,
)
from backend.schemas import (
    ComposeDraftDetailOut,
    ComposeDraftListOut,
    ComposeDraftSummaryOut,
    OkResponse,
)
from backend.worker import resolve_project_path
from core.bid_composer import compose_bid_blueprint, export_composed_bid_docx

router = APIRouter(prefix='/api/composer', tags=['composer'])

_ALLOWED_EXTS = {'.docx', '.pdf', '.txt'}
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100MB
_COMPOSER_UPLOADS_ROOT = (PROJECT_ROOT / 'data' / 'uploads' / 'composer').resolve()
_COMPOSER_OUTPUT_ROOT = (PROJECT_ROOT / 'output' / 'composer').resolve()
_DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'


def _validate_and_read_upload(upload: UploadFile, role_label: str) -> tuple[str, str, bytes]:
    orig_name = Path(upload.filename or f"{role_label}.docx").name
    ext = Path(orig_name).suffix.lower()
    if ext not in _ALLOWED_EXTS:
        raise HTTPException(
            status_code=422,
            detail={
                'code': 'UNSUPPORTED_FILE_TYPE',
                'message': f"不支持的文件格式 {ext}（{orig_name}），仅支持 .docx/.pdf/.txt",
            },
        )
    content = upload.file.read()
    if not content:
        raise HTTPException(
            status_code=422,
            detail={'code': 'EMPTY_FILE', 'message': f"上传的文件 {orig_name} 内容为空"},
        )
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=422,
            detail={'code': 'FILE_TOO_LARGE', 'message': f"文件 {orig_name} 超过 100MB 上限"},
        )
    return orig_name, ext, content


def _get_tenant_draft_or_404(db: Session, draft_id: int, tenant_id: int) -> BidComposeDraft:
    row = (
        db.query(BidComposeDraft)
        .filter_by(id=draft_id, tenant_id=tenant_id)
        .first()
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={'code': 'COMPOSER_DRAFT_NOT_FOUND', 'message': '标书编制草稿不存在或无权访问'},
        )
    return row


def _to_summary_out(row: BidComposeDraft) -> ComposeDraftSummaryOut:
    bp = row.blueprint_json or {}
    stats = row.stats_json or bp.get('stats') or {}
    mode = row.compose_mode if row.compose_mode in ('skeleton_v1', 'knowledge_draft_v2') else 'skeleton_v1'
    return ComposeDraftSummaryOut(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        title=row.title,
        tender_filename=row.tender_filename,
        compose_mode=mode,  # type: ignore[arg-type]
        project_name=str(bp.get('project_name') or row.title or ''),
        project_code=str(bp.get('project_code') or ''),
        open_date=bp.get('open_date'),
        price_cap=bp.get('price_cap'),
        manual_item_count=int(stats.get('manual_item_count') or len(bp.get('manual_checklist') or [])),
        scoring_fill_mode=str(stats.get('scoring_fill_mode') or 'outline_stub'),
        stats=stats,
        download_url=f"/api/composer/drafts/{row.id}/download",
        created_at=row.created_at.isoformat() if row.created_at else None,
    )


def _to_detail_out(row: BidComposeDraft) -> ComposeDraftDetailOut:
    summary = _to_summary_out(row)
    return ComposeDraftDetailOut(
        **summary.model_dump(),
        blueprint=row.blueprint_json or {},
    )


@router.post('/generate', response_model=ComposeDraftDetailOut)
def generate_composed_draft(
    tender_file: Optional[UploadFile] = File(default=None),
    task_id: Optional[int] = Form(default=None),
    knowledge_files: list[UploadFile] = File(default=[]),
    compose_mode: str = Form(default='skeleton_v1'),
    title: Optional[str] = Form(default=None),
    package_no: Optional[int] = Form(default=None),
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ComposeDraftDetailOut:
    """E37 §3.3：上传招标书或复用体检任务生成正向初稿骨架（V1 零幻觉 / V2 可选方案库参考）与 .docx。"""
    mode_clean = (compose_mode or 'skeleton_v1').strip()
    if mode_clean not in ('skeleton_v1', 'knowledge_draft_v2'):
        raise HTTPException(
            status_code=422,
            detail={'code': 'INVALID_COMPOSE_MODE', 'message': f"非法编制模式: {mode_clean}"},
        )

    tenant_id = int(current_user.tenant_id)
    has_upload = tender_file is not None and bool((tender_file.filename or '').strip())
    if not has_upload and task_id is None:
        raise HTTPException(
            status_code=422,
            detail={'code': 'MISSING_TENDER_INPUT', 'message': '请上传招标文件，或指定一个已有的体检任务进行装配'},
        )

    source_task: Optional[BidCheckTask] = None
    f4_findings: list[BidFinding] = []
    if task_id is not None:
        source_task = db.query(BidCheckTask).filter_by(id=int(task_id), tenant_id=tenant_id).first()
        if source_task is None:
            raise HTTPException(
                status_code=404,
                detail={'code': 'TASK_NOT_FOUND', 'message': '关联的体检任务不存在或无权访问'},
            )
        f4_findings = (
            db.query(BidFinding)
            .filter(BidFinding.task_id == source_task.id, BidFinding.feedback != 'rejected')
            .order_by(BidFinding.id.asc())
            .all()
        )

    tender_bytes_info: Optional[tuple[str, str, bytes]] = None
    if has_upload and tender_file is not None:
        tender_bytes_info = _validate_and_read_upload(tender_file, 'tender')
    else:
        assert source_task is not None
        task_tender_abs = resolve_project_path(source_task.tender_path)
        if not task_tender_abs.is_file():
            raise HTTPException(
                status_code=422,
                detail={'code': 'TENDER_FILE_MISSING', 'message': '关联任务的招标源文件缺失，请重新上传招标文件'},
            )

    valid_knowledge_payloads: list[tuple[str, str, bytes]] = []
    for idx, kf in enumerate(knowledge_files or []):
        if kf is None or not (kf.filename or '').strip():
            continue
        valid_knowledge_payloads.append(_validate_and_read_upload(kf, f"knowledge_{idx}"))

    tender_filename_disp = (
        tender_bytes_info[0]
        if tender_bytes_info is not None
        else (source_task.tender_filename or '招标文件.docx')  # type: ignore[union-attr]
    )
    eff_title = (title or '').strip() or (
        source_task.title if source_task is not None and not tender_bytes_info else Path(tender_filename_disp).stem
    ) or '未命名投标编制初稿'

    draft = BidComposeDraft(
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        title=eff_title[:250],
        tender_filename=tender_filename_disp[:250],
        tender_path='',
        compose_mode=mode_clean,
        blueprint_json={},
        output_path='',
        stats_json={},
    )
    db.add(draft)
    db.flush()

    upload_dir = (_COMPOSER_UPLOADS_ROOT / str(tenant_id) / str(draft.id)).resolve()
    if not upload_dir.is_relative_to(_COMPOSER_UPLOADS_ROOT):
        db.rollback()
        raise HTTPException(status_code=422, detail={'code': 'PATH_TRAVERSAL', 'message': '非法上传目录'})
    upload_dir.mkdir(parents=True, exist_ok=True)

    saved_k_paths: list[Path] = []
    try:
        if tender_bytes_info is not None:
            orig_t_name, t_ext, t_bytes = tender_bytes_info
            tender_abs = (upload_dir / f"tender{t_ext}").resolve()
            tender_abs.write_bytes(t_bytes)
            tender_rel = tender_abs.relative_to(PROJECT_ROOT.resolve()).as_posix()
        else:
            assert source_task is not None
            tender_abs = resolve_project_path(source_task.tender_path)
            tender_rel = source_task.tender_path

        for idx, (_k_name, k_ext, k_bytes) in enumerate(valid_knowledge_payloads):
            k_abs = (upload_dir / f"knowledge_{idx}{k_ext}").resolve()
            k_abs.write_bytes(k_bytes)
            saved_k_paths.append(k_abs)

        certs = db.query(CompanyCertificate).filter_by(tenant_id=tenant_id).all()
        rule_cfg = (
            db.query(BidRuleConfig)
            .filter_by(tenant_id=tenant_id)
            .order_by(BidRuleConfig.is_default.desc(), BidRuleConfig.id.asc())
            .first()
        )

        blueprint = compose_bid_blueprint(
            tender_abs,
            ledger_certs=certs,
            rule_config=rule_cfg,
            f4_findings=f4_findings,
            task_title=eff_title,
            package_no=package_no,
            compose_mode=mode_clean,
            knowledge_paths=saved_k_paths,
        )
        bp_dict = blueprint.to_dict()

        _COMPOSER_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        out_docx_abs = (_COMPOSER_OUTPUT_ROOT / f"{tenant_id}_{draft.id}_投标文件初稿骨架.docx").resolve()
        if not out_docx_abs.is_relative_to(_COMPOSER_OUTPUT_ROOT):
            raise HTTPException(status_code=422, detail={'code': 'PATH_TRAVERSAL', 'message': '非法输出路径'})

        export_composed_bid_docx(bp_dict, out_docx_abs)
        out_rel = out_docx_abs.relative_to(PROJECT_ROOT.resolve()).as_posix()

        draft.tender_path = tender_rel
        draft.blueprint_json = bp_dict
        draft.stats_json = bp_dict.get('stats') or {}
        draft.output_path = out_rel
        if not (title or '').strip() and bp_dict.get('project_name'):
            draft.title = str(bp_dict['project_name'])[:250]
        db.commit()
        db.refresh(draft)
    except HTTPException:
        db.rollback()
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise
    except Exception as exc:
        db.rollback()
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise HTTPException(
            status_code=422,
            detail={'code': 'COMPOSER_GENERATE_FAILED', 'message': f"标书初稿生成失败：{exc}"},
        ) from exc

    record_audit(
        db,
        action='composer_generate',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"composer:{draft.id}",
        detail_json={
            'title': draft.title,
            'compose_mode': mode_clean,
            'tender_filename': draft.tender_filename,
            'knowledge_count': len(saved_k_paths),
            'manual_item_count': int((draft.stats_json or {}).get('manual_item_count') or 0),
        },
    )
    return _to_detail_out(draft)


@router.get('/drafts', response_model=ComposeDraftListOut)
def list_composed_drafts(
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ComposeDraftListOut:
    """列出当前租户的历史标书编制初稿草稿箱。"""
    tenant_id = int(current_user.tenant_id)
    rows = (
        db.query(BidComposeDraft)
        .filter_by(tenant_id=tenant_id)
        .order_by(BidComposeDraft.id.desc())
        .all()
    )
    return ComposeDraftListOut(
        total=len(rows),
        items=[_to_summary_out(r) for r in rows],
    )


@router.get('/drafts/{draft_id}', response_model=ComposeDraftDetailOut)
def get_composed_draft(
    draft_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ComposeDraftDetailOut:
    """查看标书编制初稿蓝图详情（严格租户隔离）。"""
    row = _get_tenant_draft_or_404(db, draft_id, int(current_user.tenant_id))
    return _to_detail_out(row)


@router.get('/drafts/{draft_id}/download')
def download_composed_draft(
    draft_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """下载生成的《投标文件初稿骨架.docx》。"""
    tenant_id = int(current_user.tenant_id)
    row = _get_tenant_draft_or_404(db, draft_id, tenant_id)
    out_file = (_COMPOSER_OUTPUT_ROOT / f"{tenant_id}_{row.id}_投标文件初稿骨架.docx").resolve()
    if not out_file.is_relative_to(_COMPOSER_OUTPUT_ROOT):
        raise HTTPException(status_code=422, detail={'code': 'PATH_TRAVERSAL', 'message': '非法下载路径'})
    if not out_file.is_file():
        export_composed_bid_docx(row.blueprint_json or {}, out_file)

    record_audit(
        db,
        action='composer_download',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"composer:{row.id}",
    )
    return FileResponse(
        str(out_file),
        media_type=_DOCX_MIME,
        filename=f"{row.title}_投标文件初稿骨架.docx",
    )


@router.delete('/drafts/{draft_id}', response_model=OkResponse)
def delete_composed_draft(
    draft_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OkResponse:
    """删除编制草稿并级联清理磁盘上传文件与导出文档。"""
    tenant_id = int(current_user.tenant_id)
    row = _get_tenant_draft_or_404(db, draft_id, tenant_id)

    upload_dir = (_COMPOSER_UPLOADS_ROOT / str(tenant_id) / str(row.id)).resolve()
    if upload_dir.is_dir() and upload_dir.is_relative_to(_COMPOSER_UPLOADS_ROOT):
        shutil.rmtree(upload_dir, ignore_errors=True)
    out_file = (_COMPOSER_OUTPUT_ROOT / f"{tenant_id}_{row.id}_投标文件初稿骨架.docx").resolve()
    if out_file.is_file() and out_file.is_relative_to(_COMPOSER_OUTPUT_ROOT):
        out_file.unlink(missing_ok=True)

    db.delete(row)
    db.commit()
    record_audit(
        db,
        action='composer_delete',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"composer:{draft_id}",
    )
    return OkResponse(ok=True, message='标书编制草稿及生成文件已清理删除')
