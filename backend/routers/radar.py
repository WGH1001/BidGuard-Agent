# -*- coding: utf-8 -*-
"""B2 / E23 / E25 · 第 6 组路由：/api/radar（上传招标文件 -> F6 scan_rigging_radar 控标雷达扫描落库与单条重扫 rescan）。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from backend.auth import AuthUser, get_current_user, record_audit
from backend.database import get_db
from backend.models import BidCheckTask, BidFinding
from backend.routers.checks import (
    _get_tenant_task_or_404,
    _save_validated_upload,
    _task_to_out,
    delete_check_task,
)
from backend.schemas import (
    BidTaskListOut,
    OkResponse,
    RadarConclusionOut,
    RadarScanOut,
    SuspiciousParamOut,
)
from backend.worker import PipelineCancelled, get_worker, resolve_effective_llm_client, resolve_project_path
from core.parser import parse_document
from core.rigging_radar import scan_rigging_radar
from core.tender_extractor import extract_open_date_with_evidence
from core.tender_llm import extract_open_date_fallback_with_llm

router = APIRouter(prefix='/api/radar', tags=['radar'])


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _execute_radar_scan(
    db: Session,
    task: BidCheckTask,
    tender_abs: Path,
    tenant_id: int,
    user_id: Optional[int],
    audit_action: str,
) -> RadarScanOut:
    """E25 §2.2.1 + E30：执行或重新执行 F6 控标雷达扫描，接入 _cancel_flags 与异常落 failed 消除僵尸 checking。"""
    worker = get_worker()
    cancel_ev = worker.register_task_execution(task.id)
    try:
        worker.check_cancelled(task.id, cancel_ev)
        client, _ = resolve_effective_llm_client(db, user_id)

        detected_open_date: Optional[str] = None
        open_date_source: Optional[str] = None
        open_date_quote: Optional[str] = None
        try:
            tender_doc = parse_document(str(tender_abs))
            tender_text = tender_doc.text or ''
            date_ev = extract_open_date_with_evidence(tender_text)
            if date_ev:
                detected_open_date = str(date_ev['open_date'])
                open_date_source = 'tender_extracted'
                open_date_quote = str(date_ev.get('quote') or '')[:240] or None
            elif client is not None and tender_text:
                llm_ev = extract_open_date_fallback_with_llm(tender_text, client)
                if llm_ev:
                    detected_open_date = str(llm_ev['open_date'])
                    open_date_source = 'tender_llm'
                    open_date_quote = str(llm_ev.get('quote') or '')[:240] or None
        except PipelineCancelled:
            raise
        except Exception:
            worker.check_cancelled(task.id, cancel_ev)
            detected_open_date = None
            open_date_source = None
            open_date_quote = None

        cfg_out = dict(task.config_json or {})
        if detected_open_date:
            cfg_out['open_date'] = detected_open_date
            if open_date_source:
                cfg_out['open_date_source'] = open_date_source
            if open_date_quote:
                cfg_out['open_date_quote'] = open_date_quote
        else:
            cfg_out.pop('open_date', None)
            cfg_out.pop('open_date_source', None)
            cfg_out.pop('open_date_quote', None)
        task.config_json = cfg_out

        def _on_radar_progress(_done: int, _total: int) -> None:
            worker.check_cancelled(task.id, cancel_ev)

        worker.check_cancelled(task.id, cancel_ev)
        try:
            radar_res = scan_rigging_radar(tender_abs, client=client, progress_cb=_on_radar_progress)
        except PipelineCancelled:
            raise
        except Exception:
            worker.check_cancelled(task.id, cancel_ev)
            radar_res = scan_rigging_radar(tender_abs, client=None, progress_cb=_on_radar_progress)

        worker.check_cancelled(task.id, cancel_ev)
        suspicious_list = list(radar_res.get('suspicious_params') or [])
        conclusion = radar_res.get('conclusion') or {
            'recommendation': '值得投',
            'summary': '未检出明显疑似排他性参数（仅供参考）。',
        }
        llm_status = str(radar_res.get('llm_status') or 'skipped')

        # 清理该任务旧的 F6 findings 后重新写入
        db.query(BidFinding).filter_by(task_id=task.id, source_module='F6').delete(synchronize_session=False)

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

        now_utc = _utc_now()
        task.status = 'delivered'
        task.progress = 100
        task.error_msg = None
        task.stage_detail = f"控标扫描完成：{conclusion.get('recommendation')}（检出 {len(suspicious_list)} 处疑似项）"
        task.stats_json = {
            'suspicious_count': len(suspicious_list),
            'recommendation': conclusion.get('recommendation'),
            'summary': conclusion.get('summary'),
            'llm_status': llm_status,
        }
        task.delivered_at = now_utc
        task.updated_at = now_utc
        db.commit()

        record_audit(
            db,
            action=audit_action,
            tenant_id=tenant_id,
            user_id=user_id,
            target=f"task:{task.id}",
            detail_json=task.stats_json,
        )

        return RadarScanOut(
            open_date=detected_open_date,
            open_date_source=open_date_source,
            open_date_quote=open_date_quote,
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
    except PipelineCancelled:
        db.rollback()
        now_utc = _utc_now()
        cur_task = db.query(BidCheckTask).filter_by(id=task.id).first()
        if cur_task is not None:
            cur_task.status = 'failed'
            cur_task.stage_detail = '用户中断'
            cur_task.error_msg = '用户中断'
            cur_task.finished_at = now_utc
            cur_task.updated_at = now_utc
            cfg = dict(cur_task.config_json or {})
            cfg['cancelled_by_user'] = True
            cur_task.config_json = cfg
            db.commit()
        raise HTTPException(
            status_code=409,
            detail={'code': 'TASK_CANCELLED', 'message': '控标雷达扫描已被用户中断'},
        )
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        now_utc = _utc_now()
        err_msg = f"{type(exc).__name__}: {exc}"
        cur_task = db.query(BidCheckTask).filter_by(id=task.id).first()
        if cur_task is not None:
            cur_task.status = 'failed'
            cur_task.stage_detail = f"控标扫描异常中断：{err_msg}"
            cur_task.error_msg = err_msg
            cur_task.finished_at = now_utc
            cur_task.updated_at = now_utc
            db.commit()
        raise
    finally:
        worker.unregister_task_execution(task.id, cancel_ev)


@router.get('', response_model=BidTaskListOut)
def list_radar_tasks(
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BidTaskListOut:
    """列出当前租户下的 F6 控标雷达扫描历史任务。"""
    tenant_id = int(current_user.tenant_id)
    rows = (
        db.query(BidCheckTask)
        .filter_by(tenant_id=tenant_id, task_type='radar')
        .order_by(BidCheckTask.id.desc())
        .all()
    )
    return BidTaskListOut(items=[_task_to_out(t) for t in rows], total=len(rows))


@router.get('/{task_id}', response_model=RadarScanOut)
def get_radar_scan_detail(
    task_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RadarScanOut:
    """按 task_id 回放历史 F6 控标雷达扫描结果（从 BidCheckTask + BidFinding 还原 RadarScanOut）。"""
    tenant_id = int(current_user.tenant_id)
    task = _get_tenant_task_or_404(db, task_id, tenant_id)
    if task.task_type != 'radar':
        raise HTTPException(
            status_code=404,
            detail={'code': 'TASK_NOT_FOUND', 'message': '该任务不是控标雷达扫描记录'},
        )

    stats = task.stats_json or {}
    llm_status = str(stats.get('llm_status') or 'skipped')
    recommendation = str(stats.get('recommendation') or '值得投')
    summary = str(stats.get('summary') or '未检出明显疑似排他性参数（仅供参考）。')

    findings = (
        db.query(BidFinding)
        .filter_by(task_id=task.id, source_module='F6')
        .order_by(BidFinding.id.asc())
        .all()
    )

    suspicious_params: list[SuspiciousParamOut] = []
    for f in findings:
        detail = f.detail_json if isinstance(f.detail_json, dict) else {}
        suspicious_params.append(
            SuspiciousParamOut(
                quote=str(f.quote or ''),
                offset=int(f.quote_offset or 0),
                page=f.page,
                exclusive_type=str(detail.get('exclusive_type') or f.category or '参数定向'),
                confidence=float(detail.get('confidence') if detail.get('confidence') is not None else 0.8),
                reason=str(f.message or ''),
                quote_verified=bool(f.quote_verified),
                llm_status=str(detail.get('llm_status') or llm_status),
            )
        )

    cfg = task.config_json or {}
    return RadarScanOut(
        open_date=cfg.get('open_date'),
        open_date_source=cfg.get('open_date_source'),
        open_date_quote=cfg.get('open_date_quote'),
        task_id=task.id,
        title=task.title,
        llm_status=llm_status,
        suspicious_params=suspicious_params,
        conclusion=RadarConclusionOut(
            recommendation=recommendation,
            summary=summary,
        ),
    )


@router.post('/{task_id}/rescan', response_model=RadarScanOut)
def rescan_radar_task(
    task_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RadarScanOut:
    """E25 §2.2.1：对已上传的 F6 控标雷达记录重新执行扫描与开标日期提取（源文件缺失返回 422）。"""
    tenant_id = int(current_user.tenant_id)
    user_id = current_user.get('id')
    task = _get_tenant_task_or_404(db, task_id, tenant_id)
    if task.task_type != 'radar':
        raise HTTPException(
            status_code=404,
            detail={'code': 'TASK_NOT_FOUND', 'message': '该任务不是控标雷达扫描记录'},
        )

    tender_abs = resolve_project_path(task.tender_path)
    if not tender_abs.is_file():
        raise HTTPException(
            status_code=422,
            detail={'code': 'SOURCE_FILES_MISSING', 'message': '招标文件原件缺失，无法重新扫描'},
        )

    task.status = 'checking'
    task.progress = 50
    task.error_msg = None
    task.stage_detail = '正在重新扫描排他性与倾向性控标参数'
    task.updated_at = _utc_now()
    db.commit()

    return _execute_radar_scan(
        db=db,
        task=task,
        tender_abs=tender_abs,
        tenant_id=tenant_id,
        user_id=user_id,
        audit_action='radar_rescan',
    )


@router.delete('/{task_id}', response_model=OkResponse)
def delete_radar_task(
    task_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OkResponse:
    """删除本租户下指定的 F6 控标雷达历史记录（复用 checks 级联删除逻辑）。"""
    return delete_check_task(task_id=task_id, current_user=current_user, db=db)


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
    return _execute_radar_scan(
        db=db,
        task=task,
        tender_abs=tender_abs,
        tenant_id=tenant_id,
        user_id=user_id,
        audit_action='radar_scan',
    )
