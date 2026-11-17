# -*- coding: utf-8 -*-
"""E36 · 第 12 组路由：/api/similarity（多标书交叉比对、历史记录、报告导出与删除）。

合规底线：仅提供客观多文件相似度、元数据、实体碰撞（敏感号前3后4脱敏）与规律报价排查，
绝不提供任何文本改写洗稿或元数据伪造功能。
"""
from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import shutil
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from backend.auth import AuthUser, get_current_user, record_audit
from backend.database import PROJECT_ROOT, get_db
from backend.models import SimilarityReport
from backend.schemas import (
    OkResponse,
    SimilarityBidderOut,
    SimilarityFindingOut,
    SimilarityPairOut,
    SimilarityReportDetailOut,
    SimilarityReportListOut,
    SimilarityReportSummaryOut,
)
from core.bid_similarity import analyze_bid_similarity, export_similarity_report_docx

router = APIRouter(prefix='/api/similarity', tags=['similarity'])

_ALLOWED_EXTS = {'.docx', '.pdf', '.txt'}
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100MB
_SIMILARITY_UPLOADS_ROOT = (PROJECT_ROOT / 'data' / 'uploads' / 'similarity').resolve()
_SIMILARITY_OUTPUT_ROOT = (PROJECT_ROOT / 'output' / 'similarity').resolve()
_DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'


def _save_similarity_upload(upload: UploadFile, target_dir: Path, role_prefix: str) -> tuple[str, Path, str]:
    orig_name = Path(upload.filename or f"{role_prefix}.docx").name
    ext = Path(orig_name).suffix.lower()
    if ext not in _ALLOWED_EXTS:
        raise HTTPException(
            status_code=422,
            detail={
                'code': 'UNSUPPORTED_FILE_TYPE',
                'message': f"不支持的文件格式: {ext}，仅支持 .docx/.pdf/.txt",
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
            detail={'code': 'FILE_TOO_LARGE', 'message': '上传文件超过 100MB 上限'},
        )

    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = (target_dir / f"{role_prefix}{ext}").resolve()
    if not target_file.is_relative_to(_SIMILARITY_UPLOADS_ROOT):
        raise HTTPException(
            status_code=422,
            detail={'code': 'PATH_TRAVERSAL', 'message': '非法文件存储路径'},
        )
    target_file.write_bytes(content)
    rel_path = target_file.relative_to(PROJECT_ROOT.resolve()).as_posix()
    return orig_name, target_file, rel_path


def _parse_labels_json(raw: Optional[str]) -> list[str]:
    if not raw or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x or '').strip() for x in parsed]
    except Exception:
        pass
    return []


def _get_tenant_report_or_404(db: Session, report_id: int, tenant_id: int) -> SimilarityReport:
    row = (
        db.query(SimilarityReport)
        .filter_by(id=report_id, tenant_id=tenant_id)
        .first()
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={'code': 'SIMILARITY_REPORT_NOT_FOUND', 'message': '比对报告不存在或无权访问'},
        )
    return row


def _to_summary_out(row: SimilarityReport) -> SimilarityReportSummaryOut:
    bidders = list(row.bidders_json or [])
    findings = list(row.findings_json or [])
    max_pct = float(Decimal(str(row.max_similarity_pct if row.max_similarity_pct is not None else '0.00')))
    risk_val = row.risk_level if row.risk_level in ('low', 'medium', 'high') else 'low'
    return SimilarityReportSummaryOut(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        title=row.title,
        tender_filename=row.tender_filename,
        bidder_count=len(bidders),
        max_similarity_pct=max_pct,
        risk_level=risk_val,
        finding_count=len(findings),
        created_at=row.created_at.isoformat() if row.created_at else None,
    )


def _to_detail_out(row: SimilarityReport) -> SimilarityReportDetailOut:
    summary = _to_summary_out(row)
    bidders = [SimilarityBidderOut(**b) for b in (row.bidders_json or [])]
    pairs = [SimilarityPairOut(**p) for p in (row.pair_matrix_json or [])]
    findings = [SimilarityFindingOut(**f) for f in (row.findings_json or [])]
    return SimilarityReportDetailOut(
        **summary.model_dump(),
        bidders=bidders,
        pair_matrix=pairs,
        findings=findings,
        export_url=f"/api/similarity/{row.id}/export",
    )


@router.post('/compare', response_model=SimilarityReportDetailOut)
def compare_bids(
    bid_files: list[UploadFile] = File(...),
    tender_file: Optional[UploadFile] = File(default=None),
    title: Optional[str] = Form(default=None),
    labels_json: Optional[str] = Form(default=None),
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SimilarityReportDetailOut:
    """上传 2~6 份参选投标文件（及可选招标文件底稿）执行五维交叉比对与落库。"""
    if not (2 <= len(bid_files) <= 6):
        raise HTTPException(
            status_code=422,
            detail={'code': 'INVALID_BID_FILE_COUNT', 'message': '请上传 2 至 6 份投标文件进行对比'},
        )

    tenant_id = int(current_user.tenant_id)
    user_id = current_user.get('id')
    custom_labels = _parse_labels_json(labels_json)

    clean_title = (title or '').strip()
    if not clean_title:
        stems = [Path(f.filename or f"标书{idx + 1}").stem for idx, f in enumerate(bid_files[:3])]
        clean_title = f"多标书交叉比对（{' vs '.join(stems)}{' 等' if len(bid_files) > 3 else ''}）"

    report = SimilarityReport(
        tenant_id=tenant_id,
        user_id=user_id,
        title=clean_title[:256],
        bidders_json=[],
        pair_matrix_json=[],
        findings_json=[],
        max_similarity_pct=Decimal('0.00'),
        risk_level='low',
    )
    db.add(report)
    db.flush()

    report_dir = (_SIMILARITY_UPLOADS_ROOT / str(tenant_id) / str(report.id)).resolve()
    try:
        tender_abs: Optional[Path] = None
        if tender_file is not None and (tender_file.filename or '').strip():
            t_name, t_abs, t_rel = _save_similarity_upload(tender_file, report_dir, 'tender')
            report.tender_filename = t_name
            report.tender_path = t_rel
            tender_abs = t_abs

        bid_specs: list[dict[str, Any]] = []
        for idx, bf in enumerate(bid_files):
            orig_fn, b_abs, b_rel = _save_similarity_upload(bf, report_dir, f"bid_{idx + 1}")
            lbl = (
                custom_labels[idx]
                if idx < len(custom_labels) and custom_labels[idx]
                else Path(orig_fn).stem
            )
            bid_specs.append({
                'label': lbl[:64],
                'filename': orig_fn,
                'path': b_abs,
                'rel_path': b_rel,
            })

        result = analyze_bid_similarity(bid_specs, tender_path=tender_abs)
        report.bidders_json = result['bidders']
        report.pair_matrix_json = result['pair_matrix']
        report.findings_json = result['findings']
        report.max_similarity_pct = Decimal(str(result['max_similarity_pct']))
        report.risk_level = result['risk_level']

        db.commit()
        db.refresh(report)
    except HTTPException:
        db.rollback()
        shutil.rmtree(report_dir, ignore_errors=True)
        raise
    except Exception as exc:
        db.rollback()
        shutil.rmtree(report_dir, ignore_errors=True)
        raise HTTPException(
            status_code=422,
            detail={'code': 'SIMILARITY_ANALYZE_FAILED', 'message': f"标书交叉比对解析失败：{exc}"},
        ) from exc

    record_audit(
        db,
        action='similarity_compare',
        tenant_id=tenant_id,
        user_id=user_id,
        target=f"similarity:{report.id}",
        detail_json={
            'title': report.title,
            'bidder_count': len(bid_specs),
            'has_tender_baseline': tender_abs is not None,
            'max_similarity_pct': str(report.max_similarity_pct),
            'risk_level': report.risk_level,
        },
    )
    return _to_detail_out(report)


@router.get('', response_model=SimilarityReportListOut)
def list_similarity_reports(
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SimilarityReportListOut:
    """列出当前租户的历史多标书交叉比对记录。"""
    tenant_id = int(current_user.tenant_id)
    rows = (
        db.query(SimilarityReport)
        .filter_by(tenant_id=tenant_id)
        .order_by(SimilarityReport.id.desc())
        .all()
    )
    return SimilarityReportListOut(
        total=len(rows),
        items=[_to_summary_out(r) for r in rows],
    )


@router.get('/{report_id}', response_model=SimilarityReportDetailOut)
def get_similarity_report(
    report_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SimilarityReportDetailOut:
    """查询指定交叉比对报告详情（严格租户隔离）。"""
    row = _get_tenant_report_or_404(db, report_id, int(current_user.tenant_id))
    return _to_detail_out(row)


@router.get('/{report_id}/export')
def export_similarity_report(
    report_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """导出可读《多标书交叉比对与相似度分析报告.docx》。"""
    tenant_id = int(current_user.tenant_id)
    row = _get_tenant_report_or_404(db, report_id, tenant_id)
    out_file = (_SIMILARITY_OUTPUT_ROOT / f"{tenant_id}_{row.id}_similarity_report.docx").resolve()
    if not out_file.is_relative_to(_SIMILARITY_OUTPUT_ROOT):
        raise HTTPException(status_code=422, detail={'code': 'PATH_TRAVERSAL', 'message': '非法导出路径'})

    export_similarity_report_docx(
        {
            'title': row.title,
            'tender_filename': row.tender_filename,
            'bidders': row.bidders_json or [],
            'pair_matrix': row.pair_matrix_json or [],
            'findings': row.findings_json or [],
            'max_similarity_pct': f"{Decimal(str(row.max_similarity_pct or '0.00')):.2f}",
            'risk_level': row.risk_level,
        },
        out_file,
    )
    record_audit(
        db,
        action='similarity_export',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"similarity:{row.id}",
    )
    return FileResponse(
        str(out_file),
        media_type=_DOCX_MIME,
        filename=f"{row.title}_交叉比对分析报告.docx",
    )


@router.delete('/{report_id}', response_model=OkResponse)
def delete_similarity_report(
    report_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OkResponse:
    """删除比对报告并级联清理磁盘上传文件与导出文档。"""
    tenant_id = int(current_user.tenant_id)
    row = _get_tenant_report_or_404(db, report_id, tenant_id)

    upload_dir = (_SIMILARITY_UPLOADS_ROOT / str(tenant_id) / str(row.id)).resolve()
    if upload_dir.is_dir() and upload_dir.is_relative_to(_SIMILARITY_UPLOADS_ROOT):
        shutil.rmtree(upload_dir, ignore_errors=True)
    out_file = (_SIMILARITY_OUTPUT_ROOT / f"{tenant_id}_{row.id}_similarity_report.docx").resolve()
    if out_file.is_file() and out_file.is_relative_to(_SIMILARITY_OUTPUT_ROOT):
        out_file.unlink(missing_ok=True)

    db.delete(row)
    db.commit()
    record_audit(
        db,
        action='similarity_delete',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"similarity:{report_id}",
    )
    return OkResponse(ok=True, message='交叉比对报告及上传文件已清理删除')
