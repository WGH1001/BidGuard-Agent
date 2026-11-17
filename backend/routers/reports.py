# -*- coding: utf-8 -*-
"""B2 / E16 · 第 9 组路由：/api/reports（交付中心单任务打包 ZIP、终核签字单独立下载、多任务内容自选批量打包 ZIP；以及管理员审计日志查询）。

严格遵循铁律 6（redline 回写适配）：
- BidFinding.severity 的 error/warning/info 必须先映射为 redline 识别的 high/medium/low；
- 逐条包装为支持点属性访问的 _RedlineFindingAdapter 对象传入 inject_word_comments / inject_pdf_highlights，
  严禁裸传 dict。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
from typing import Optional
import zipfile

from docx import Document
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.auth import AuthUser, get_current_user, record_audit, require_admin
from backend.database import PROJECT_ROOT, get_db
from backend.models import AuditLog, BidCheckTask, BidFinding
from backend.schemas import (
    AuditLogItemOut,
    AuditLogListOut,
    CleanBidAppliedItemOut,
    CleanBidExportOut,
    CleanBidExportRequest,
    CleanBidPendingItemOut,
)
from backend.worker import build_and_cache_deviation_artifacts, resolve_effective_llm_client, resolve_project_path
from core.redline import export_clean_revised_docx, inject_pdf_highlights, inject_word_comments
from core.tender_extractor import build_review_checklist

router = APIRouter(prefix='/api/reports', tags=['reports'])

_REDLINE_DIR = (PROJECT_ROOT / 'output' / 'redline').resolve()
_DEVIATION_DIR = (PROJECT_ROOT / 'output' / 'deviation').resolve()
_DELIVER_DIR = (PROJECT_ROOT / 'output' / 'deliver').resolve()
_DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'

_VALID_INCLUDE_KEYS = frozenset({'original', 'redline', 'deviation', 'signoff'})
_DEFAULT_SINGLE_ZIP_INCLUDE = frozenset({'redline', 'deviation', 'signoff'})

_SEVERITY_TO_REDLINE = {
    'error': 'high',
    'warning': 'medium',
    'info': 'low',
    'high': 'high',
    'medium': 'medium',
    'low': 'low',
}


class BatchDeliverZipRequest(BaseModel):
    """E16 §2.1：批量打包请求体。"""
    task_ids: list[int] = Field(..., min_length=1)
    include: list[str] = Field(
        default_factory=lambda: ['original', 'redline', 'deviation', 'signoff']
    )


class BatchFailedItemOut(BaseModel):
    """E16 §2.1：批量打包中被跳过的任务及原因。"""
    task_id: int
    reason: str


class BatchDeliverZipOut(BaseModel):
    """E16 §2.1：批量打包响应体（含下载 URL、成功任务列表与诚实降级 failed 列表）。"""
    download_url: str
    zip_path: str
    output_filename: str
    succeeded_task_ids: list[int]
    failed: list[BatchFailedItemOut]


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


def _sanitize_folder_title(title: Optional[str]) -> str:
    """清理任务标题中的非法路徑字符，供批量大包内子目录 {task_id}_{任务标题}/ 命名。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', '_', (title or '').strip())
    return cleaned[:80] or '防废标体检任务'


def _build_task_deliver_zip(
    task_id: int,
    include: set[str],
    db: Optional[Session] = None,
    tenant_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> Path:
    """E16 §2.1：按指定 include 集合（{'original','redline','deviation','signoff'}）构建单任务交付包 ZIP 并返回其 Path。"""
    own_session = False
    if db is None:
        from backend.database import SessionLocal
        db = SessionLocal()
        own_session = True

    try:
        q = db.query(BidCheckTask).filter_by(id=int(task_id))
        if tenant_id is not None:
            q = q.filter_by(tenant_id=int(tenant_id))
        task = q.first()
        if task is None:
            raise FileNotFoundError(f"任务 #{task_id} 不存在或无权访问")

        tender_abs = resolve_project_path(task.tender_path)
        bid_abs = resolve_project_path(task.bid_path)
        if not bid_abs.is_file() or ('deviation' in include and not tender_abs.is_file()):
            raise FileNotFoundError(f"任务 #{task_id} 招投标源文件缺失")
        if not tender_abs.is_file():
            raise FileNotFoundError(f"任务 #{task_id} 招标源文件缺失")

        findings = db.query(BidFinding).filter_by(task_id=task.id).order_by(BidFinding.id.asc()).all()
        adapted = _adapt_findings_for_redline(findings)

        _REDLINE_DIR.mkdir(parents=True, exist_ok=True)
        _DEVIATION_DIR.mkdir(parents=True, exist_ok=True)
        _DELIVER_DIR.mkdir(parents=True, exist_ok=True)

        bid_ext = bid_abs.suffix.lower() or '.docx'
        entries: list[tuple[str, bytes]] = []

        # 0. 投标书原件（original）
        if 'original' in include:
            entries.append((f"0_投标书原件{bid_ext}", bid_abs.read_bytes()))

        # 1. 原位标红批注件（redline）
        if 'redline' in include:
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
            entries.append((f"1_投标书原位标红批注件{bid_ext}", redline_bytes))

        # 2. 技术与商务偏离表.docx（deviation）
        if 'deviation' in include:
            dev_path = (_DEVIATION_DIR / f"{task.id}_偏离表.docx").resolve()
            if not dev_path.is_file():
                checklist = build_review_checklist(str(tender_abs))
                reqs = list(checklist.get('marked_clauses') or []) + list(checklist.get('invalid_bid_clauses') or [])
                client, _ = resolve_effective_llm_client(db, user_id if user_id is not None else task.user_id)
                build_and_cache_deviation_artifacts(
                    task_id=task.id,
                    tender_abs=tender_abs,
                    bid_abs=bid_abs,
                    requirements=reqs,
                    client=client,
                )
            entries.append(("2_技术与商务偏离表.docx", dev_path.read_bytes()))

        # 3. 人工终核签字确认单.docx（signoff）
        if 'signoff' in include:
            signoff_bytes = _build_signoff_sheet_bytes(task, findings)
            entries.append(("3_封标前人工终核签字确认单.docx", signoff_bytes))

        zip_path = (_DELIVER_DIR / f"{task.id}_交付包.zip").resolve()
        with zipfile.ZipFile(str(zip_path), mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
            for arc_name, raw_bytes in entries:
                zf.writestr(arc_name, raw_bytes)
        return zip_path
    finally:
        if own_session:
            db.close()


@router.get('/{task_id}/signoff')
def download_signoff_docx(
    task_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """E16 §2.1：独立生成并下载《3_封标前人工终核签字确认单.docx》（与 ZIP 内命名一致；任务或源文件缺失返回 404）。"""
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
            status_code=404,
            detail={'code': 'SOURCE_FILES_MISSING', 'message': '任务的招投标源文件不存在，无法生成终核签字单'},
        )

    findings = db.query(BidFinding).filter_by(task_id=task.id).order_by(BidFinding.id.asc()).all()
    signoff_bytes = _build_signoff_sheet_bytes(task, findings)

    _DELIVER_DIR.mkdir(parents=True, exist_ok=True)
    out_path = (_DELIVER_DIR / f"{task.id}_3_封标前人工终核签字确认单.docx").resolve()
    out_path.write_bytes(signoff_bytes)

    record_audit(
        db,
        action='export_signoff_docx',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"task:{task.id}",
        detail_json={'filename': '3_封标前人工终核签字确认单.docx'},
    )

    return FileResponse(
        str(out_path),
        media_type=_DOCX_MIME,
        filename='3_封标前人工终核签字确认单.docx',
    )


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


@router.post('/{task_id}/clean-bid', response_model=CleanBidExportOut)
def export_clean_bid_document(
    task_id: int,
    body: CleanBidExportRequest = Body(default_factory=CleanBidExportRequest),
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CleanBidExportOut:
    """E37 §3.3：基于任务已采纳整改项（feedback=='accepted'）生成 Clean 无批注《修订定稿版投标书.docx》。

    合规拦截：仅支持 .docx 原件；.pdf 原件明确返回 422 PDF_CLEAN_EXPORT_UNSUPPORTED。
    """
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

    if bid_abs.suffix.lower() != '.docx':
        raise HTTPException(
            status_code=422,
            detail={
                'code': 'PDF_CLEAN_EXPORT_UNSUPPORTED',
                'message': 'PDF 原件暂不支持安全无损修订导出，请在 Word 原件（.docx）中修改后再核验，以免破坏版面与电子签章。',
            },
        )

    q = db.query(BidFinding).filter_by(task_id=task.id, feedback='accepted')
    if body.finding_ids is not None:
        allowed_ids = [int(x) for x in body.finding_ids]
        q = q.filter(BidFinding.id.in_(allowed_ids)) if allowed_ids else q.filter(BidFinding.id == -1)
    accepted_rows = q.order_by(BidFinding.id.asc()).all()

    _DELIVER_DIR.mkdir(parents=True, exist_ok=True)
    out_filename = f"{task.id}_修订定稿版投标书.docx"
    out_abs = (_DELIVER_DIR / f"{tenant_id}_{out_filename}").resolve()
    if not out_abs.is_relative_to(_DELIVER_DIR):
        raise HTTPException(status_code=422, detail={'code': 'PATH_TRAVERSAL', 'message': '非法导出路径'})

    result = export_clean_revised_docx(
        bid_abs,
        accepted_rows,
        out_abs,
        replacements=body.replacements,
    )
    rel_out = out_abs.relative_to(PROJECT_ROOT.resolve()).as_posix()

    record_audit(
        db,
        action='export_clean_bid',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"task:{task.id}",
        detail_json={
            'accepted_count': len(accepted_rows),
            'applied_count': result['applied_count'],
            'manual_pending_count': result['manual_pending_count'],
            'output': out_abs.name,
        },
    )

    return CleanBidExportOut(
        task_id=task.id,
        output_filename=out_filename,
        output_path=rel_out,
        download_url=f"/api/reports/{task.id}/clean-bid/download",
        applied_count=int(result['applied_count']),
        manual_pending_count=int(result['manual_pending_count']),
        applied_items=[CleanBidAppliedItemOut(**it) for it in result['applied_items']],
        manual_pending_items=[CleanBidPendingItemOut(**it) for it in result['manual_pending_items']],
    )


@router.get('/{task_id}/clean-bid/download')
def download_clean_bid_document(
    task_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """下载最近生成的《修订定稿版投标书.docx》。"""
    tenant_id = int(current_user.tenant_id)
    task = db.query(BidCheckTask).filter_by(id=task_id, tenant_id=tenant_id).first()
    if task is None:
        raise HTTPException(
            status_code=404,
            detail={'code': 'TASK_NOT_FOUND', 'message': '任务不存在或无权访问'},
        )
    out_filename = f"{task.id}_修订定稿版投标书.docx"
    out_abs = (_DELIVER_DIR / f"{tenant_id}_{out_filename}").resolve()
    if not out_abs.is_relative_to(_DELIVER_DIR) or not out_abs.is_file():
        raise HTTPException(
            status_code=404,
            detail={'code': 'CLEAN_BID_NOT_GENERATED', 'message': '尚未生成修订定稿文件，请先执行导出计算'},
        )
    return FileResponse(str(out_abs), media_type=_DOCX_MIME, filename=out_filename)


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

    zip_path = _build_task_deliver_zip(
        task_id=task.id,
        include=set(_DEFAULT_SINGLE_ZIP_INCLUDE),
        db=db,
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
    )

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


@router.post('/batch-zip', response_model=BatchDeliverZipOut)
def export_batch_deliver_zip(
    body: BatchDeliverZipRequest,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BatchDeliverZipOut:
    """E16 §2.1：多任务批量打包与内容自选接口（落盘 output/deliver/batch_{utc_ts}_交付包.zip，诚实列出 failed 清单）。"""
    tenant_id = int(current_user.tenant_id)
    include_set = {str(k).strip().lower() for k in (body.include or [])} & _VALID_INCLUDE_KEYS
    if not include_set:
        raise HTTPException(
            status_code=422,
            detail={'code': 'INVALID_INCLUDE_OPTIONS', 'message': '请至少勾选一项要打包的交付物内容'},
        )

    _DELIVER_DIR.mkdir(parents=True, exist_ok=True)
    utc_ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')
    batch_filename = f"batch_{utc_ts}_交付包.zip"
    batch_zip_path = (_DELIVER_DIR / batch_filename).resolve()

    succeeded_ids: list[int] = []
    failed_items: list[BatchFailedItemOut] = []
    sub_zips_to_pack: list[tuple[BidCheckTask, Path]] = []

    seen_ids: set[int] = set()
    for raw_tid in body.task_ids:
        tid = int(raw_tid)
        if tid in seen_ids:
            continue
        seen_ids.add(tid)

        task = db.query(BidCheckTask).filter_by(id=tid, tenant_id=tenant_id).first()
        if task is None:
            failed_items.append(BatchFailedItemOut(task_id=tid, reason='任务不存在或无权访问'))
            continue

        if task.status in ('created', 'extracting', 'checking', 'grading'):
            failed_items.append(
                BatchFailedItemOut(task_id=tid, reason=f"任务尚未完成核验（当前状态：{task.status}）")
            )
            continue

        tender_abs = resolve_project_path(task.tender_path)
        bid_abs = resolve_project_path(task.bid_path)
        if not tender_abs.is_file() or not bid_abs.is_file():
            failed_items.append(BatchFailedItemOut(task_id=tid, reason='招投标源文件缺失'))
            continue

        try:
            sub_zip = _build_task_deliver_zip(
                task_id=task.id,
                include=include_set,
                db=db,
                tenant_id=tenant_id,
                user_id=current_user.get('id'),
            )
            sub_zips_to_pack.append((task, sub_zip))
            succeeded_ids.append(task.id)
        except Exception as exc:
            failed_items.append(BatchFailedItemOut(task_id=tid, reason=f"打包异常：{exc}"))

    if not succeeded_ids:
        raise HTTPException(
            status_code=422,
            detail={
                'code': 'BATCH_ALL_FAILED',
                'message': '所选任务均无法生成交付包',
                'failed': [f.model_dump() for f in failed_items],
            },
        )

    with zipfile.ZipFile(str(batch_zip_path), mode='w', compression=zipfile.ZIP_DEFLATED) as master_zf:
        for task, sub_zip_path in sub_zips_to_pack:
            folder_prefix = f"{task.id}_{_sanitize_folder_title(task.title)}"
            with zipfile.ZipFile(str(sub_zip_path), mode='r') as sub_zf:
                for info in sub_zf.infolist():
                    master_zf.writestr(f"{folder_prefix}/{info.filename}", sub_zf.read(info.filename))

    rel_zip = batch_zip_path.relative_to(PROJECT_ROOT.resolve()).as_posix()
    record_audit(
        db,
        action='export_batch_zip',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"batch:{','.join(map(str, succeeded_ids))}",
        detail_json={
            'task_ids': body.task_ids,
            'succeeded_task_ids': succeeded_ids,
            'failed': [f.model_dump() for f in failed_items],
            'include': sorted(include_set),
            'zip': batch_filename,
        },
    )

    return BatchDeliverZipOut(
        download_url=f"/api/files/{rel_zip}",
        zip_path=rel_zip,
        output_filename=batch_filename,
        succeeded_task_ids=succeeded_ids,
        failed=failed_items,
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


_CSV_FORMULA_PREFIXES = ('=', '+', '-', '@')
_MAX_AUDIT_EXPORT_ROWS = 10000


def _sanitize_csv_cell(val: object) -> str:
    """E25 §2.5：对以 = / + / - / @ 开头的单元格前置单引号转义，防御 Excel CSV 公式注入攻击。"""
    if val is None:
        return ''
    s = str(val)
    if s and s[0] in _CSV_FORMULA_PREFIXES:
        return f"'{s}"
    return s


@router.get('/audit-logs/export')
@router.get('/audit/export', include_in_schema=False)
def export_audit_logs_csv(
    action: Optional[str] = Query(default=None),
    current_user: AuthUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """E25 §2.5：管理员导出本租户安全审计留痕日志为 UTF-8 BOM CSV 文件（上限 10000 条，防公式注入转义）。"""
    tenant_id = int(current_user.tenant_id)
    q = db.query(AuditLog).filter_by(tenant_id=tenant_id)
    clean_action = action.strip() if (action and action.strip()) else None
    if clean_action:
        q = q.filter_by(action=clean_action)

    rows = q.order_by(AuditLog.id.desc()).limit(_MAX_AUDIT_EXPORT_ROWS).all()

    buf = io.StringIO()
    buf.write('\ufeff')  # UTF-8 BOM，确保 Excel 直接双击打开中文不乱码
    writer = csv.writer(buf, lineterminator='\n')
    writer.writerow(['ID', '审计动作', '操作对象', '详情快照', 'IP', '记录时间(UTC)'])

    for r in rows:
        detail_str = (
            json.dumps(r.detail_json, ensure_ascii=False)
            if r.detail_json is not None
            else ''
        )
        created_str = r.created_at.strftime('%Y-%m-%d %H:%M:%S') if r.created_at else ''
        writer.writerow([
            r.id,
            _sanitize_csv_cell(r.action),
            _sanitize_csv_cell(r.target or ''),
            _sanitize_csv_cell(detail_str),
            _sanitize_csv_cell(r.ip or ''),
            _sanitize_csv_cell(created_str),
        ])

    record_audit(
        db,
        action='export_audit_logs',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target='audit_logs.csv',
        detail_json={'action_filter': clean_action, 'exported_count': len(rows)},
    )

    date_tag = datetime.now(timezone.utc).strftime('%Y%m%d')
    csv_bytes = buf.getvalue().encode('utf-8')
    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="audit_logs_{date_tag}.csv"'},
    )
