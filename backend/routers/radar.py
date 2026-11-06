# -*- coding: utf-8 -*-
"""B2 · 第 6 组路由：/api/radar（上传招标文件 -> F6 scan_rigging_radar 控标雷达扫描并落库）。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from backend.auth import AuthUser, get_current_user, record_audit
from backend.database import get_db
from backend.models import BidCheckTask, BidFinding
from backend.routers.checks import _save_validated_upload
from backend.schemas import RadarConclusionOut, RadarScanOut, SuspiciousParamOut
from backend.worker import resolve_effective_llm_client, resolve_project_path
from core.rigging_radar import scan_rigging_radar

router = APIRouter(prefix='/api/radar', tags=['radar'])


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@router.post('', response_model=RadarScanOut)
def run_radar_scan(
    tender_file: UploadFile = File(...),
    title: Optional[str] = Form(default=None),
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RadarScanOut:
    """上传甲方招标文件，执行 F6 投前控标雷达扫描并落库为 task_type='radar' 的任务与 findings。"""
    tenant_id = int(current_user.tenant_id)
    user_id = current_user.get('id')
    task_title = (title or '').strip() or f"{Path(tender_file.filename or '招标文件').stem}-控标雷达评估"

    task = BidCheckTask(
        tenant_id=tenant_id,
        user_id=user_id,
        task_type='radar',
        title=task_title,
        status='checking',
        progress=50,
        stage_detail='正在扫描排他性与倾向性控标参数',
    )
    db.add(task)
    db.flush()

    t_orig, t_rel = _save_validated_upload(tender_file, tenant_id, task.id, 'tender')
    task.tender_filename = t_orig
    task.tender_path = t_rel
    db.commit()

    tender_abs = resolve_project_path(t_rel)
    client, _ = resolve_effective_llm_client(db, user_id)
    try:
        radar_res = scan_rigging_radar(tender_abs, client=client)
    except Exception:
        radar_res = scan_rigging_radar(tender_abs, client=None)

    suspicious_list = list(radar_res.get('suspicious_params') or [])
    conclusion = radar_res.get('conclusion') or {
        'recommendation': '值得投',
        'summary': '未检出明显疑似排他性参数（仅供参考）。',
    }
    llm_status = str(radar_res.get('llm_status') or 'skipped')

    for item in suspicious_list:
        bf = BidFinding(
            task_id=task.id,
            source_module='F6',
            code='SUSPECTED_EXCLUSIVE_PARAM',
            severity='warning',
            category=str(item.get('exclusive_type') or '疑似排他参数'),
            clause_no='招标技术/资格条款',
            quote=str(item.get('quote') or ''),
            quote_offset=item.get('offset'),
            page=item.get('page'),
            quote_verified=bool(item.get('quote_verified', True)),
            message=str(item.get('reason') or '疑似排他性控标参数'),
            suggestion='建议投前向招标代理机构发出书面澄清/提疑函确认是否接受等效产品或资质',
            consequence='deduction',
            detail_json={
                'exclusive_type': item.get('exclusive_type'),
                'confidence': item.get('confidence'),
                'llm_status': item.get('llm_status', llm_status),
            },
        )
        db.add(bf)

    task.status = 'delivered'
    task.progress = 100
    task.stage_detail = f"控标扫描完成：{conclusion.get('recommendation')}（检出 {len(suspicious_list)} 处疑似项）"
    task.stats_json = {
        'suspicious_count': len(suspicious_list),
        'recommendation': conclusion.get('recommendation'),
        'summary': conclusion.get('summary'),
        'llm_status': llm_status,
    }
    task.delivered_at = _utc_now()
    db.commit()

    record_audit(
        db,
        action='radar_scan',
        tenant_id=tenant_id,
        user_id=user_id,
        target=f"task:{task.id}",
        detail_json=task.stats_json,
    )

    return RadarScanOut(
        task_id=task.id,
        title=task.title,
        llm_status=llm_status,
        suspicious_params=[
            SuspiciousParamOut(
                quote=str(p.get('quote') or ''),
                offset=int(p.get('offset') or 0),
                page=p.get('page'),
                exclusive_type=str(p.get('exclusive_type') or '参数定向'),
                confidence=float(p.get('confidence') or 0.8),
                reason=str(p.get('reason') or ''),
                quote_verified=bool(p.get('quote_verified', True)),
                llm_status=str(p.get('llm_status') or llm_status),
            )
            for p in suspicious_list
        ],
        conclusion=RadarConclusionOut(
            recommendation=str(conclusion.get('recommendation') or '值得投'),
            summary=str(conclusion.get('summary') or ''),
        ),
    )