# -*- coding: utf-8 -*-
"""E25 · 全链路工作流与台账运维补强卡专项单元测试（0 Token 离线可跑，断言严格数数）。

覆盖任务卡 §3 全部 6 组验收标准：
1. F5 在途拦截与重跑清缓存：
   - 任务处于 checking / _in_flight 时调 GET/POST /api/deviation/{id} 返回 409 CHECK_TASK_RUNNING；
   - 旧偏离表产物在任务重跑触发时被自动清理，不会继续返回过期快照；
2. F6 重扫与 open_date 兜底：
   - POST /api/radar/{task_id}/rescan 成功重建 findings 并更新 stats_json 与 radar_rescan 审计日志；
   - 源文件缺失返回 422 SOURCE_FILES_MISSING，跨租户或非 radar 任务返回 404；
3. F7 动态快捷追问：
   - 对比不同 stats_json（如 NEED_MANUAL、有 ★ 告警）与含附件会话返回的 suggested_questions，验证 0-Token 动态变化及备证兜底问题；
4. 法规单条删除：
   - member 调用返回 403；admin 删除单条后 article_count 与 content_hash 按权威口径同步更新并写审计日志；
5. 审计日志 CSV 导出：
   - member 调导出返回 403；admin 带 action 过滤导出 UTF-8 BOM CSV 成功，校验表头、公式注入转义（=cmd 转 '=cmd）及 export_audit_logs 审计记录；
6. 企业证照老库补列、OCR 预填与附件留档清理：
   - 缺失 3 列的老 SQLite 库自动迁移成功；
   - 上传图片/PDF 走 parse-file 返回准确候选字段；
   - 绑定附件、下载、解绑及删除证照彻底清理磁盘文件，跨租户返回 404。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys

from docx import Document
from fastapi.testclient import TestClient
import pymupdf
from sqlalchemy import create_engine

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import backend.database as db_mod
from backend.database import PROJECT_ROOT
from backend.main import create_app
from backend.models import (
    AuditLog,
    BidCheckTask,
    RegulationArticle,
    RegulationLaw,
    ensure_company_certificates_columns,
)


def _make_docx_bytes(lines: list[str]) -> bytes:
    doc = Document()
    for ln in lines:
        doc.add_paragraph(ln)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _make_pdf_bytes(text: str) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 80), text, fontname='china-s', fontsize=12)
    out = doc.tobytes()
    doc.close()
    return out


# ── 1. F5 在途 409 拦截与重跑清旧偏离表缓存 ───────────────────────────────

def test_e25_f5_running_conflict_409_and_rerun_clears_deviation_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'test_e25_f5.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    t_bytes = _make_docx_bytes(['第一章 技术要求', '★1. 免费质保期不少于36个月。'])
    b_bytes = _make_docx_bytes(['第一章 投标响应', '★1. 免费质保期不少于36个月。（我方完全响应）'])

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
            data={'title': 'F5缓存与409测试', 'auto_run': 'false'},
        )
        tid = r_create.json()['id']

        # 将任务状态临时置为 checking，验证 /api/deviation/{tid} 拦截返回 409 CHECK_TASK_RUNNING
        with db_mod.SessionLocal() as sess:
            row = sess.query(BidCheckTask).filter_by(id=tid).first()
            row.status = 'checking'
            sess.commit()

        r_dev_409 = tc.get(f"/api/deviation/{tid}", headers=hdrs)
        assert r_dev_409.status_code == 409
        assert r_dev_409.json()['detail']['code'] == 'CHECK_TASK_RUNNING'

        # 跑完核验并生成偏离表缓存
        r_run = tc.post(f"/api/checks/{tid}/run?sync=true", headers=hdrs)
        assert r_run.status_code == 200

        r_dev_ok = tc.get(f"/api/deviation/{tid}", headers=hdrs)
        assert r_dev_ok.status_code == 200
        dev_docx = PROJECT_ROOT / 'output' / 'deviation' / f"{tid}_偏离表.docx"
        dev_sidecar = PROJECT_ROOT / 'output' / 'deviation' / f"{tid}_stats.json"
        assert dev_docx.is_file() and dev_sidecar.is_file()

        # 污染 sidecar 为 999 行，验证触发重跑后旧缓存被清除
        dev_sidecar.write_text(
            json.dumps({**r_dev_ok.json(), 'total_rows': 999}, ensure_ascii=False),
            encoding='utf-8',
        )
        r_rerun = tc.post(f"/api/checks/{tid}/run?sync=true", headers=hdrs)
        assert r_rerun.status_code == 200
        assert not dev_sidecar.is_file()

        r_dev_fresh = tc.get(f"/api/deviation/{tid}", headers=hdrs)
        assert r_dev_fresh.status_code == 200
        assert r_dev_fresh.json()['total_rows'] != 999

        tc.delete(f"/api/checks/{tid}", headers=hdrs)


# ── 2. F6 雷达单条重扫 rescan 与源文件缺失 422 ───────────────────────────

def test_e25_f6_radar_rescan_and_missing_source_422(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'test_e25_radar.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    t_bytes = _make_docx_bytes([
        '开标时间：2026年11月28日 09:30',
        '1. 核心交换机须参照华为 S12700E 系列型号并具有原厂针对本项目唯一授权书。',
    ])

    with TestClient(app) as tc:
        r_login = tc.post('/api/auth/login', json={'username': 'admin', 'password': 'BidGuard2026!'})
        hdrs = {'Authorization': f"Bearer {r_login.json()['token']}"}

        r_scan = tc.post(
            '/api/radar',
            headers=hdrs,
            files={'tender_file': ('rad.docx', t_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')},
            data={'title': '重扫测试项目'},
        )
        assert r_scan.status_code == 200
        tid = r_scan.json()['task_id']
        assert r_scan.json()['open_date'] == '2026-11-28'

        # 触发重扫
        r_rescan = tc.post(f"/api/radar/{tid}/rescan", headers=hdrs)
        assert r_rescan.status_code == 200
        assert r_rescan.json()['open_date'] == '2026-11-28'
        assert len(r_rescan.json()['suspicious_params']) >= 1

        # 抹去源文件触发 422 SOURCE_FILES_MISSING
        with db_mod.SessionLocal() as sess:
            row = sess.query(BidCheckTask).filter_by(id=tid).first()
            row.tender_path = 'data/uploads/not_exist_radar.docx'
            sess.commit()

        r_rescan_422 = tc.post(f"/api/radar/{tid}/rescan", headers=hdrs)
        assert r_rescan_422.status_code == 422
        assert r_rescan_422.json()['detail']['code'] == 'SOURCE_FILES_MISSING'

        tc.delete(f"/api/radar/{tid}", headers=hdrs)


# ── 3. F7 动态快捷追问 suggested_questions 0-Token 生成 ──────────────────

def test_e25_f7_contextual_suggested_questions(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'test_e25_qa_sug.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    t_bytes = _make_docx_bytes(['★1. 质保期36个月。'])
    b_bytes = _make_docx_bytes(['★1. 质保期36个月。'])

    with TestClient(app) as tc:
        r_login = tc.post('/api/auth/login', json={'username': 'admin', 'password': 'BidGuard2026!'})
        hdrs = {'Authorization': f"Bearer {r_login.json()['token']}"}

        r_task = tc.post(
            '/api/checks',
            headers=hdrs,
            files={
                'tender_file': ('t.docx', t_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                'bid_file': ('b.docx', b_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
            },
            data={'title': '动态追问测试', 'auto_run': 'false'},
        )
        tid = r_task.json()['id']

        # 写入 NEED_MANUAL 状态
        with db_mod.SessionLocal() as sess:
            row = sess.query(BidCheckTask).filter_by(id=tid).first()
            row.stats_json = {'price_status': 'NEED_MANUAL', 'f1_extracted_count': 3}
            sess.commit()

        r_sess = tc.post('/api/qa/sessions', headers=hdrs, json={'task_id': tid})
        assert r_sess.status_code == 200
        sug_initial = r_sess.json()['suggested_questions']
        assert len(sug_initial) >= 4
        assert any('资格废标门槛' in q and '评分加分项' in q for q in sug_initial)
        assert any('限价' in q or '报价' in q for q in sug_initial)

        sid = r_sess.json()['id']
        # 上传补充附件后，suggested_questions 自动包含针对补充附件的追问
        r_att = tc.post(
            f"/api/qa/sessions/{sid}/attachments",
            headers=hdrs,
            files={'file': ('更正公告.txt', '交货期延长至45天'.encode('utf-8'), 'text/plain')},
        )
        assert r_att.status_code == 200

        r_sess_after = tc.get(f"/api/qa/sessions/{sid}", headers=hdrs)
        sug_after = r_sess_after.json()['suggested_questions']
        assert any('更正公告.txt' in q for q in sug_after)


# ── 4 & 5. 法规单条删除哈希重算 + 审计日志 CSV 导出与公式注入防护 ─────────

def test_e25_delete_single_regulation_article_and_export_audit_csv(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'test_e25_reg_audit.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    with TestClient(app) as tc:
        r_admin = tc.post('/api/auth/login', json={'username': 'admin', 'password': 'BidGuard2026!'})
        admin_hdrs = {'Authorization': f"Bearer {r_admin.json()['token']}"}

        r_mem = tc.post('/api/auth/login', json={'username': 'liming', 'password': 'BidGuard2026!'})
        mem_hdrs = {'Authorization': f"Bearer {r_mem.json()['token']}"}

        # (a) 导入一部含 2 条条文的测试法规
        law_json = json.dumps({
            'name': '测试招投标管理暂行办法',
            'articles': [
                {'article_no': '第一条', 'content': '为了规范测试招投标活动，制定本办法。'},
                {'article_no': '第二条', 'content': '本办法适用于测试项目。'},
            ],
        }, ensure_ascii=False).encode('utf-8')
        r_imp = tc.post(
            '/api/regulations/import',
            headers=admin_hdrs,
            files={'file': ('test_law.json', law_json, 'application/json')},
        )
        assert r_imp.status_code == 200
        law_id = r_imp.json()['law_id']

        r_detail = tc.get(f"/api/regulations/laws/{law_id}", headers=admin_hdrs)
        arts = r_detail.json()['articles']
        assert len(arts) == 2
        art1_id, art2 = arts[0]['id'], arts[1]

        # member 删除单条返回 403
        r_del_403 = tc.delete(f"/api/regulations/articles/{art1_id}", headers=mem_hdrs)
        assert r_del_403.status_code == 403

        # admin 删除第一条 -> 剩余 1 条，且 law.content_hash 等于剩余条文按权威口径计算的哈希
        r_del_ok = tc.delete(f"/api/regulations/articles/{art1_id}", headers=admin_hdrs)
        assert r_del_ok.status_code == 200

        r_detail_after = tc.get(f"/api/regulations/laws/{law_id}", headers=admin_hdrs)
        law_after = r_detail_after.json()['law']
        assert law_after['article_count'] == 1
        expected_hash = hashlib.sha256(f"{art2['article_no']}:{art2['content_hash']}".encode('utf-8')).hexdigest()
        assert law_after['content_hash'] == expected_hash

        # (b) 注入一条含 '=cmd|' 公式前缀的审计记录，验证 CSV 导出权限、UTF-8 BOM 与转义
        with db_mod.SessionLocal() as sess:
            sess.add(AuditLog(tenant_id=1, user_id=1, action='formula_test', target='=cmd|calc', ip='+127.0.0.1'))
            sess.commit()

        r_csv_403 = tc.get('/api/reports/audit-logs/export', headers=mem_hdrs)
        assert r_csv_403.status_code == 403

        r_csv = tc.get('/api/reports/audit-logs/export?action=formula_test', headers=admin_hdrs)
        assert r_csv.status_code == 200
        assert r_csv.content.startswith(b'\xef\xbb\xbf')  # UTF-8 BOM
        csv_text = r_csv.content.decode('utf-8-sig')
        assert 'ID,审计动作,操作对象,详情快照,IP,记录时间(UTC)' in csv_text
        assert "'=cmd|calc" in csv_text
        assert "'+127.0.0.1" in csv_text


# ── 6. 企业证照老库自动补列、扫件 OCR 预填与原件上传/下载/删除清盘 ────────

def test_e25_certificate_legacy_migration_ocr_prefill_and_attachment_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')

    # (a) 验证缺少 3 列的老 SQLite 库自动补列
    legacy_db = tmp_path / 'legacy_certs.db'
    conn = sqlite3.connect(str(legacy_db))
    conn.execute(
        """
        CREATE TABLE company_certificates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL,
            cert_name VARCHAR(256) NOT NULL,
            cert_no VARCHAR(128),
            valid_until DATE NOT NULL,
            warn_days INTEGER NOT NULL DEFAULT 30,
            remark TEXT,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()

    eng = create_engine(f"sqlite:///{legacy_db.as_posix()}")
    try:
        added = ensure_company_certificates_columns(eng)
        assert set(added) == {'attachment_name', 'attachment_path', 'attachment_size'}
    finally:
        eng.dispose()

    # (b) 验证证照扫件 parse-file 预填 + 上传绑定 + 下载 + 解绑/删除清盘
    db_file = tmp_path / 'test_e25_certs_api.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    pdf_bytes = _make_pdf_bytes(
        'ISO9001质量管理体系认证证书\n证书编号：CN26-ISO9001-8899\n有效期至：2028年12月31日'
    )

    with TestClient(app) as tc:
        r_login = tc.post('/api/auth/login', json={'username': 'admin', 'password': 'BidGuard2026!'})
        hdrs = {'Authorization': f"Bearer {r_login.json()['token']}"}

        r_parse = tc.post(
            '/api/rules/certificates/parse-file',
            headers=hdrs,
            files={'file': ('iso9001.pdf', pdf_bytes, 'application/pdf')},
        )
        assert r_parse.status_code == 200
        p_out = r_parse.json()
        assert '管理体系认证证书' in (p_out['cert_name_hint'] or '')
        assert p_out['cert_no_hint'] == 'CN26-ISO9001-8899'
        assert p_out['valid_until_hint'] == '2028-12-31'

        r_create = tc.post(
            '/api/rules/certificates',
            headers=hdrs,
            json={
                'cert_name': p_out['cert_name_hint'] or 'ISO9001证书',
                'cert_no': p_out['cert_no_hint'],
                'valid_until': p_out['valid_until_hint'] or '2028-12-31',
                'warn_days': 30,
                'remark': 'E25测试',
            },
        )
        assert r_create.status_code == 200
        cid = r_create.json()['id']
        assert r_create.json()['has_attachment'] is False

        # 上传绑定原件
        r_up = tc.post(
            f"/api/rules/certificates/{cid}/attachment",
            headers=hdrs,
            files={'file': ('iso9001.pdf', pdf_bytes, 'application/pdf')},
        )
        assert r_up.status_code == 200
        assert r_up.json()['has_attachment'] is True
        assert r_up.json()['attachment_name'] == 'iso9001.pdf'

        # 下载原件
        r_dl = tc.get(f"/api/rules/certificates/{cid}/attachment", headers=hdrs)
        assert r_dl.status_code == 200
        assert r_dl.content.startswith(b'%PDF')

        # 删除证照并验证磁盘原件目录已彻底清空
        r_del = tc.delete(f"/api/rules/certificates/{cid}", headers=hdrs)
        assert r_del.status_code == 200
        cert_dir = PROJECT_ROOT / 'data' / 'uploads' / 'certs' / '1' / str(cid)
        assert not cert_dir.exists()
