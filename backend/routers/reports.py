# -*- coding: utf-8 -*-
"""B2 · 第 9 组路由：/api/reports（交付中心单任务打包 ZIP：标红原件 + 偏离表 + 终核签字单；以及管理员审计日志查询）。

严格遵循铁律 6（redline 回写适配）：
- BidFinding.severity 的 error/warning/info 必须先映射为 redline 识别的 high/medium/low；
- 逐条包装为支持点属性访问的 _RedlineFindingAdapter 对象传入 inject_word_comments / inject_pdf_highlights，
  严禁裸传 dict。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import io
from pathlib import Path
from typing import Optional
import zipfile

from docx import Document
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from backend.auth import AuthUser, get_current_user, record_audit, require_admin
from backend.database import PROJECT_ROOT, get_db
from backend.models import AuditLog, BidCheckTask, BidFinding
from backend.schemas import AuditLogItemOut, AuditLogListOut
from backend.worker import resolve_effective_llm_client, resolve_project_path
from core.deviation_exporter import export_deviation_table
from core.redline import inject_pdf_highlights, inject_word_comments
from core.tender_extractor import build_review_checklist

router = APIRouter(prefix='/api/reports', tags=['reports'])

_REDLINE_DIR = (PROJECT_ROOT / 'output' / 'redline').resolve()
_DEVIATION_DIR = (PROJECT_ROOT / 'output' / 'deviation').resolve()
_DELIVER_DIR = (PROJECT_ROOT / 'output' / 'deliver').resolve()

_SEVERITY_TO_REDLINE = {
    'error': 'high',
    'warning': 'medium',
    'info': 'low',
    'high': 'high',
    'medium': 'medium',
    'low': 'low',
}


@dataclass
class _RedlineFindingAdapter:
    """铁律 6：适配 core/redline.py 点属性访问契约，并将 error/warning/info 映射为 high/medium/low。"""
    severity: str
    rule_name: str
    message: str
    suggestion: str
    legal_basis: str
    clause_no: str
    quote: str


def _adapt_findings_for_redline(rows: list[BidFinding]) -> list[_RedlineFindingAdapter]:
    adapted: list[_RedlineFindingAdapter] = []
    for r in rows:
        if r.feedback == 'rejected':
            continue
        mapped_sev = _SEVERITY_TO_REDLINE.get((r.severity or 'error').lower(), 'high')
        target_quote = (r.bid_quote or r.quote or '').strip()
        adapted.append(
            _RedlineFindingAdapter(
                severity=mapped_sev,
                rule_name=r.rule_name,
                message=r.message or '',
                suggestion=r.suggestion or '请对照招标文件要求核实并修正该处内容',
                legal_basis=r.legal_basis,
                clause_no=r.clause_no or '全文',
                quote=target_quote,
            )
        )
    return adapted


def _build_signoff_sheet_bytes(task: BidCheckTask, findings: list[BidFinding]) -> bytes:
    """生成《封标前人工终核签字确认单.docx》字节流。"""
    doc = Document()
    doc.add_heading('封标前防废标体检 · 人工终核签字确认单', level=1)
    doc.add_paragraph(f"项目名称：{task.title}")
    doc.add_paragraph(f"招标文件：{task.tender_filename or ''} ｜ 投标文件：{task.bid_filename or ''}")
    doc.add_paragraph(f"导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    err_list = [f for f in findings if f.severity == 'error' and f.feedback != 'rejected']
    warn_list = [f for f in findings if f.severity == 'warning' and f.feedback != 'rejected']
    doc.add_paragraph(
        f"核查统计：共检出一票否决高危项 {len(err_list)} 处，预警项 {len(warn_list)} 处。"
    )

    tbl = doc.add_table(rows=1, cols=5)
    tbl.style = 'Table Grid'
    for idx, h in enumerate(('序号', '模块', '风险等级', '核查问题描述', '人工复核确认签字')):
        tbl.rows[0].cells[idx].text = h

    for seq, f in enumerate(err_list + warn_list, start=1):
        cells = tbl.add_row().cells
        cells[0].text = str(seq)
        cells[1].text = f.source_module
        cells[2].text = '一票否决' if f.severity == 'error' else '预警提示'
        cells[3].text = f.message or ''
        cells[4].text = '□ 已整改  签字：________'

    doc.add_paragraph(
        '\n免责与合规边界声明：本系统定位于封标前印前体检与风险提示工具，不承诺“保证不废标”。'
        '正式打印封标前请投标商务负责人逐项复核签字确认。'
    )
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


@router.get('/{task_id}/redline')
def download_redline_bid(
    task_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """生成并下载投标书原位标红批注副本（output/redline/{task_id}_标红原件.{docx|pdf}）。"""
    tenant_id = int(current_user.tenant_id)
    task = db.query(BidCheckTask).filter_by(id=task_id, tenant_id=tenant_id).first()
    if task is None:
        raise HTTPException(
            status_code=404,
            detail={'code': 'TASK_NOT_FOUND', 'message': '任务不存在或无权访问'},
        )

    bid_abs = resolve_project_path(task.bid_path)
    if not bid_abs.is_file():
        raise HTTPException(
            status_code=422,
            detail={'code': 'BID_FILE_MISSING', 'message': '投标文件原件不存在'},
        )

    ext = bid_abs.suffix.lower()
    if ext not in ('.docx', '.pdf'):
        raise HTTPException(
            status_code=422,
            detail={'code': 'REDLINE_FORMAT_UNSUPPORTED', 'message': '原位标红仅支持 .docx 与 .pdf'},
        )

    findings = db.query(BidFinding).filter_by(task_id=task.id).order_by(BidFinding.id.asc()).all()
    adapted = _adapt_findings_for_redline(findings)

    _REDLINE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = (_REDLINE_DIR / f"{task.id}_标红原件{ext}").resolve()

    if ext == '.docx':
        inject_word_comments(str(bid_abs), adapted, out_docx_path=str(out_path), author='BidGuard防废标体检员')
        media = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    else:
        inject_pdf_highlights(str(bid_abs), adapted, out_pdf_path=str(out_path), author='BidGuard防废标体检员')
        media = 'application/pdf'

    return FileResponse(str(out_path), media_type=media, filename=f"{task.id}_标红原件{ext}")


@router.get('/{task_id}/deliver.zip')
@router.post('/{task_id}/deliver-zip', include_in_schema=False)
@router.get('/{task_id}/zip', include_in_schema=False)
def export_task_deliver_zip(
    task_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """交付中心单任务一键打包下载（标红原件 + 偏离表.docx + 人工终核签字单.docx），落盘 output/deliver/{task_id}_交付包.zip。"""
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
            detail={'code': 'SOURCE_FILES_MISSING', 'message': '招投标源文件缺失，无法生成交付包'},
        )

    findings = db.query(BidFinding).filter_by(task_id=task.id).order_by(BidFinding.id.asc()).all()
    adapted = _adapt_findings_for_redline(findings)

    _REDLINE_DIR.mkdir(parents=True, exist_ok=True)
    _DEVIATION_DIR.mkdir(parents=True, exist_ok=True)
    _DELIVER_DIR.mkdir(parents=True, exist_ok=True)

    # 1. 原位标红原件
    bid_ext = bid_abs.suffix.lower()
    redline_path = (_REDLINE_DIR / f"{task.id}_标红原件{bid_ext}").resolve()
    if bid_ext == '.docx':
        redline_bytes = inject_word_comments(
            str(bid_abs), adapted, out_docx_path=str(redline_path), author='BidGuard防废标体检员'
        )
    elif bid_ext == '.pdf':
        redline_bytes = inject_pdf_highlights(
            str(bid_abs), adapted, out_pdf_path=str(redline_path), author='BidGuard防废标体检员'
        )
    else:
        redline_bytes = bid_abs.read_bytes()

    # 2. 技术与商务偏离表.docx
    dev_path = (_DEVIATION_DIR / f"{task.id}_偏离表.docx").resolve()
    if not dev_path.is_file():
        checklist = build_review_checklist(str(tender_abs))
        reqs = list(checklist.get('marked_clauses') or []) + list(checklist.get('invalid_bid_clauses') or [])
        client, _ = resolve_effective_llm_client(db, current_user.get('id'))
        export_deviation_table(tender_abs, bid_abs, dev_path, requirements=reqs, client=client)
    dev_bytes = dev_path.read_bytes()

    # 3. 人工终核签字确认单.docx
    signoff_bytes = _build_signoff_sheet_bytes(task, findings)

    # 4. 打包写入 output/deliver/{task_id}_交付包.zip
    zip_path = (_DELIVER_DIR / f"{task.id}_交付包.zip").resolve()
    with zipfile.ZipFile(str(zip_path), mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"1_投标书原位标红批注件{bid_ext}", redline_bytes)
        zf.writestr("2_技术与商务偏离表.docx", dev_bytes)
        zf.writestr("3_封标前人工终核签字确认单.docx", signoff_bytes)

    record_audit(
        db,
        action='export_deliver_zip',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"task:{task.id}",
        detail_json={'zip': zip_path.name},
    )

    return StreamingResponse(
        io.BytesIO(zip_path.read_bytes()),
        media_type='application/zip',
        headers={'Content-Disposition': f'attachment; filename="deliver_task_{task.id}.zip"'},
    )


@router.get('/audit-logs', response_model=AuditLogListOut)
@router.get('/audit', response_model=AuditLogListOut, include_in_schema=False)
def list_audit_logs(
    action: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: AuthUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> AuditLogListOut:
    """安全审计日志分页查询（仅 admin 角色可访问，本租户隔离）。"""
    tenant_id = int(current_user.tenant_id)
    q = db.query(AuditLog).filter_by(tenant_id=tenant_id)
    if action:
        q = q.filter_by(action=action.strip())
    total = q.count()
    rows = (
        q.order_by(AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return AuditLogListOut(
        total=total,
        items=[
            AuditLogItemOut(
                id=r.id,
                tenant_id=r.tenant_id,
                user_id=r.user_id,
                action=r.action,
                target=r.target,
                detail_json=r.detail_json,
                ip=r.ip,
                created_at=r.created_at.isoformat() if r.created_at else None,
            )
            for r in rows
        ],
    )