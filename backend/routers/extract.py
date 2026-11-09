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
from backend.schemas import ExtractChecklistOut
from backend.worker import resolve_effective_llm_client
from core.tender_extractor import build_review_checklist
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
        client, llm_status = resolve_effective_llm_client(db, current_user.get('id'))
        if client is not None:
            try:
                res = extract_tender_with_llm(str(tmp_path), client=client)
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
                'total_extracted': len(marked_list) + len(invalid_list) + len(qual_list) + len(comp_list),
            },
        )

        return ExtractChecklistOut(
            filename=orig_name,
            source_type=str(res.get('source_type') or ext.lstrip('.')),
            pages=int(res.get('pages') or 1),
            chars=int(res.get('chars') or 0),
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
        )
    finally:
        tmp_path.unlink(missing_ok=True)
        for sfx in ('.ocr.txt', '.ocr.json', '.media.ocr.json'):
            Path(str(tmp_path) + sfx).unlink(missing_ok=True)