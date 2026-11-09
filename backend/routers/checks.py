# -*- coding: utf-8 -*-
"""B2 · 第 3 组路由：/api/checks（双书上传、任务 CRUD、流水线触发、SSE 流、反馈、NEED_MANUAL 人工回填）。"""
from __future__ import annotations

import json
from pathlib import Path
import queue
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from backend.auth import AuthUser, get_current_user, record_audit
from backend.database import PROJECT_ROOT, get_db
from backend.models import BidCheckTask, BidFinding
from backend.schemas import (
    BidFindingOut,
    BidTaskDetailOut,
    BidTaskListOut,
    BidTaskOut,
    FindingFeedbackRequest,
    FindingListOut,
    OkResponse,
    PriceManualBackfillOut,
    PriceManualBackfillRequest,
    RunTaskTriggerOut,
)
from backend.worker import get_worker
from core.price_sandbox import PriceSandbox

router = APIRouter(prefix='/api/checks', tags=['checks'])

_ALLOWED_EXTS = {'.docx', '.pdf', '.txt'}
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100MB
_UPLOADS_ROOT = (PROJECT_ROOT / 'data' / 'uploads').resolve()


def _save_validated_upload(upload: UploadFile, tenant_id: int, task_id: int, role_prefix: str) -> tuple[str, str]:
    """按 INTERFACE.md §3.4 校验扩展名、大小与路径穿越，保存为固定文件名并返回 (原始文件名, 数据库相对路径)。"""
    orig_name = Path(upload.filename or f"{role_prefix}.docx").name
    ext = Path(orig_name).suffix.lower()
    if ext not in _ALLOWED_EXTS:
        raise HTTPException(
            status_code=422,
            detail={'code': 'UNSUPPORTED_FILE_TYPE', 'message': f"不支持的文件类型: {ext}，仅支持 docx/pdf/txt"},
        )

    content = upload.file.read()
    if len(content) == 0:
        raise HTTPException(
            status_code=422,
            detail={'code': 'EMPTY_FILE', 'message': f"上传的文件 {orig_name} 内容为空"},
        )
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=422,
            detail={'code': 'FILE_TOO_LARGE', 'message': '上传文件超过 100MB 上限'},
        )

    target_dir = (_UPLOADS_ROOT / str(tenant_id) / str(task_id)).resolve()
    if not target_dir.is_relative_to(_UPLOADS_ROOT):
        raise HTTPException(
            status_code=422,
            detail={'code': 'PATH_TRAVERSAL', 'message': '检测到非法路径穿越'},
        )
    target_dir.mkdir(parents=True, exist_ok=True)

    fixed_file = (target_dir / f"{role_prefix}{ext}").resolve()
    if not fixed_file.is_relative_to(_UPLOADS_ROOT):
        raise HTTPException(
            status_code=422,
            detail={'code': 'PATH_TRAVERSAL', 'message': '目标文件路径越界'},
        )
    fixed_file.write_bytes(content)
    rel_path = fixed_file.relative_to(PROJECT_ROOT.resolve()).as_posix()
    return orig_name, rel_path


def _task_to_out(t: BidCheckTask) -> BidTaskOut:
    return BidTaskOut(
        id=t.id,
        tenant_id=t.tenant_id,
        user_id=t.user_id,
        task_type=t.task_type,
        title=t.title,
        status=t.status,
        progress=t.progress,
        stage_detail=t.stage_detail,
        tender_filename=t.tender_filename,
        bid_filename=t.bid_filename,
        tender_url=f"/api/files/{t.tender_path}" if t.tender_path else None,
        bid_url=f"/api/files/{t.bid_path}" if t.bid_path else None,
        error_msg=t.error_msg,
        stats_json=t.stats_json,
        price_manual_json=t.price_manual_json,
        config_json=t.config_json,
        llm_usage_json=t.llm_usage_json,
        delivered_at=t.delivered_at.isoformat() if t.delivered_at else None,
        created_at=t.created_at.isoformat() if t.created_at else None,
        updated_at=t.updated_at.isoformat() if t.updated_at else None,
    )


def _finding_to_out(f: BidFinding) -> BidFindingOut:
    return BidFindingOut(
        id=f.id,
        task_id=f.task_id,
        source_module=f.source_module,
        code=f.code,
        severity=f.severity,
        category=f.category,
        clause_no=f.clause_no,
        quote=f.quote,
        bid_quote=f.bid_quote,
        quote_offset=f.quote_offset,
        quote_verified=f.quote_verified,
        page=f.page,
        message=f.message,
        suggestion=f.suggestion,
        consequence=f.consequence,
        feedback=f.feedback,
        feedback_note=f.feedback_note,
        detail_json=f.detail_json,
        created_at=f.created_at.isoformat() if f.created_at else None,
    )


def _get_tenant_task_or_404(db: Session, task_id: int, tenant_id: int) -> BidCheckTask:
    task = db.query(BidCheckTask).filter_by(id=task_id, tenant_id=tenant_id).first()
    if task is None:
        raise HTTPException(
            status_code=404,
            detail={'code': 'TASK_NOT_FOUND', 'message': '任务不存在或无权访问'},
        )
    return task


@router.post('', response_model=BidTaskOut)
def create_check_task(
    tender_file: UploadFile = File(...),
    bid_file: UploadFile = File(...),
    title: Optional[str] = Form(default=None),
    open_date: Optional[str] = Form(default=None),
    buyer_name: Optional[str] = Form(default=None),
    auto_run: bool = Form(default=False),
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BidTaskOut:
    """上传招投标双书并创建体检任务（默认 status='created'；传 auto_run=True 时自动启动流水线）。"""
    tenant_id = int(current_user.tenant_id)
    user_id = current_user.get('id')
    task_title = (title or '').strip() or Path(bid_file.filename or '封标防废标体检任务').stem

    cfg_snap = {}
    if open_date:
        cfg_snap['open_date'] = open_date.strip()
    if buyer_name:
        cfg_snap['buyer_name'] = buyer_name.strip()

    task = BidCheckTask(
        tenant_id=tenant_id,
        user_id=user_id,
        task_type='pre_check',
        title=task_title,
        status='created',
        progress=0,
        stage_detail='双书已上传，待启动核验',
        config_json=cfg_snap,
    )
    db.add(task)
    db.flush()

    t_orig, t_rel = _save_validated_upload(tender_file, tenant_id, task.id, 'tender')
    b_orig, b_rel = _save_validated_upload(bid_file, tenant_id, task.id, 'bid')
    task.tender_filename = t_orig
    task.bid_filename = b_orig
    task.tender_path = t_rel
    task.bid_path = b_rel
    db.commit()
    db.refresh(task)

    record_audit(
        db,
        action='create_check_task',
        tenant_id=tenant_id,
        user_id=user_id,
        target=f"task:{task.id}",
        detail_json={'title': task.title, 'tender': t_orig, 'bid': b_orig},
    )

    if auto_run:
        get_worker().submit_check_task(task.id)

    return _task_to_out(task)


@router.get('', response_model=BidTaskListOut)
def list_check_tasks(
    status: Optional[str] = Query(default=None),
    task_type: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BidTaskListOut:
    """分页查询本租户下的任务列表。"""
    tenant_id = int(current_user.tenant_id)
    q = db.query(BidCheckTask).filter_by(tenant_id=tenant_id)
    if status:
        q = q.filter_by(status=status.strip())
    if task_type:
        q = q.filter_by(task_type=task_type.strip())
    total = q.count()
    rows = (
        q.order_by(BidCheckTask.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return BidTaskListOut(total=total, items=[_task_to_out(r) for r in rows])


@router.get('/{task_id}', response_model=BidTaskDetailOut)
def get_check_task_detail(
    task_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BidTaskDetailOut:
    """获取单条任务详情与全部检出 findings（跨租户返回 404）。"""
    tenant_id = int(current_user.tenant_id)
    task = _get_tenant_task_or_404(db, task_id, tenant_id)
    findings = (
        db.query(BidFinding)
        .filter_by(task_id=task.id)
        .order_by(BidFinding.id.asc())
        .all()
    )
    return BidTaskDetailOut(
        task=_task_to_out(task),
        findings=[_finding_to_out(f) for f in findings],
    )


@router.post('/{task_id}/run', response_model=RunTaskTriggerOut)
def trigger_check_pipeline(
    task_id: int,
    sync: bool = Query(default=False),
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RunTaskTriggerOut:
    """触发后台核验流水线（支持后台异步入队与 sync=True 同步完成）。"""
    tenant_id = int(current_user.tenant_id)
    task = _get_tenant_task_or_404(db, task_id, tenant_id)

    worker = get_worker()
    worker.submit_check_task(task.id, sync=sync)
    db.expire_all()
    task = _get_tenant_task_or_404(db, task_id, tenant_id)

    record_audit(
        db,
        action='run_check_task',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"task:{task.id}",
    )
    return RunTaskTriggerOut(
        task_id=task.id,
        status=task.status,
        message='防废标核验流水线已启动',
    )


@router.get('/{task_id}/stream')
def stream_check_progress(
    task_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """SSE 实时进度流（严格按 INTERFACE.md §3.3 推送 event: progress / done / error）。"""
    tenant_id = int(current_user.tenant_id)
    task = _get_tenant_task_or_404(db, task_id, tenant_id)
    init_status = task.status
    init_progress = task.progress
    init_detail = task.stage_detail or ''
    init_stats = task.stats_json or {}
    init_err = task.error_msg or ''

    # 连接池防耗尽（生产实锤：SSE 流存活整段任务期，yield 依赖的 db 会话要等响应结束才释放，
    # 每条 SSE 流白占 1 个池连接；3 条流水线 + 前端多页签并发即打满 QueuePool）。
    # 生成器只用 broadcaster，不碰 db，这里读完初始态立即归还连接。
    db.close()

    worker = get_worker()

    def _sse_gen():
        q = worker.broadcaster.subscribe(task_id)
        try:
            # 1. 若任务在连接前已处于终态，直接推送终态事件并闭合
            if init_status == 'delivered':
                payload = json.dumps(
                    {'task_id': task_id, 'status': 'delivered', 'stats': init_stats},
                    ensure_ascii=False,
                )
                yield f"event: done\ndata: {payload}\n\n"
                return
            if init_status == 'failed':
                payload = json.dumps(
                    {'task_id': task_id, 'status': 'failed', 'error_msg': init_err},
                    ensure_ascii=False,
                )
                yield f"event: error\ndata: {payload}\n\n"
                return

            # 2. 推送当前进度帧
            first_payload = json.dumps(
                {
                    'task_id': task_id,
                    'status': init_status,
                    'progress': init_progress,
                    'stage_detail': init_detail,
                },
                ensure_ascii=False,
            )
            yield f"event: progress\ndata: {first_payload}\n\n"

            # 3. 持续监听广播总线或轮询 DB 终态
            ticks = 0
            while ticks < 100:
                try:
                    ev_name, ev_data = q.get(timeout=0.2)
                    data_str = json.dumps(ev_data, ensure_ascii=False)
                    yield f"event: {ev_name}\ndata: {data_str}\n\n"
                    if ev_name in ('done', 'error'):
                        return
                except queue.Empty:
                    latest = worker.broadcaster.latest(task_id)
                    if latest is not None and latest[0] in ('done', 'error'):
                        ev_name, ev_data = latest
                        yield f"event: {ev_name}\ndata: {json.dumps(ev_data, ensure_ascii=False)}\n\n"
                        return
                    ticks += 1
        finally:
            worker.broadcaster.unsubscribe(task_id, q)

    return StreamingResponse(
        _sse_gen(),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        },
    )


@router.get('/{task_id}/findings', response_model=FindingListOut)
def list_task_findings(
    task_id: int,
    source_module: Optional[str] = Query(default=None),
    severity: Optional[str] = Query(default=None),
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FindingListOut:
    """查询指定任务的检出风险明细列表。"""
    tenant_id = int(current_user.tenant_id)
    _get_tenant_task_or_404(db, task_id, tenant_id)

    q = db.query(BidFinding).filter_by(task_id=task_id)
    if source_module:
        q = q.filter_by(source_module=source_module.strip())
    if severity:
        q = q.filter_by(severity=severity.strip())
    rows = q.order_by(BidFinding.id.asc()).all()
    return FindingListOut(total=len(rows), items=[_finding_to_out(r) for r in rows])


@router.post('/{task_id}/findings/{finding_id}/feedback', response_model=BidFindingOut)
def update_finding_feedback(
    task_id: int,
    finding_id: int,
    body: FindingFeedbackRequest,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BidFindingOut:
    """更新单条 finding 的采纳/驳回状态（pending / accepted / rejected）。"""
    tenant_id = int(current_user.tenant_id)
    _get_tenant_task_or_404(db, task_id, tenant_id)

    finding = db.query(BidFinding).filter_by(id=finding_id, task_id=task_id).first()
    if finding is None:
        raise HTTPException(
            status_code=404,
            detail={'code': 'FINDING_NOT_FOUND', 'message': '未找到指定的风险项'},
        )

    finding.feedback = body.feedback
    finding.feedback_note = body.feedback_note
    db.commit()
    db.refresh(finding)

    record_audit(
        db,
        action='finding_feedback',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"finding:{finding.id}",
        detail_json={'task_id': task_id, 'feedback': body.feedback},
    )
    return _finding_to_out(finding)


@router.post('/{task_id}/price-manual', response_model=PriceManualBackfillOut)
def backfill_manual_price(
    task_id: int,
    body: PriceManualBackfillRequest,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PriceManualBackfillOut:
    """NEED_MANUAL 报价表人工框选回填接口：写入 price_manual_json 并调用 PriceSandbox 复算落库。"""
    tenant_id = int(current_user.tenant_id)
    task = _get_tenant_task_or_404(db, task_id, tenant_id)

    items_dicts = [
        {
            'name': item.name,
            'qty': str(item.qty),
            'unit_price': str(item.unit_price),
            'total_price': str(item.total_price),
        }
        for item in body.items
    ]
    manual_payload = {
        'items': items_dicts,
        'table_total': str(body.table_total) if body.table_total is not None else None,
        'bid_total': str(body.bid_total) if body.bid_total is not None else None,
        'bid_total_upper': body.bid_total_upper,
        'price_cap': str(body.price_cap) if body.price_cap is not None else (task.stats_json or {}).get('price_cap'),
    }
    task.price_manual_json = manual_payload

    sandbox = PriceSandbox(
        items=items_dicts,
        table_total=manual_payload['table_total'],
        bid_total=manual_payload['bid_total'],
        bid_total_upper=manual_payload['bid_total_upper'],
        price_cap=manual_payload['price_cap'],
    )
    price_findings = sandbox.run()

    # 替换该任务原有的 F2 findings
    db.query(BidFinding).filter_by(task_id=task.id, source_module='F2').delete()
    inserted: list[BidFinding] = []
    for pf in price_findings:
        bf = BidFinding(
            task_id=task.id,
            source_module='F2',
            code=str(pf.get('code') or 'PRICE_ERROR'),
            severity=str(pf.get('severity') or 'error'),
            category='报价算术核验（人工回填）',
            clause_no='开标一览表/分项报价表',
            quote=str(pf.get('actual') or ''),
            quote_verified=True,
            message=str(pf.get('message') or ''),
            suggestion='请根据算术沙盒复算结果修正报价表数值或大写金额',
            consequence='veto',
            detail_json=pf,
        )
        db.add(bf)
        inserted.append(bf)

    stats = dict(task.stats_json or {})
    stats['price_status'] = 'OK'
    stats['bid_total'] = manual_payload['bid_total']
    task.stats_json = stats
    if task.status == 'need_manual':
        task.status = 'delivered'
        task.progress = 100
    db.commit()
    for bf in inserted:
        db.refresh(bf)

    record_audit(
        db,
        action='price_manual_backfill',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"task:{task.id}",
        detail_json={'items_count': len(items_dicts), 'findings_count': len(inserted)},
    )
    return PriceManualBackfillOut(
        task_id=task.id,
        status=task.status,
        price_findings_count=len(inserted),
        findings=[_finding_to_out(x) for x in inserted],
    )


@router.delete('/{task_id}', response_model=OkResponse)
def delete_check_task(
    task_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OkResponse:
    """删除本租户下的指定任务（级联清理 findings 与 qa_sessions）。"""
    tenant_id = int(current_user.tenant_id)
    task = _get_tenant_task_or_404(db, task_id, tenant_id)
    db.delete(task)
    db.commit()

    record_audit(
        db,
        action='delete_check_task',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"task:{task_id}",
    )
    return OkResponse(ok=True, message=f"任务 #{task_id} 已删除")