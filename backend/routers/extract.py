# -*- coding: utf-8 -*-
"""B2 · 第 4 组路由：/api/extract（上传招标文件 -> F1 四张清单结构化提取）。

严格遵循铁律 5：
- 当处于无模型降级模式（client is None）时，显式直调 build_review_checklist()，
  严禁调用 extract_tender_with_llm / extract_scoring_with_llm 触发隐式联网加载。
"""
from __future__ import annotations

from pathlib import Path
import tempfile

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from backend.auth import AuthUser, get_current_user, record_audit
from backend.database import get_db
from backend.models import CompanyCertificate
from backend.schemas import ExtractChecklistOut, PrivacyStatsOut
from backend.worker import resolve_effective_llm_client
from core.parser import parse_document
from core.privacy_sanitizer import scan_document_file
from core.qualification_scanner import match_required_certificates_with_ledger
from core.response_checker import build_unified_page_context
from core.tender_extractor import build_review_checklist, extract_required_certificates
from core.tender_llm import extract_scoring_with_llm, extract_tender_with_llm

router = APIRouter(prefix='/api/extract', tags=['extract'])

_ALLOWED_EXTS = {'.docx', '.pdf', '.txt'}


@router.post('', response_model=ExtractChecklistOut)
def extract_tender_checklists(
    file: UploadFile = File(...),
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ExtractChecklistOut:
    """上传甲方招标文件，返回 F1 四张结构化清单（usage_note / table_marker 原样带出并标注 kind）。"""
    orig_name = Path(file.filename or 'tender.docx').name
    ext = Path(orig_name).suffix.lower()
    if ext not in _ALLOWED_EXTS:
        raise HTTPException(
            status_code=422,
            detail={'code': 'UNSUPPORTED_FILE_TYPE', 'message': f"不支持的文件格式: {ext}"},
        )

    content = file.file.read()
    if not content:
        raise HTTPException(
            status_code=422,
            detail={'code': 'EMPTY_FILE', 'message': '上传的招标文件内容为空'},
        )

    fd, tmp_str = tempfile.mkstemp(suffix=ext)
    import os
    os.close(fd)
    tmp_path = Path(tmp_str)
    tmp_path.write_bytes(content)

    try:
        p_stats = scan_document_file(tmp_path)
        client, llm_status = resolve_effective_llm_client(db, current_user.get('id'))
        if client is not None:
            try:
                res = extract_tender_with_llm(
                    str(tmp_path),
                    client=client,
                    open_date_fallback=True,
                )
                score_res = extract_scoring_with_llm(str(tmp_path), client=client)
                scoring_prose = list(score_res.get('scoring_prose') or [])
                llm_structured = list(res.get('llm_structured') or [])
            except Exception:
                res = build_review_checklist(str(tmp_path))
                scoring_prose = []
                llm_structured = []
                llm_status = 'skipped'
        else:
            # 铁律 5：0 Token 降级模式显式直调确定性层 build_review_checklist
            res = build_review_checklist(str(tmp_path))
            scoring_prose = []
            llm_structured = []
            llm_status = 'skipped'

        marked_list = list(res.get('marked_clauses') or [])
        invalid_list = list(res.get('invalid_bid_clauses') or [])
        qual_list = list(res.get('qualification_hits') or [])
        comp_list = list(res.get('compliance_hits') or [])
        req_certs_raw = list(res.get('required_certificates') or [])
        if scoring_prose:
            parsed_prose_doc = parse_document(str(tmp_path))
            prose_certs = extract_required_certificates(
                parsed_prose_doc.text,
                scoring_prose=scoring_prose,
                page_ctx=build_unified_page_context(str(tmp_path)),
            )
            seen_cert_keys = {
                (c.get('cert_name'), c.get('requirement_level'))
                for c in req_certs_raw
            }
            for pc in prose_certs:
                k = (pc.get('cert_name'), pc.get('requirement_level'))
                if k not in seen_cert_keys:
                    seen_cert_keys.add(k)
                    req_certs_raw.append(pc)
            req_certs_raw.sort(
                key=lambda x: (
                    0 if x.get('requirement_level') == 'mandatory' else 1,
                    x.get('offset') if x.get('offset') is not None else 10**9,
                    str(x.get('cert_name') or ''),
                )
            )

        ledger_rows = (
            db.query(CompanyCertificate)
            .filter(CompanyCertificate.tenant_id == current_user.tenant_id)
            .order_by(CompanyCertificate.id.asc())
            .all()
        )
        matched_certs, cert_summary = match_required_certificates_with_ledger(
            req_certs_raw,
            ledger_rows,
            reference_date=res.get('open_date'),
        )

        record_audit(
            db,
            action='extract_tender',
            tenant_id=current_user.tenant_id,
            user_id=current_user.get('id'),
            target=orig_name,
            detail_json={
                'llm_status': llm_status,
                'marked_count': len(marked_list),
                'invalid_count': len(invalid_list),
                'required_certs_count': len(matched_certs),
                'total_extracted': len(marked_list) + len(invalid_list) + len(qual_list) + len(comp_list),
            },
        )

        return ExtractChecklistOut(
            filename=orig_name,
            source_type=str(res.get('source_type') or ext.lstrip('.')),
            pages=int(res.get('pages') or 1),
            chars=int(res.get('chars') or 0),
            open_date=res.get('open_date'),
            open_date_source=res.get('open_date_source') or ('tender_extracted' if res.get('open_date') else None),
            open_date_quote=res.get('open_date_quote'),
            llm_status=llm_status,
            chapters=list(res.get('chapters') or []),
            marked_clauses=marked_list,
            invalid_bid_clauses=invalid_list,
            qualification_hits=qual_list,
            compliance_hits=comp_list,
            eval_chapter=res.get('eval_chapter'),
            scoring_tables=list(res.get('scoring_tables') or []),
            scoring_prose=scoring_prose,
            llm_structured=llm_structured,
            required_certificates=matched_certs,
            cert_ledger_summary=cert_summary,
            privacy_stats=PrivacyStatsOut(**p_stats),
        )
    finally:
        tmp_path.unlink(missing_ok=True)
        for sfx in ('.ocr.txt', '.ocr.json', '.media.ocr.json'):
            Path(str(tmp_path) + sfx).unlink(missing_ok=True)
