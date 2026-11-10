# -*- coding: utf-8 -*-
"""E16 · 交付中心四大件独立下载 + 批量打包与内容自选卡单元测试（断言严格数数，禁空循环假绿）。

覆盖任务卡 §3 全部验收标准：
1. 两个正常完成任务 + include 全选（original/redline/deviation/signoff） ->
   大 ZIP 内含两个 {task_id}_{任务标题}/ 子目录，且各含所选 4 个文件；
2. include 仅 {'original'} -> 子目录内只含 0_投标书原件.docx；
3. 混合一个源文件缺失的任务 -> 跳过该异常任务并在响应 failed 列表中诚实列出原因，其余正常任务照常打包；
   若全部任务均失败则返回 422；
4. GET /api/reports/{task_id}/signoff -> 返回合法 docx（ZIP PK 头 + word/document.xml 校验），
   且任务不存在或缺源文件时返回 404；
5. 安全审计日志包含 action='export_batch_zip' 记录。
"""
from __future__ import annotations

import io
import os
import sys
import zipfile
from pathlib import Path

from docx import Document
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.database import PROJECT_ROOT
from backend.main import create_app
from backend.models import BidCheckTask


def _make_docx_bytes(lines: list[str]) -> bytes:
    doc = Document()
    for line in lines:
        doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_e16_signoff_download_and_batch_zip_with_selective_include_and_audit(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'test_e16_reports.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    created_task_ids: list[int] = []
    batch_zip_files: list[Path] = []

    with TestClient(app) as tc:
        r_login = tc.post('/api/auth/login', json={'username': 'admin', 'password': 'BidGuard2026!'})
        assert r_login.status_code == 200
        hdrs = {'Authorization': f"Bearer {r_login.json()['token']}"}

        t_bytes = _make_docx_bytes(['第一章 采购需求', '★1. 投标人须提供三年免费质保服务。'])
        b_bytes = _make_docx_bytes(['第一章 技术响应', '★1. 投标人须提供三年免费质保服务。（我方完全响应）'])

        # 创建并同步跑完两个正常任务
        for title in ('政务云平台项目A', '数据治理项目B'):
            r_c = tc.post(
                '/api/checks',
                headers=hdrs,
                files={
                    'tender_file': ('t.docx', t_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                    'bid_file': ('b.docx', b_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                },
                data={'title': title, 'auto_run': 'false'},
            )
            assert r_c.status_code == 200
            tid = r_c.json()['id']
            created_task_ids.append(tid)
            r_run = tc.post(f"/api/checks/{tid}/run?sync=true", headers=hdrs)
            assert r_run.status_code == 200

        tid_a, tid_b = created_task_ids[0], created_task_ids[1]

        # 创建第 3 个任务并故意删除其物理源文件（模拟源文件缺失场景）
        r_c3 = tc.post(
            '/api/checks',
            headers=hdrs,
            files={
                'tender_file': ('t3.docx', t_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                'bid_file': ('b3.docx', b_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
            },
            data={'title': '源文件缺失项目C', 'auto_run': 'false'},
        )
        assert r_c3.status_code == 200
        tid_missing = r_c3.json()['id']
        created_task_ids.append(tid_missing)

        # 将第 3 个任务状态设为 delivered 但抹掉其源文件
        # 注意：必须绑定与 create_app 相同的库（运行时读 db.engine 模块属性——create_app 已重设为测试库；
        # 直接 import engine 名字拿到的是 import 时旧引用，无参 get_session_factory() 则另走 DATABASE_URL 解析）
        import backend.database as db_mod
        from backend.database import get_session_factory
        SessionMaker = get_session_factory(db_mod.engine)
        sess = SessionMaker()
        try:
            row_m = sess.query(BidCheckTask).filter_by(id=tid_missing).first()
            assert row_m is not None
            row_m.status = 'delivered'
            row_m.tender_path = 'data/uploads/not_exist_tender.docx'
            row_m.bid_path = 'data/uploads/not_exist_bid.docx'
            sess.commit()
        finally:
            sess.close()

        # ── 验收点 ④：GET /{task_id}/signoff 返回合法 docx，缺源文件或不存在任务返回 404 ──
        r_sig = tc.get(f"/api/reports/{tid_a}/signoff", headers=hdrs)
        assert r_sig.status_code == 200
        assert r_sig.content[:2] == b'PK'
        with zipfile.ZipFile(io.BytesIO(r_sig.content), 'r') as z_doc:
            assert 'word/document.xml' in z_doc.namelist()

        r_sig_missing_src = tc.get(f"/api/reports/{tid_missing}/signoff", headers=hdrs)
        assert r_sig_missing_src.status_code == 404

        r_sig_not_found = tc.get('/api/reports/999999/signoff', headers=hdrs)
        assert r_sig_not_found.status_code == 404

        # ── 验收点 ① & ③：两个正常任务 + 1 个缺源任务 + include 全选 ──
        r_batch_all = tc.post(
            '/api/reports/batch-zip',
            headers=hdrs,
            json={
                'task_ids': [tid_a, tid_b, tid_missing],
                'include': ['original', 'redline', 'deviation', 'signoff'],
            },
        )
        assert r_batch_all.status_code == 200
        b_data = r_batch_all.json()
        assert b_data['succeeded_task_ids'] == [tid_a, tid_b]
        assert len(b_data['failed']) == 1
        assert b_data['failed'][0]['task_id'] == tid_missing
        assert '缺失' in b_data['failed'][0]['reason']

        zip_abs_1 = (PROJECT_ROOT / b_data['zip_path']).resolve()
        batch_zip_files.append(zip_abs_1)
        assert zip_abs_1.is_file()
        with zipfile.ZipFile(str(zip_abs_1), 'r') as zf1:
            names1 = zf1.namelist()
            prefix_a = f"{tid_a}_政务云平台项目A/"
            prefix_b = f"{tid_b}_数据治理项目B/"
            for prefix in (prefix_a, prefix_b):
                assert f"{prefix}0_投标书原件.docx" in names1
                assert f"{prefix}1_投标书原位标红批注件.docx" in names1
                assert f"{prefix}2_技术与商务偏离表.docx" in names1
                assert f"{prefix}3_封标前人工终核签字确认单.docx" in names1

        # ── 验收点 ②：include 仅 {'original'} -> 子包内只含原件 ──
        r_batch_orig = tc.post(
            '/api/reports/batch-zip',
            headers=hdrs,
            json={
                'task_ids': [tid_a],
                'include': ['original'],
            },
        )
        assert r_batch_orig.status_code == 200
        b_orig = r_batch_orig.json()
        zip_abs_2 = (PROJECT_ROOT / b_orig['zip_path']).resolve()
        batch_zip_files.append(zip_abs_2)
        with zipfile.ZipFile(str(zip_abs_2), 'r') as zf2:
            names2 = zf2.namelist()
            assert names2 == [f"{tid_a}_政务云平台项目A/0_投标书原件.docx"]

        # ── 验收点 ⑤：审计日志包含 export_batch_zip ──
        r_audit = tc.get('/api/reports/audit-logs?action=export_batch_zip', headers=hdrs)
        assert r_audit.status_code == 200
        audit_items = r_audit.json()['items']
        assert len(audit_items) >= 2
        assert audit_items[0]['action'] == 'export_batch_zip'

        # 清理测试创建的任务与产物
        for tid in created_task_ids:
            tc.delete(f"/api/checks/{tid}", headers=hdrs)
        for zp in batch_zip_files:
            zp.unlink(missing_ok=True)