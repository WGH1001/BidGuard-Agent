# -*- coding: utf-8 -*-
"""E30 · 可中断流水线与活性心跳卡 专项验收测试（0 Token 离线可跑）。

覆盖 6 组核心验收要求：
1. test_e30_cancel_running_pipeline_and_cleanup：
   - 流水线中途取消后清理半途 BidFinding 与偏离表产物、标 failed('用户中断')、置 cancelled_by_user=True、广播 error 帧、释放取消标志；
   - 重跑同任务时自动清除 cancelled_by_user 与旧 stats_json，顺利 delivered；
2. test_e30_f1_loop_cancel_not_swallowed_by_fallback：
   - 专项覆盖 worker.py F1 fallback（except Exception -> build_review_checklist）不吞 _PipelineCancelled，严禁降级成半残 0-Token 报告；
   - 同步验证 extract_tender_with_llm / run_response_check / scan_rigging_radar 透传 _PipelineCancelled(BaseException)；
3. test_e30_cancel_api_404_409_audit_and_delete_message：
   - POST /api/checks/{id}/cancel 状态门控(409 TASK_NOT_RUNNING)、跨租户与不存在(404)、落库与 cancel_check_task 审计；
   - DELETE 拒删提示升级为 '运行中任务请先中断后再删除'，取消标志已触发时允许立即删除；
4. test_e30_startup_recover_stale_tasks：
   - 服务启动恢复 recover_stale_tasks 自动将 extracting/checking/grading 遗留任务（含 pre_check 与 radar）置 failed('服务重启中断') 并幂等；
5. test_e30_f1_heartbeat_progress_frames：
   - 设置 _f1_heartbeat_interval_sec=0.0，核验 SSE progress 帧推送 '已判定 X / Y 条' 与 updated_at；
   - 节流窗口开启时中间条款合并节流，末条强制刷出；
6. test_e30_radar_cancel_and_exception_no_zombie_checking：
   - POST /api/radar 与 /rescan 接入 _cancel_flags：取消时置 failed('用户中断') 返回 409 TASK_CANCELLED；
   - 意外异常时亦置 failed，绝不遗留 checking 僵尸任务导致无法删除。
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

from docx import Document
from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import backend.database as db_mod
import backend.routers.checks as checks_router_mod
import backend.routers.radar as radar_router_mod
import backend.worker as worker_mod
from backend.database import Base, PROJECT_ROOT, get_engine, get_session_factory
from backend.main import create_app, recover_stale_tasks
from backend.models import AuditLog, BidCheckTask, BidFinding, Tenant
from backend.worker import (
    BidTaskWorker,
    _PipelineCancelled,
    _cleanup_task_deviation_artifacts,
    deviation_stats_sidecar_path,
)
from core.response_checker import run_response_check
from core.rigging_radar import scan_rigging_radar
from core.tender_llm import extract_tender_with_llm


def _make_docx_file(path: Path, lines: list[str]) -> Path:
    doc = Document()
    for ln in lines:
        doc.add_paragraph(ln)
    doc.save(str(path))
    return path


def _make_docx_bytes(lines: list[str]) -> bytes:
    doc = Document()
    for ln in lines:
        doc.add_paragraph(ln)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ── 1. 运行中流水线取消、脏数据清理与可重跑恢复 ─────────────────────────────

def test_e30_cancel_running_pipeline_and_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    tender_docx = _make_docx_file(
        tmp_path / 'tender.docx',
        ['第一章 技术要求', '★1. 免费质保期不少于36个月。'],
    )
    bid_docx = _make_docx_file(
        tmp_path / 'bid.docx',
        ['第一章 投标响应', '★1. 免费质保期不少于36个月。（我方完全响应）'],
    )

    db_file = tmp_path / 'e30_pipeline_cancel.db'
    eng = get_engine(f"sqlite:///{db_file.as_posix()}")
    Base.metadata.create_all(bind=eng)
    SessionMaker = get_session_factory(eng)

    s = SessionMaker()
    try:
        tenant = Tenant(name='E30流水线取消测试租户', is_active=True)
        s.add(tenant)
        s.flush()
        task = BidCheckTask(
            tenant_id=tenant.id,
            task_type='pre_check',
            title='E30中断与重跑测试',
            status='created',
            progress=0,
            tender_filename='tender.docx',
            bid_filename='bid.docx',
            tender_path=str(tender_docx),
            bid_path=str(bid_docx),
            config_json={'open_date': '2026-11-28'},
        )
        s.add(task)
        s.commit()
        tid = task.id
    finally:
        s.close()

    worker = BidTaskWorker(session_factory=SessionMaker, max_workers=1)
    q = worker.broadcaster.subscribe(tid)
    orig_f4 = worker_mod.run_response_check
    dev_docx = PROJECT_ROOT / 'output' / 'deviation' / f"{tid}_偏离表.docx"

    def _cancel_during_f4(*args, **kwargs):
        # 模拟流水线运行中途已产出半途发现与偏离表残留，随后收到用户中断
        s_mid = SessionMaker()
        try:
            s_mid.add(BidFinding(
                task_id=tid,
                source_module='F4',
                code='TEMP_HALFWAY',
                severity='warning',
                message='半途残留风险',
            ))
            s_mid.commit()
        finally:
            s_mid.close()
        dev_docx.parent.mkdir(parents=True, exist_ok=True)
        dev_docx.write_bytes(b'dirty-docx')
        deviation_stats_sidecar_path(tid).write_text('{"total_rows": 99}', encoding='utf-8', newline='\n')
        assert worker.request_cancel(tid) is True
        assert worker.is_cancel_requested(tid) is True
        cb = kwargs.get('progress_cb')
        if cb is not None:
            cb(1, 1)
        return orig_f4(*args, **kwargs)

    monkeypatch.setattr(worker_mod, 'run_response_check', _cancel_during_f4)
    try:
        worker.submit_check_task(tid, sync=True)

        assert not worker.is_cancel_requested(tid)
        assert not dev_docx.exists()
        assert not deviation_stats_sidecar_path(tid).exists()

        s2 = SessionMaker()
        try:
            row = s2.query(BidCheckTask).filter_by(id=tid).first()
            assert row is not None
            assert row.status == 'failed'
            assert row.error_msg == '用户中断'
            assert row.stage_detail == '用户中断'
            assert row.stats_json is None
            assert row.updated_at is not None
            assert (row.config_json or {}).get('cancelled_by_user') is True
            left_findings = s2.query(BidFinding).filter_by(task_id=tid).count()
            assert left_findings == 0
        finally:
            s2.close()

        # 收集 SSE 队列，确认推送了 error 帧且携带 updated_at
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        err_frames = [data for (ev, data) in events if ev == 'error']
        assert err_frames, f"应广播 SSE error 帧，实际收到: {events}"
        assert err_frames[-1]['status'] == 'failed'
        assert err_frames[-1]['stage_detail'] == '用户中断'
        assert err_frames[-1].get('updated_at')

        # 重跑同一任务：必须清掉 cancelled_by_user 并正常 delivered
        monkeypatch.setattr(worker_mod, 'run_response_check', orig_f4)
        worker.submit_check_task(tid, sync=True)

        s3 = SessionMaker()
        try:
            row_ok = s3.query(BidCheckTask).filter_by(id=tid).first()
            assert row_ok is not None
            assert row_ok.status == 'delivered'
            assert row_ok.progress == 100
            assert 'cancelled_by_user' not in (row_ok.config_json or {})
        finally:
            s3.close()
    finally:
        worker.broadcaster.unsubscribe(tid, q)
        _cleanup_task_deviation_artifacts(tid)
        worker.shutdown(wait=False)
        eng.dispose()


# ── 2. _PipelineCancelled(BaseException) 穿透 F1 fallback 防静默降级 ──────────

def test_e30_f1_loop_cancel_not_swallowed_by_fallback(tmp_path, monkeypatch):
    # 类型契约：_PipelineCancelled 必须直接继承 BaseException 而非 Exception
    assert issubclass(_PipelineCancelled, BaseException)
    assert not issubclass(_PipelineCancelled, Exception)

    tender_docx = _make_docx_file(
        tmp_path / 'tender_f1.docx',
        ['第四章 采购需求', '★1. 投标人须提供7×24小时售后响应承诺。'],
    )
    bid_docx = _make_docx_file(
        tmp_path / 'bid_f1.docx',
        ['投标响应', '我方承诺提供7×24小时售后响应。'],
    )

    # 核心函数直接验证：progress_cb 抛 _PipelineCancelled 时必须透传而非被异常捕获吞掉
    def _raise_cancel(_done: int, _total: int) -> None:
        raise _PipelineCancelled('unit test cancel')

    with pytest.raises(_PipelineCancelled):
        extract_tender_with_llm(str(tender_docx), client=object(), progress_cb=_raise_cancel)

    with pytest.raises(_PipelineCancelled):
        run_response_check(
            tender_docx,
            bid_docx,
            requirements=[{'clause_no': '1', 'mark': '★', 'text': '7×24小时售后响应'}],
            client=None,
            progress_cb=_raise_cancel,
        )

    class _FakeRadarClient:
        def chat(self, *args, **kwargs):
            return type('Resp', (), {'content': '{}'})()

    radar_docx = _make_docx_file(
        tmp_path / 'radar_f1.docx',
        [
            '第三章 采购需求与技术规格要求',
            '★1. 核心交换机设备须参照中兴品牌 ZXR10 型号的技术指标与硬件架构进行配置。',
            '★2. 仅限具备奇安信原厂授权的代理商参与投标，须提供原厂唯一授权书。',
        ],
    )
    with pytest.raises(_PipelineCancelled):
        scan_rigging_radar(
            str(radar_docx),
            client=_FakeRadarClient(),
            progress_cb=_raise_cancel,
        )

    # 专项回归：验证 worker.py F1 `try ... except _PipelineCancelled: raise except Exception:` 不吞取消
    db_file = tmp_path / 'e30_f1_fallback.db'
    eng = get_engine(f"sqlite:///{db_file.as_posix()}")
    Base.metadata.create_all(bind=eng)
    SessionMaker = get_session_factory(eng)

    s = SessionMaker()
    try:
        tenant = Tenant(name='E30防降级测试租户', is_active=True)
        s.add(tenant)
        s.flush()
        task = BidCheckTask(
            tenant_id=tenant.id,
            task_type='pre_check',
            title='F1循环中断防降级测试',
            status='created',
            progress=0,
            tender_filename='tender_f1.docx',
            bid_filename='bid_f1.docx',
            tender_path=str(tender_docx),
            bid_path=str(bid_docx),
        )
        s.add(task)
        s.commit()
        tid = task.id
    finally:
        s.close()

    worker = BidTaskWorker(session_factory=SessionMaker, max_workers=1)
    fallback_called = [False]

    def _forbid_fallback(*args, **kwargs):
        fallback_called[0] = True
        raise AssertionError('取消异常不得被 worker.py F1 fallback(except Exception) 吞掉并降级！')

    def _fake_extract_llm(tender_path, client=None, progress_cb=None):
        assert worker.request_cancel(tid) is True
        assert progress_cb is not None
        progress_cb(1, 3)  # 在 worker._on_f1_clause_progress 内抛出 _PipelineCancelled
        return {'marked_clauses': [], 'invalid_bid_clauses': []}

    monkeypatch.setattr(worker_mod, 'resolve_effective_llm_client', lambda _s, _uid: (object(), 'active'))
    monkeypatch.setattr(worker_mod, 'build_review_checklist', _forbid_fallback)
    monkeypatch.setattr(worker_mod, 'extract_tender_with_llm', _fake_extract_llm)

    try:
        worker.submit_check_task(tid, sync=True)
        assert fallback_called[0] is False

        s2 = SessionMaker()
        try:
            row = s2.query(BidCheckTask).filter_by(id=tid).first()
            assert row is not None
            assert row.status == 'failed'
            assert row.error_msg == '用户中断'
            assert row.stage_detail == '用户中断'
            assert row.stats_json is None
            assert (row.config_json or {}).get('cancelled_by_user') is True
        finally:
            s2.close()
    finally:
        _cleanup_task_deviation_artifacts(tid)
        worker.shutdown(wait=False)
        eng.dispose()


# ── 3. Cancel HTTP 接口（404 / 409 / 审计 / 删除文案与重入删除）───────────────

def test_e30_cancel_api_404_409_audit_and_delete_message(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'e30_cancel_api.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    t_bytes = _make_docx_bytes(['第一章 需求', '★1. 质保期不少于12个月。'])
    b_bytes = _make_docx_bytes(['第一章 响应', '★1. 质保期不少于12个月。'])

    with TestClient(app) as tc:
        r_login = tc.post('/api/auth/login', json={'username': 'admin', 'password': 'BidGuard2026!'})
        hdrs = {'Authorization': f"Bearer {r_login.json()['token']}"}

        r_create = tc.post(
            '/api/checks',
            headers=hdrs,
            files={
                'tender_file': ('t.docx', t_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                'bid_file': ('b.docx', b_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
            },
            data={'title': 'Cancel API 测试任务', 'auto_run': 'false'},
        )
        assert r_create.status_code == 200
        tid = r_create.json()['id']

        # created 非运行态调 cancel 返回 409 TASK_NOT_RUNNING
        r_cancel_idle = tc.post(f"/api/checks/{tid}/cancel", headers=hdrs)
        assert r_cancel_idle.status_code == 409
        assert r_cancel_idle.json()['detail']['code'] == 'TASK_NOT_RUNNING'

        # 不存在的任务 & 跨租户任务调 cancel 返回 404
        assert tc.post('/api/checks/999999/cancel', headers=hdrs).status_code == 404
        with db_mod.SessionLocal() as sess:
            other_t = Tenant(name='其他租户_E30', is_active=True)
            sess.add(other_t)
            sess.flush()
            other_task = BidCheckTask(
                tenant_id=other_t.id,
                task_type='pre_check',
                title='他租户任务',
                status='checking',
                tender_filename='t.docx',
                bid_filename='b.docx',
            )
            sess.add(other_task)
            sess.commit()
            other_tid = other_task.id
        assert tc.post(f"/api/checks/{other_tid}/cancel", headers=hdrs).status_code == 404

        # 模拟运行态：登记取消事件 + 半途 finding
        worker = checks_router_mod.get_worker()
        cancel_ev = worker.register_task_execution(tid)
        with db_mod.SessionLocal() as sess:
            row = sess.query(BidCheckTask).filter_by(id=tid).first()
            row.status = 'checking'
            row.progress = 55
            row.stage_detail = 'F2 算术校验中'
            sess.add(BidFinding(
                task_id=tid,
                source_module='F2',
                code='TMP',
                severity='warning',
                message='运行中临时发现',
            ))
            sess.commit()

        try:
            # 未中断直接删除应报 409，并提示'运行中任务请先中断后再删除'
            r_del_running = tc.delete(f"/api/checks/{tid}", headers=hdrs)
            assert r_del_running.status_code == 409
            assert r_del_running.json()['detail']['code'] == 'TASK_RUNNING'
            assert r_del_running.json()['detail']['message'] == '运行中任务请先中断后再删除'

            q = worker.broadcaster.subscribe(tid)
            try:
                # 执行中断
                r_cancel = tc.post(f"/api/checks/{tid}/cancel", headers=hdrs)
                assert r_cancel.status_code == 200
                body = r_cancel.json()
                assert body['ok'] is True
                assert '已发送中断指令' in body['message']
                assert cancel_ev.is_set() is True

                events = []
                while not q.empty():
                    events.append(q.get_nowait())
                assert any(ev == 'error' and data.get('stage_detail') == '用户中断' for (ev, data) in events)
            finally:
                worker.broadcaster.unsubscribe(tid, q)

            # 校验数据库：BidFinding 已清空，config_json.cancelled_by_user=True，审计日志已落库
            with db_mod.SessionLocal() as sess:
                row_after = sess.query(BidCheckTask).filter_by(id=tid).first()
                assert row_after.status == 'failed'
                assert row_after.stage_detail == '用户中断'
                assert row_after.error_msg == '用户中断'
                assert (row_after.config_json or {}).get('cancelled_by_user') is True
                assert sess.query(BidFinding).filter_by(task_id=tid).count() == 0
                audit = (
                    sess.query(AuditLog)
                    .filter_by(action='cancel_check_task', target=f"task:{tid}")
                    .first()
                )
                assert audit is not None

            # 线程尚未退出时重复点击中断保持幂等 200；线程 unwind 退出注消后再次 cancel 返回 409 TASK_NOT_RUNNING
            r_cancel_idempotent = tc.post(f"/api/checks/{tid}/cancel", headers=hdrs)
            assert r_cancel_idempotent.status_code == 200

            worker.unregister_task_execution(tid, cancel_ev)
            r_cancel_again = tc.post(f"/api/checks/{tid}/cancel", headers=hdrs)
            assert r_cancel_again.status_code == 409
            assert r_cancel_again.json()['detail']['code'] == 'TASK_NOT_RUNNING'

            # 验证真实竞态：DB 已由 cancel 置 failed，后台线程仍在 _in_flight 收尾时，DELETE 仍允许立即清理
            cancel_ev2 = worker.register_task_execution(tid)
            worker.request_cancel(tid)
            assert tid in worker._in_flight
            assert worker.is_cancel_requested(tid) is True
            r_del_after_cancel = tc.delete(f"/api/checks/{tid}", headers=hdrs)
            assert r_del_after_cancel.status_code == 200
        finally:
            worker.unregister_task_execution(tid, cancel_ev)
            if 'cancel_ev2' in locals():
                worker.unregister_task_execution(tid, cancel_ev2)


# ── 4. 服务启动遗留 running 任务自动恢复 ──────────────────────────────────────

def test_e30_startup_recover_stale_tasks(tmp_path):
    db_file = tmp_path / 'e30_recover_stale.db'
    eng = get_engine(f"sqlite:///{db_file.as_posix()}")
    Base.metadata.create_all(bind=eng)
    SessionMaker = get_session_factory(eng)

    s = SessionMaker()
    try:
        tenant = Tenant(name='E30启动恢复租户', is_active=True)
        s.add(tenant)
        s.flush()
        tasks = [
            BidCheckTask(tenant_id=tenant.id, task_type='pre_check', title='遗留extracting', status='extracting', progress=20),
            BidCheckTask(tenant_id=tenant.id, task_type='pre_check', title='遗留grading', status='grading', progress=90),
            BidCheckTask(tenant_id=tenant.id, task_type='radar', title='遗留radar', status='checking', progress=45),
            BidCheckTask(tenant_id=tenant.id, task_type='pre_check', title='正常已完成', status='delivered', progress=100),
            BidCheckTask(tenant_id=tenant.id, task_type='pre_check', title='此前用户中断', status='failed', stage_detail='用户中断', error_msg='用户中断'),
        ]
        s.add_all(tasks)
        s.commit()
        ids = [t.id for t in tasks]
    finally:
        s.close()

    try:
        recovered = recover_stale_tasks(session_factory=SessionMaker)
        assert recovered == 3

        s2 = SessionMaker()
        try:
            for tid in ids[:3]:
                row = s2.query(BidCheckTask).filter_by(id=tid).first()
                assert row.status == 'failed'
                assert row.stage_detail == '服务重启中断'
                assert row.error_msg == '服务重启中断'
                assert row.finished_at is not None
                assert row.updated_at is not None

            done_row = s2.query(BidCheckTask).filter_by(id=ids[3]).first()
            assert done_row.status == 'delivered'

            user_fail_row = s2.query(BidCheckTask).filter_by(id=ids[4]).first()
            assert user_fail_row.status == 'failed'
            assert user_fail_row.stage_detail == '用户中断'

            audit = s2.query(AuditLog).filter_by(action='recover_stale_tasks').first()
            assert audit is not None
            assert isinstance(audit.detail_json, dict)
            assert audit.detail_json.get('reason') == 'service_restart'
        finally:
            s2.close()

        # 二次调用保持幂等
        assert recover_stale_tasks(session_factory=SessionMaker) == 0
    finally:
        eng.dispose()


# ── 5. F1 循环心跳帧与节流护栏 ────────────────────────────────────────────────

def test_e30_f1_heartbeat_progress_frames(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    tender_docx = _make_docx_file(
        tmp_path / 'tender_hb.docx',
        ['第一章 采购需求', '★1. 质保期不少于12个月。'],
    )
    bid_docx = _make_docx_file(
        tmp_path / 'bid_hb.docx',
        ['第一章 投标响应', '★1. 质保期不少于12个月。（完全响应）'],
    )

    db_file = tmp_path / 'e30_heartbeat.db'
    eng = get_engine(f"sqlite:///{db_file.as_posix()}")
    Base.metadata.create_all(bind=eng)
    SessionMaker = get_session_factory(eng)

    def _create_task(title: str) -> int:
        s = SessionMaker()
        try:
            tenant = s.query(Tenant).first()
            if tenant is None:
                tenant = Tenant(name='E30心跳租户', is_active=True)
                s.add(tenant)
                s.flush()
            task = BidCheckTask(
                tenant_id=tenant.id,
                task_type='pre_check',
                title=title,
                status='created',
                progress=0,
                tender_filename='tender_hb.docx',
                bid_filename='bid_hb.docx',
                tender_path=str(tender_docx),
                bid_path=str(bid_docx),
                config_json={'open_date': '2026-11-28'},
            )
            s.add(task)
            s.commit()
            return task.id
        finally:
            s.close()

    worker = BidTaskWorker(session_factory=SessionMaker, max_workers=1)
    monkeypatch.setattr(worker_mod, 'resolve_effective_llm_client', lambda _s, _uid: (object(), 'active'))

    def _fake_extract_3_clauses(tender_path, client=None, progress_cb=None):
        assert progress_cb is not None
        progress_cb(0, 3)
        progress_cb(1, 3)
        progress_cb(2, 3)
        progress_cb(3, 3)
        return {
            'marked_clauses': [
                {'clause_no': '1', 'mark': '★', 'text': '质保期不少于12个月', 'requirement_type': 'requirement'},
            ],
            'invalid_bid_clauses': [],
        }

    monkeypatch.setattr(worker_mod, 'extract_tender_with_llm', _fake_extract_3_clauses)

    tid1 = _create_task('心跳无节流全推验证')
    tid2 = _create_task('心跳60秒节流验证')
    q1 = worker.broadcaster.subscribe(tid1)
    q2 = worker.broadcaster.subscribe(tid2)
    try:
        # 场景 A：关闭节流间隔，1/3、2/3、3/3 全部刷出且带 updated_at
        worker._f1_heartbeat_interval_sec = 0.0
        worker.submit_check_task(tid1, sync=True)

        frames1 = []
        while not q1.empty():
            frames1.append(q1.get_nowait())
        prog1 = [data for (ev, data) in frames1 if ev == 'progress']
        details1 = [d['stage_detail'] for d in prog1]
        assert any('已判定 1 / 3 条' in d for d in details1)
        assert any('已判定 2 / 3 条' in d for d in details1)
        assert any('已判定 3 / 3 条' in d for d in details1)
        assert all(bool(d.get('updated_at')) for d in prog1)
        done1 = [data for (ev, data) in frames1 if ev == 'done']
        assert done1 and done1[-1].get('updated_at')

        # 场景 B：节流窗口 60s 下，中间 2/3 被合并跳过，首条 1/3 与末条 3/3 必达
        worker._f1_heartbeat_interval_sec = 60.0
        worker.submit_check_task(tid2, sync=True)

        frames2 = []
        while not q2.empty():
            frames2.append(q2.get_nowait())
        details2 = [data['stage_detail'] for (ev, data) in frames2 if ev == 'progress']
        assert any('已判定 1 / 3 条' in d for d in details2)
        assert not any('已判定 2 / 3 条' in d for d in details2)
        assert any('已判定 3 / 3 条' in d for d in details2)
    finally:
        worker.broadcaster.unsubscribe(tid1, q1)
        worker.broadcaster.unsubscribe(tid2, q2)
        _cleanup_task_deviation_artifacts(tid1)
        _cleanup_task_deviation_artifacts(tid2)
        worker.shutdown(wait=False)
        eng.dispose()


# ── 6. F6 围串标雷达接入 _cancel_flags 与异常收敛（无僵尸 checking）─────────

def test_e30_radar_cancel_and_exception_no_zombie_checking(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'e30_radar_cancel.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    t_bytes = _make_docx_bytes([
        '开标时间：2026年11月28日 09:30',
        '1. 核心交换机须参照华为 S12700E 系列型号并具有原厂针对本项目唯一授权书。',
    ])

    with TestClient(app) as tc:
        r_login = tc.post('/api/auth/login', json={'username': 'admin', 'password': 'BidGuard2026!'})
        hdrs = {'Authorization': f"Bearer {r_login.json()['token']}"}
        orig_scan = radar_router_mod.scan_rigging_radar

        # Case A：POST /api/radar 扫描中途通过服务端 request_cancel 中断 -> 409 TASK_CANCELLED，无僵尸 checking
        captured_tid: list[int] = []

        def _cancel_on_first_scan(*args, **kwargs):
            with db_mod.SessionLocal() as sess:
                checking_row = (
                    sess.query(BidCheckTask)
                    .filter_by(task_type='radar', status='checking')
                    .order_by(BidCheckTask.id.desc())
                    .first()
                )
                assert checking_row is not None
                radar_tid = checking_row.id
                captured_tid.append(radar_tid)
            assert checks_router_mod.get_worker().request_cancel(radar_tid) is True
            cb = kwargs.get('progress_cb')
            assert cb is not None
            cb(1, 2)
            return {}

        monkeypatch.setattr(radar_router_mod, 'scan_rigging_radar', _cancel_on_first_scan)
        r_scan_cancel = tc.post(
            '/api/radar',
            headers=hdrs,
            files={'tender_file': ('rad_cancel.docx', t_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')},
            data={'title': '雷达扫描中断测试'},
        )
        assert r_scan_cancel.status_code == 409
        assert r_scan_cancel.json()['detail']['code'] == 'TASK_CANCELLED'
        assert len(captured_tid) == 1
        cancelled_tid = captured_tid[0]
        assert not checks_router_mod.get_worker().is_cancel_requested(cancelled_tid)

        with db_mod.SessionLocal() as sess:
            row_c = sess.query(BidCheckTask).filter_by(id=cancelled_tid).first()
            assert row_c is not None
            assert row_c.status == 'failed'
            assert row_c.stage_detail == '用户中断'
            assert row_c.error_msg == '用户中断'
            assert (row_c.config_json or {}).get('cancelled_by_user') is True

        # 已标记 failed，可直接删除，不会卡成永久僵尸记录
        assert tc.delete(f"/api/radar/{cancelled_tid}", headers=hdrs).status_code == 200

        # Case B：正常新建雷达任务，再测试 /rescan 取消与意外异常收敛
        monkeypatch.setattr(radar_router_mod, 'scan_rigging_radar', orig_scan)
        r_ok = tc.post(
            '/api/radar',
            headers=hdrs,
            files={'tender_file': ('rad_ok.docx', t_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')},
            data={'title': '雷达重扫与异常收敛测试'},
        )
        assert r_ok.status_code == 200
        ok_tid = r_ok.json()['task_id']

        def _cancel_on_rescan(*args, **kwargs):
            assert checks_router_mod.get_worker().request_cancel(ok_tid) is True
            kwargs['progress_cb'](1, 2)
            return {}

        monkeypatch.setattr(radar_router_mod, 'scan_rigging_radar', _cancel_on_rescan)
        r_rescan_cancel = tc.post(f"/api/radar/{ok_tid}/rescan", headers=hdrs)
        assert r_rescan_cancel.status_code == 409
        assert r_rescan_cancel.json()['detail']['code'] == 'TASK_CANCELLED'
        with db_mod.SessionLocal() as sess:
            row_rc = sess.query(BidCheckTask).filter_by(id=ok_tid).first()
            assert row_rc.status == 'failed'
            assert row_rc.stage_detail == '用户中断'

        def _boom_scan(*args, **kwargs):
            raise RuntimeError('simulated radar engine failure')

        monkeypatch.setattr(radar_router_mod, 'scan_rigging_radar', _boom_scan)
        try:
            r_rescan_err = tc.post(f"/api/radar/{ok_tid}/rescan", headers=hdrs)
            assert r_rescan_err.status_code == 500
        except RuntimeError as exc:
            assert 'simulated radar engine failure' in str(exc)
        with db_mod.SessionLocal() as sess:
            row_err = sess.query(BidCheckTask).filter_by(id=ok_tid).first()
            assert row_err.status == 'failed'
            assert 'simulated radar engine failure' in (row_err.error_msg or '')
            assert row_err.finished_at is not None

        assert tc.delete(f"/api/radar/{ok_tid}", headers=hdrs).status_code == 200
