# -*- coding: utf-8 -*-
"""B2 / E25 · 第 5 组路由：/api/deviation（调用 F5 export_deviation_table 生成偏离表 docx 并提供下载，拦截在途核验任务 409）。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from backend.auth import AuthUser, get_current_user, record_audit
from backend.database import get_db
from backend.models import BidCheckTask
from backend.schemas import DeviationExportOut
from backend.worker import (
    _DEVIATION_OUT_DIR,
    build_and_cache_deviation_artifacts,
    deviation_stats_sidecar_path,
    get_worker,
    resolve_effective_llm_client,
    resolve_project_path,
)
from core.tender_extractor import build_review_checklist

router = APIRouter(prefix='/api/deviation', tags=['deviation'])

_DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
_stats_sidecar_path = deviation_stats_sidecar_path


def _generate_deviation_and_cache(
    task: BidCheckTask,
    tender_abs: Path,
    bid_abs: Path,
    client,
    db: Session,
    tenant_id: int,
    user_id=None,
) -> DeviationExportOut:
    """全量生成偏离表：解析双书 → F4 对齐 → 写 docx + 统计快照 sidecar。"""
    # 铁律 1：F1 抽取结果显式传入 requirements=，避免内部重复解析
    checklist = build_review_checklist(str(tender_abs))
    reqs = list(checklist.get('marked_clauses') or []) + list(checklist.get('invalid_bid_clauses') or [])

    payload = build_and_cache_deviation_artifacts(
        task_id=task.id,
        tender_abs=tender_abs,
        bid_abs=bid_abs,
        requirements=reqs,
        client=client,
    )
    out = DeviationExportOut(**payload)

    record_audit(
        db,
        action='export_deviation_table',
        tenant_id=tenant_id,
        user_id=user_id,
        target=f"task:{task.id}",
        detail_json={'total_rows': out.total_rows, 'output': out.download_url},
    )
    return out


@router.post('/{task_id}', response_model=DeviationExportOut)
def regenerate_deviation_for_task(
    task_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DeviationExportOut:
    """强制重新生成偏离表（用户显式点击"重新生成"时调用）。"""
    return _deviation_entry(task_id, current_user, db, force_rebuild=True)


@router.get('/{task_id}', response_model=DeviationExportOut)
def get_deviation_for_task(
    task_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DeviationExportOut:
    """获取偏离表：产物与统计快照已存在时直接复用（秒回，零重算）；不存在时才首次生成。"""
    return _deviation_entry(task_id, current_user, db, force_rebuild=False)


def _deviation_entry(
    task_id: int,
    current_user: AuthUser,
    db: Session,
    *,
    force_rebuild: bool,
) -> DeviationExportOut:
    tenant_id = int(current_user.tenant_id)
    task = db.query(BidCheckTask).filter_by(id=task_id, tenant_id=tenant_id).first()
    if task is None:
        raise HTTPException(
            status_code=404,
            detail={'code': 'TASK_NOT_FOUND', 'message': '任务不存在或无权访问'},
        )

    # E25 §2.1.2：若任务正在核验中，直接返回 HTTP 409 CHECK_TASK_RUNNING，防止半途抢跑或读脏数据
    worker = get_worker()
    if task.status in ('extracting', 'checking', 'grading') or task.id in worker._in_flight:
        raise HTTPException(
            status_code=409,
            detail={
                'code': 'CHECK_TASK_RUNNING',
                'message': '该体检任务正在核验中，请等待完成后再查看或生成偏离表',
            },
        )

    tender_abs = resolve_project_path(task.tender_path)
    bid_abs = resolve_project_path(task.bid_path)
    if not tender_abs.is_file() or not bid_abs.is_file():
        raise HTTPException(
            status_code=422,
            detail={'code': 'SOURCE_FILES_MISSING', 'message': '任务的招投标源文件缺失，无法生成偏离表'},
        )

    # 复用通道：非强制重建且产物 docx 与统计快照均在 → 直接返回快照（零重算）
    out_abs = (_DEVIATION_OUT_DIR / f"{task.id}_偏离表.docx").resolve()
    sidecar = _stats_sidecar_path(task.id)
    if not force_rebuild and out_abs.is_file() and sidecar.is_file():
        try:
            import json
            from core.tender_extractor import classify_invalid_clause_role
            cached = json.loads(sidecar.read_text(encoding='utf-8'))
            cached_rows = cached.get('rows') or []
            has_stale_non_req = any(
                classify_invalid_clause_role(
                    str(r.get('tender_requirement') or ''),
                    str(r.get('clause_no') or ''),
                ) in ('eval_rule', 'submission_rule')
                for r in cached_rows
            )
            if not has_stale_non_req:
                return DeviationExportOut(**cached)
        except Exception:
            pass  # 快照损坏则走重新生成兜底

    client, _ = resolve_effective_llm_client(db, current_user.get('id'))
    return _generate_deviation_and_cache(
        task, tender_abs, bid_abs, client, db, tenant_id,
        user_id=current_user.get('id'),
    )


@router.get('/{task_id}/download')
def download_deviation_docx(
    task_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """直接下载已生成的《{task_id}_偏离表.docx》。"""
    tenant_id = int(current_user.tenant_id)
    task = db.query(BidCheckTask).filter_by(id=task_id, tenant_id=tenant_id).first()
    if task is None:
        raise HTTPException(
            status_code=404,
            detail={'code': 'TASK_NOT_FOUND', 'message': '任务不存在或无权访问'},
        )
    out_abs = (_DEVIATION_OUT_DIR / f"{task.id}_偏离表.docx").resolve()
    if not out_abs.is_file():
        _deviation_entry(task_id=task_id, current_user=current_user, db=db, force_rebuild=True)
    return FileResponse(
        str(out_abs),
        media_type=_DOCX_MIME,
        filename=f"{task.title}_技术与商务偏离表.docx",
    )
