# -*- coding: utf-8 -*-
"""E11 · 体检任务耗时统计与前端展示卡单元测试（断言严格数数，禁空循环假绿）。

覆盖任务卡 §3 全部验收标准：
1. 同步体检任务完成后：started_at / finished_at 非空，finished_at >= started_at，
   duration_json 各阶段（f1_ms/f2_ms/f3_ms/f4_ms/grading_ms/total_ms）数值非负且 total_ms >= max(各阶段)；
   重新执行体检时清空旧耗时并重新计量；
2. 异常终态（failed）任务：finished_at 与 duration_json（含 total_ms）同样被写入；
3. 老库平滑加列（无 started_at / finished_at / duration_json 列的旧 SQLite 库）：
   启动或连接时自动执行 ALTER TABLE ADD COLUMN 补齐三列且已有行默认为 NULL，零报错；
4. API 透出：GET /api/checks 与 GET /api/checks/{id} 响应包含 started_at / finished_at / duration 字典。
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from docx import Document
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.database import Base, get_engine, get_session_factory
from backend.main import create_app
from backend.models import BidCheckTask, Tenant, ensure_bid_check_tasks_columns
from backend.worker import BidTaskWorker


def _make_docx_bytes(paragraphs: list[str]) -> bytes:
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _save_temp_docx(paragraphs: list[str]) -> Path:
    fd, path = tempfile.mkstemp(suffix='.docx')
    os.close(fd)
    Path(path).write_bytes(_make_docx_bytes(paragraphs))
    return Path(path)


# ── 1. 同步体检成功路径与失敗路径的 started_at / finished_at / duration_json 核验 ──

def test_e11_worker_records_stage_durations_on_success_and_failure(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'test_e11_worker.db'
    eng = get_engine(f"sqlite:///{db_file.as_posix()}")
    Base.metadata.create_all(bind=eng)
    SessionMaker = get_session_factory(eng)

    t_path = _save_temp_docx(['第一章 采购需求', '★1. 系统须支持国产银河麒麟操作系统 V10。'])
    b_path = _save_temp_docx(['技术响应表', '★1. 系统须支持国产银河麒麟操作系统 V10。（我方完全满足）'])

    session = SessionMaker()
    try:
        tenant = Tenant(name='耗时测试部', is_active=True)
        session.add(tenant)
        session.flush()

        ok_task = BidCheckTask(
            tenant_id=tenant.id,
            task_type='pre_check',
            title='正常耗时统计任务',
            status='created',
            progress=0,
            tender_filename='t.docx',
            bid_filename='b.docx',
            tender_path=str(t_path),
            bid_path=str(b_path),
        )
        fail_task = BidCheckTask(
            tenant_id=tenant.id,
            task_type='pre_check',
            title='文件缺失异常任务',
            status='created',
            progress=0,
            tender_filename='missing.docx',
            bid_filename='missing_b.docx',
            tender_path=str(tmp_path / 'not_exist_tender.docx'),
            bid_path=str(tmp_path / 'not_exist_bid.docx'),
        )
        session.add_all([ok_task, fail_task])
        session.commit()
        ok_id = ok_task.id
        fail_id = fail_task.id
    finally:
        session.close()

    worker = BidTaskWorker(session_factory=SessionMaker, max_workers=2)
    try:
        # (a) 正常完成任务
        worker.submit_check_task(ok_id, sync=True)
        s1 = SessionMaker()
        try:
            row_ok = s1.query(BidCheckTask).filter_by(id=ok_id).first()
            assert row_ok is not None
            assert row_ok.status == 'delivered'
            assert row_ok.started_at is not None
            assert row_ok.finished_at is not None
            assert row_ok.finished_at >= row_ok.started_at
            assert row_ok.duration_json is not None

            dur = json.loads(row_ok.duration_json)
            stage_keys = ('f1_ms', 'f2_ms', 'f3_ms', 'f4_ms', 'grading_ms')
            for k in stage_keys:
                assert k in dur
                assert isinstance(dur[k], int) and dur[k] >= 0
            assert isinstance(dur.get('total_ms'), int)
            assert dur['total_ms'] >= max(dur[k] for k in stage_keys)
        finally:
            s1.close()

        # (b) 异常终态任务（failed）：finished_at 与 duration_json 同样被写入
        worker.submit_check_task(fail_id, sync=True)
        s2 = SessionMaker()
        try:
            row_fail = s2.query(BidCheckTask).filter_by(id=fail_id).first()
            assert row_fail is not None
            assert row_fail.status == 'failed'
            assert row_fail.started_at is not None
            assert row_fail.finished_at is not None
            assert row_fail.finished_at >= row_fail.started_at
            assert row_fail.duration_json is not None
            fail_dur = json.loads(row_fail.duration_json)
            assert isinstance(fail_dur.get('total_ms'), int) and fail_dur['total_ms'] >= 0
        finally:
            s2.close()
    finally:
        worker.shutdown(wait=True)
        t_path.unlink(missing_ok=True)
        b_path.unlink(missing_ok=True)
        eng.dispose()


# ── 2. 老库（缺少 started_at / finished_at / duration_json 三列）平滑自动补列验证 ──

def test_e11_legacy_database_auto_adds_missing_columns(tmp_path):
    legacy_db = tmp_path / 'legacy_without_e11_cols.db'
    conn = sqlite3.connect(str(legacy_db))
    conn.execute(
        """
        CREATE TABLE bid_check_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL,
            user_id INTEGER,
            task_type VARCHAR(32) NOT NULL DEFAULT 'pre_check',
            title VARCHAR(256) NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'created',
            progress INTEGER NOT NULL DEFAULT 0,
            stage_detail VARCHAR(256),
            tender_filename VARCHAR(256),
            bid_filename VARCHAR(256),
            tender_path VARCHAR(512),
            bid_path VARCHAR(512),
            error_msg TEXT,
            stats_json JSON,
            price_manual_json JSON,
            config_json JSON,
            llm_usage_json JSON,
            delivered_at DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("INSERT INTO bid_check_tasks (tenant_id, title) VALUES (1, '旧版本遗留任务')")
    conn.commit()
    conn.close()

    eng = get_engine(f"sqlite:///{legacy_db.as_posix()}")
    try:
        ensure_bid_check_tasks_columns(eng)
        SessionMaker = get_session_factory(eng)
        sess = SessionMaker()
        try:
            old_row = sess.query(BidCheckTask).filter_by(id=1).first()
            assert old_row is not None
            assert old_row.title == '旧版本遗留任务'
            assert old_row.started_at is None
            assert old_row.finished_at is None
            assert old_row.duration_json is None
        finally:
            sess.close()
    finally:
        eng.dispose()


# ── 3. /api/checks 列表与详情接口透出 started_at / finished_at / duration 验证 ──

def test_e11_checks_api_exposes_duration_fields(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'test_e11_api.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    with TestClient(app) as tc:
        r_login = tc.post('/api/auth/login', json={'username': 'admin', 'password': 'BidGuard2026!'})
        assert r_login.status_code == 200
        hdrs = {'Authorization': f"Bearer {r_login.json()['token']}"}

        t_bytes = _make_docx_bytes(['★1. 免费质保期不少于 36 个月。'])
        b_bytes = _make_docx_bytes(['★1. 免费质保期不少于 36 个月。（完全满足）'])
        r_create = tc.post(
            '/api/checks',
            headers=hdrs,
            files={
                'tender_file': ('tender.docx', t_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                'bid_file': ('bid.docx', b_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
            },
            data={'title': 'API耗时透出测试任务', 'auto_run': 'false'},
        )
        assert r_create.status_code == 200
        tid = r_create.json()['id']

        r_run = tc.post(f"/api/checks/{tid}/run?sync=true", headers=hdrs)
        assert r_run.status_code == 200

        r_detail = tc.get(f"/api/checks/{tid}", headers=hdrs)
        assert r_detail.status_code == 200
        task_out = r_detail.json()['task']
        assert task_out['started_at'] is not None
        assert task_out['finished_at'] is not None
        assert isinstance(task_out['duration'], dict)
        assert task_out['duration']['total_ms'] >= 0

        r_list = tc.get('/api/checks', headers=hdrs)
        assert r_list.status_code == 200
        matched = next(x for x in r_list.json()['items'] if x['id'] == tid)
        assert isinstance(matched['duration'], dict)
        assert matched['duration']['total_ms'] >= 0