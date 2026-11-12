# -*- coding: utf-8 -*-
"""B2 / E23 · 第 2 组路由：/api/dashboard（防废标体检总览，全部按 tenant_id 过滤聚合）。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.auth import AuthUser, get_current_user
from backend.database import get_db
from backend.models import BidCheckTask, BidFinding, BidRuleConfig, CompanyCertificate
from backend.schemas import (
    BidTaskOut,
    CountdownTaskItem,
    DashboardStatsOut,
    DefenseMatrixItem,
)

router = APIRouter(prefix='/api/dashboard', tags=['dashboard'])


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


@router.get('', response_model=DashboardStatsOut)
@router.get('/stats', response_model=DashboardStatsOut)
def get_dashboard_stats(
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DashboardStatsOut:
    """获取当前租户的防废标大盘统计数据（4 统计卡 + 六大防线矩阵 + 截标倒计时 + 近期任务）。"""
    tenant_id = int(current_user.tenant_id)
    tasks = (
        db.query(BidCheckTask)
        .filter_by(tenant_id=tenant_id)
        .order_by(BidCheckTask.id.desc())
        .all()
    )

    total_projects = len(tasks)
    pre_check_count = sum(1 for t in tasks if t.task_type == 'pre_check')
    radar_count = sum(1 for t in tasks if t.task_type == 'radar')
    delivered_count = sum(1 for t in tasks if t.status == 'delivered' and t.task_type == 'pre_check')

    task_ids = [t.id for t in tasks]
    findings = (
        db.query(BidFinding).filter(BidFinding.task_id.in_(task_ids)).all()
        if task_ids
        else []
    )

    veto_count = sum(1 for f in findings if f.severity == 'error' and f.feedback != 'rejected')
    warning_count = sum(1 for f in findings if f.severity == 'warning' and f.feedback != 'rejected')

    f1_task_extracted = sum(
        int((t.stats_json or {}).get('f1_extracted_count') or (t.stats_json or {}).get('total_requirements') or 0)
        for t in tasks
        if t.task_type == 'pre_check'
    )
    mod_counts = {'F1': f1_task_extracted, 'F2': 0, 'F3': 0, 'F4': 0, 'F5': delivered_count, 'F6': 0}
    for f in findings:
        if f.source_module in mod_counts:
            mod_counts[f.source_module] += 1

    defense_matrix = [
        DefenseMatrixItem(module='F1', title='废标红线与 ★/▲ 提取', hit_count=mod_counts['F1'], status='ok'),
        DefenseMatrixItem(module='F2', title='报价算术与大小写沙盒', hit_count=mod_counts['F2'], status='ok'),
        DefenseMatrixItem(module='F3', title='证照效期与串名扫描', hit_count=mod_counts['F3'], status='ok'),
        DefenseMatrixItem(module='F4', title='★条款响应与矛盾检测', hit_count=mod_counts['F4'], status='ok'),
        DefenseMatrixItem(module='F5', title='标准 Word 偏离表生成', hit_count=mod_counts['F5'], status='ok'),
        DefenseMatrixItem(module='F6', title='投前控标雷达扫描', hit_count=mod_counts['F6'], status='ok'),
    ]

    # 证照到期预警数
    today = date.today()
    certs = db.query(CompanyCertificate).filter_by(tenant_id=tenant_id).all()
    expiring_certs = sum(
        1 for c in certs if c.valid_until and (c.valid_until - today).days <= int(c.warn_days or 30)
    )

    # E23 §2.2：截标倒计时与重点任务看板
    # - F6 (radar) 仅读自身 config_json.open_date，不盲借 pre_check 的租户默认规则日期；
    # - 优先将有 open_date 的项目置顶，其次附最近 open_date=None 的项目，上限放宽至 8 条。
    default_rule = db.query(BidRuleConfig).filter_by(tenant_id=tenant_id, is_default=True).first()
    default_open_date = default_rule.open_date if default_rule else None

    findings_by_task: dict[int, list[BidFinding]] = {}
    for f in findings:
        findings_by_task.setdefault(f.task_id, []).append(f)

    dated_items: list[CountdownTaskItem] = []
    undated_items: list[CountdownTaskItem] = []

    for t in tasks:
        t_findings = findings_by_task.get(t.id, [])
        t_veto = sum(1 for f in t_findings if f.severity == 'error')
        t_warn = sum(1 for f in t_findings if f.severity == 'warning')
        cfg_open = str((t.config_json or {}).get('open_date') or '').strip() or None
        if t.task_type == 'radar':
            open_str = cfg_open
        else:
            open_str = cfg_open or (default_open_date.isoformat() if default_open_date else None)

        days_left = None
        if open_str:
            try:
                days_left = (date.fromisoformat(str(open_str)[:10]) - today).days
            except Exception:
                days_left = None
                open_str = None

        item = CountdownTaskItem(
            task_id=t.id,
            title=t.title,
            task_type=t.task_type,
            status=t.status,
            open_date=open_str,
            days_left=days_left,
            veto_count=t_veto,
            warning_count=t_warn,
        )
        if open_str is not None:
            dated_items.append(item)
        else:
            undated_items.append(item)

    countdown_alerts = (dated_items + undated_items)[:8]

    return DashboardStatsOut(
        tenant_id=tenant_id,
        total_projects=total_projects,
        pre_check_count=pre_check_count,
        radar_count=radar_count,
        veto_intercepted_count=veto_count,
        warning_count=warning_count,
        deviation_tables_generated=delivered_count,
        star_recall_rate=1.0,
        expiring_certs_count=expiring_certs,
        defense_matrix=defense_matrix,
        countdown_alerts=countdown_alerts,
        recent_tasks=[_task_to_out(t) for t in tasks[:10]],
    )