# -*- coding: utf-8 -*-
"""B2 · 第 5 组路由：/api/deviation（调用 F5 export_deviation_table 生成偏离表 docx 并提供下载）。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from backend.auth import AuthUser, get_current_user, record_audit
from backend.database import PROJECT_ROOT, get_db
from backend.models import BidCheckTask
from backend.schemas import DeviationExportOut
from backend.worker import resolve_effective_llm_client, resolve_project_path
from core.deviation_exporter import export_deviation_table
from core.tender_extractor import build_review_checklist

router = APIRouter(prefix='/api/deviation', tags=['deviation'])

_DEVIATION_OUT_DIR = (PROJECT_ROOT / 'output' / 'deviation').resolve()
_DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'


@router.post('/{task_id}', response_model=DeviationExportOut)
@router.get('/{task_id}', response_model=DeviationExportOut)
def generate_deviation_for_task(
    task_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DeviationExportOut:
    """基于已有任务调用 export_deviation_table 生成《技术与商务偏离表.docx》，落盘 output/deviation/。"""
    tenant_id = int(current_user.tenant_id)
    task = db.query(BidCheckTask).filter_by(id=task_id, tenant_id=tenant_id).first()
    if task is None:
        raise HTTPException(
            status_code=404,
            detail={'code': 'TASK_NOT_FOUND', 'message': '任务不存在或无权访问'},
        )

    tender_abs = resolve_project_path(task.tender_path)
    bid_abs = resolve_project_path(task.bid_path)
    if not tender_abs.is_file() or not bid_abs.is_file():
        raise HTTPException(
            status_code=422,
            detail={'code': 'SOURCE_FILES_MISSING', 'message': '任务的招投标源文件缺失，无法生成偏离表'},
        )

    _DEVIATION_OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_filename = f"{task.id}_偏离表.docx"
    out_abs = (_DEVIATION_OUT_DIR / out_filename).resolve()

    # 铁律 1：F1 抽取结果显式传入 requirements=，避免内部重复解析
    checklist = build_review_checklist(str(tender_abs))
    reqs = list(checklist.get('marked_clauses') or []) + list(checklist.get('invalid_bid_clauses') or [])
    client, _ = resolve_effective_llm_client(db, current_user.get('id'))

    res = export_deviation_table(
        tender_path=tender_abs,
        bid_path=bid_abs,
        output_path=out_abs,
        requirements=reqs,
        client=client,
    )

    rel_out = out_abs.relative_to(PROJECT_ROOT.resolve()).as_posix()
    record_audit(
        db,
        action='export_deviation_table',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"task:{task.id}",
        detail_json={'total_rows': res['total_rows'], 'output': rel_out},
    )

    return DeviationExportOut(
        task_id=task.id,
        download_url=f"/api/files/{rel_out}",
        output_filename=out_filename,
        total_rows=int(res.get('total_rows', 0)),
        no_deviation_count=int(res.get('no_deviation_count', 0)),
        negative_deviation_count=int(res.get('negative_deviation_count', 0)),
        missing_count=int(res.get('missing_count', 0)),
        contradiction_count=int(res.get('contradiction_count', 0)),
        rows=list(res.get('rows') or []),
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
        generate_deviation_for_task(task_id=task_id, current_user=current_user, db=db)
    return FileResponse(
        str(out_abs),
        media_type=_DOCX_MIME,
        filename=f"{task.title}_技术与商务偏离表.docx",
    )