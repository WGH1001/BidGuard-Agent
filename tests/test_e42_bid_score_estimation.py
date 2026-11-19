# -*- coding: utf-8 -*-
"""E42 · 标书评分预估与提分建议卡全链路验收测试。

覆盖 E42 §3 全部验收标准：
1. Decimal 评分钳位（越界分数防溢出、0.01 守恒汇总）、价格分诚实置 None（pending_open_bid）、
   客观“每不满足 1 项扣 N 分”精确扣减、模糊扣分规则诚实标 need_llm（ambiguous_deduction_rule）、
   以及分值合计≠100 时的 score_scale_warning 告警；
2. 一票否决先导联动（F3 证照过期 + E43 `source_module='BLIND_BID'` / `BLIND_BID_*` 暗标违规）
   与 verify_quote 溯源拦截（虚假摘录清空置 quote_verified=False）、client=None 0-Token 降级与单条异常降级；
3. Word 诊断报告导出（封面与页眉强标注「AI 质量自评预估（非最终评委实际打分）」免责声明）；
4. API 全链路（GET/POST `/api/checks/{id}/score-estimate`、GET `/export`、404 未生成拦截、
   409 CHECK_TASK_RUNNING、重启遗留 running 懒恢复、多租户 404 隔离、老库 score_estimate_json 平滑加列且保持 14 表）；
5. 实弹探针（只读对账 `data/corpus/sim_bids/` 与 `m0_samples/`：表格型 .docx 与段落/PDF 型标书各 1 对全流程跑通）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

from docx import Document
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, inspect

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.main import create_app
from backend.models import Base, BidCheckTask, BidFinding, ensure_bid_check_tasks_e42_columns
from core.bid_scorer import (
    SCORE_DISCLAIMER_TEXT,
    collect_scoring_criteria_items,
    estimate_bid_score,
    export_score_estimate_docx,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _login_headers(client: TestClient, username: str = 'admin', password: str = 'BidGuard2026!') -> dict[str, str]:
    resp = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert resp.status_code == 200, resp.text
    return {'Authorization': f"Bearer {resp.json()['token']}"}


def _build_scoring_tender_docx(path: Path) -> Path:
    doc = Document()
    doc.add_heading('智慧政务云平台采购项目 招标文件', level=1)
    doc.add_paragraph('第五章 评标办法与评分细则（满分 100 分）')
    tbl = doc.add_table(rows=6, cols=3)
    tbl.style = 'Table Grid'
    rows = [
        ['评分因素', '评分标准', '分值'],
        ['投标报价得分', '采用低价优先法计算评标基准价，投标报价得分=（评标基准价/投标报价）×30。', '30'],
        ['技术参数响应情况', '技术参数完全满足得满分；每有一项不满足扣 2 分，扣完为止。', '20'],
        ['商务偏离考核', '视偏离严重程度酌情扣分。', '10'],
        ['体系认证证书', '具有有效的 ISO9001、ISO27001 认证证书得满分。', '10'],
        ['总体技术架构方案', '架构设计先进、高可用容灾机制完善、模块划分清晰得 0-15 分。', '15'],
    ]
    for r_i, r_vals in enumerate(rows):
        for c_i, val in enumerate(r_vals):
            tbl.rows[r_i].cells[c_i].text = val
    doc.save(str(path))
    return path


def _build_sample_bid_docx(path: Path) -> Path:
    doc = Document()
    doc.add_heading('智慧政务云平台采购项目 投标文件', level=1)
    doc.add_paragraph('我方已持有最新有效的 ISO9001 质量管理体系认证证书，但暂未办理其他安全证书。')
    doc.add_paragraph(
        '总体技术架构方案：本项目采用微服务云原生双活高可用容灾架构，支持秒级故障自动切换与全链路监控告警。'
    )
    doc.save(str(path))
    return path


# ── 1. Decimal 钳位、价格分 pending_open_bid、客观扣分、模糊规则 need_llm 与溯源核验 ──

def test_e42_decimal_clamping_price_pending_and_scale_warning(tmp_path: Path):
    t_path = _build_scoring_tender_docx(tmp_path / 'tender_score.docx')
    b_path = _build_sample_bid_docx(tmp_path / 'bid_score.docx')

    # Mock LLM：故意返回超满分 99.99 分 + 1 条真实逐字摘录 + 1 条编造摘录
    class _OverflowMockLLM:
        def chat(self, messages, model_tier='flash', **_kwargs):
            return SimpleNamespace(content=json.dumps({
                'estimated_score': 99.99,
                'deduction_reasons': ['架构图细节可进一步丰富'],
                'improvement_suggestions': ['补充双活容灾拓扑图'],
                'evidence_quotes': [
                    '本项目采用微服务云原生双活高可用容灾架构，支持秒级故障自动切换',
                    '这是一句投标书里根本不存在的虚假编造引文99999',
                ],
            }, ensure_ascii=False))

    f4_findings = [
        {'source_module': 'F4', 'code': 'MISSING_RESPONSE', 'severity': 'warning', 'message': '漏应答1'},
        {'source_module': 'F4', 'code': 'CONTRADICTION', 'severity': 'error', 'consequence': 'deduction', 'message': '负偏离2'},
        {'source_module': 'F4', 'code': 'MISSING_EVIDENCE', 'severity': 'warning', 'message': '缺证明3'},
    ]

    res = estimate_bid_score(
        t_path,
        b_path,
        f2_res={'status': 'OK', 'findings': []},
        f4_findings=f4_findings,
        client=_OverflowMockLLM(),
    )

    assert res['status'] == 'completed'
    assert res['disclaimer'] == SCORE_DISCLAIMER_TEXT
    # 表内 5 项合计 30+20+10+10+15 = 85.00 != 100.00 -> 必须触发 score_scale_warning
    assert res['total_declared_max_score'] == '100.00'
    assert res['items_sum_max_score'] == '85.00'
    assert res['score_scale_warning'] and '85.00' in res['score_scale_warning']

    by_factor = {it['factor']: it for it in res['items']}

    # (a) 价格分：严禁捏造分数，置 None + pending_open_bid
    price_it = by_factor['投标报价得分']
    assert price_it['status'] == 'pending_open_bid'
    assert price_it['estimated_score'] is None
    assert res['price_pending_max_score'] == '30.00'

    # (b) 客观扣分项：满分 20，每项不满足扣 2 分 × 3 项 = 扣 6 分 -> 14.00
    ded_it = by_factor['技术参数响应情况']
    assert ded_it['status'] == 'evaluated'
    assert ded_it['estimated_score'] == '14.00'

    # (c) 模糊扣分项：“视偏离严重程度酌情扣分” -> 诚实标 need_llm (ambiguous_deduction_rule)
    amb_it = by_factor['商务偏离考核']
    assert amb_it['status'] == 'need_llm'
    assert amb_it['status_reason'] == 'ambiguous_deduction_rule'
    assert amb_it['estimated_score'] is None

    # (d) 证书客观项：要求 ISO9001 + ISO27001，投标书仅有 ISO9001 -> 5.00 / 10.00
    cert_it = by_factor['体系认证证书']
    assert cert_it['status'] == 'evaluated'
    assert cert_it['estimated_score'] == '5.00'

    # (e) 主观项：LLM 返回 99.99 被钳位至满分 15.00；真引文 verified=True，假引文清空且 verified=False
    sub_it = by_factor['总体技术架构方案']
    assert sub_it['status'] == 'evaluated'
    assert sub_it['estimated_score'] == '15.00'
    quotes = sub_it['evidence_quotes']
    assert any(q['quote_verified'] is True and '双活高可用容灾架构' in q['quote'] for q in quotes)
    assert any(q['quote_verified'] is False and q['quote'] == '' for q in quotes)

    # 已评满分 = 20 + 10 + 15 = 45.00；已评预估分 = 14 + 5 + 15 = 34.00
    assert res['assessed_max_score'] == '45.00'
    assert res['assessed_estimated_score'] == '34.00'
    assert res['need_llm_max_score'] == '10.00'
    assert len(res['top_priorities']) >= 2


# ── 2. 一票否决联动（含 E43 BLIND_BID_*）、0-Token/异常降级与 Word 免责声明导出 ──

def test_e42_veto_alerts_e43_blind_bid_zero_token_and_docx_export(tmp_path: Path):
    t_path = _build_scoring_tender_docx(tmp_path / 't2.docx')
    b_path = _build_sample_bid_docx(tmp_path / 'b2.docx')

    f3_findings = [
        {
            'source_module': 'F3',
            'code': 'EXPIRED_CERT',
            'severity': 'error',
            'consequence': 'veto',
            'clause_no': '资格审查',
            'message': 'ISO9001 证书已过期',
            'suggestion': '更换新证',
        }
    ]
    blind_findings = [
        {
            'source_module': 'BLIND_BID',
            'code': 'BLIND_BID_STYLE_VIOLATION',
            'severity': 'error',
            'consequence': 'veto',
            'clause_no': '暗标特殊样式核查',
            'message': '暗标检测到表格底纹 w:shd 违规',
            'suggestion': '切换按暗标素颜导出',
        }
    ]

    # 2a) client=None 0-Token 离线降级模式
    res_zero = estimate_bid_score(
        t_path,
        b_path,
        f3_findings=f3_findings,
        blind_bid_findings=blind_findings,
        client=None,
    )
    assert res_zero['llm_status'] == 'skipped'
    assert res_zero['has_veto_risk'] is True
    veto_mods = {v['source_module'] for v in res_zero['veto_alerts']}
    assert 'F3' in veto_mods and 'BLIND_BID' in veto_mods
    sub_zero = next(it for it in res_zero['items'] if it['factor'] == '总体技术架构方案')
    assert sub_zero['status'] == 'need_llm'
    assert sub_zero['status_reason'] == 'no_llm_client'
    assert sub_zero['estimated_score'] is None

    # 2b) 单条模型调用抛错时降级为 need_llm (llm_call_failed)，不阻断整份报告
    class _BrokenLLM:
        def chat(self, messages, model_tier='flash', **_kwargs):
            raise RuntimeError('模拟网关超时')

    res_broken = estimate_bid_score(t_path, b_path, client=_BrokenLLM())
    sub_broken = next(it for it in res_broken['items'] if it['factor'] == '总体技术架构方案')
    assert sub_broken['status'] == 'need_llm'
    assert sub_broken['status_reason'] == 'llm_call_failed'

    # 2c) 导出 Word 诊断报告：封面正文与页眉均须含「AI 质量自评预估（非最终评委实际打分）」
    out_docx = export_score_estimate_docx(res_zero, tmp_path / 'score_report.docx', task_title='政务云测试标')
    assert out_docx.is_file()
    doc = Document(str(out_docx))
    body_text = '\n'.join(p.text for p in doc.paragraphs)
    header_text = '\n'.join(p.text for sec in doc.sections for p in sec.header.paragraphs)
    assert SCORE_DISCLAIMER_TEXT in body_text
    assert SCORE_DISCLAIMER_TEXT in header_text


# ── 3. 老库平滑迁移（14 表守恒）与 API 端点（POST/GET/export/404/409/多租户隔离） ──

def test_e42_api_score_estimate_lifecycle_and_column_migration(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')

    # 3a) 模拟缺少 score_estimate_json 的老 SQLite 库并验证平滑加列
    legacy_db = tmp_path / 'legacy_e42.db'
    conn = sqlite3.connect(str(legacy_db))
    conn.execute(
        "CREATE TABLE bid_check_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id INTEGER, title VARCHAR(256), status VARCHAR(32))"
    )
    conn.commit()
    conn.close()

    eng_legacy = create_engine(f"sqlite:///{legacy_db.as_posix()}")
    added = ensure_bid_check_tasks_e42_columns(eng_legacy)
    assert 'score_estimate_json' in added
    cols = {c['name'] for c in inspect(eng_legacy).get_columns('bid_check_tasks')}
    assert 'score_estimate_json' in cols

    # 3b) FastAPI 全链路验证
    api_db = tmp_path / 'api_e42.db'
    db_url = f"sqlite:///{api_db.as_posix()}"
    import backend.database as db_mod

    t_path = _build_scoring_tender_docx(tmp_path / 'api_t.docx')
    b_path = _build_sample_bid_docx(tmp_path / 'api_b.docx')

    app = create_app(database_url=db_url)
    assert len(Base.metadata.tables) == 14

    with TestClient(app) as tc:
        admin_headers = _login_headers(tc, 'admin', 'BidGuard2026!')

        # 创建体检任务（不自动跑流水线）
        r_create = tc.post(
            '/api/checks',
            headers=admin_headers,
            files={
                'tender_file': ('api_t.docx', t_path.read_bytes(), 'application/octet-stream'),
                'bid_file': ('api_b.docx', b_path.read_bytes(), 'application/octet-stream'),
            },
            data={'title': 'E42接口测试任务', 'auto_run': 'false'},
        )
        assert r_create.status_code == 200, r_create.text
        task_id = r_create.json()['id']

        try:
            # 尚未发起评分预估时：GET 返回 not_started，export 返回 404 SCORE_ESTIMATE_NOT_FOUND
            r_get0 = tc.get(f"/api/checks/{task_id}/score-estimate", headers=admin_headers)
            assert r_get0.status_code == 200
            assert r_get0.json()['status'] == 'not_started'
            assert r_get0.json()['disclaimer'] == SCORE_DISCLAIMER_TEXT

            r_exp0 = tc.get(f"/api/checks/{task_id}/score-estimate/export", headers=admin_headers)
            assert r_exp0.status_code == 404
            assert r_exp0.json()['detail']['code'] == 'SCORE_ESTIMATE_NOT_FOUND'

            # 模拟任务正处于 checking 状态 + 注入一条 E43 BLIND_BID 发现
            sess = db_mod.SessionLocal()
            try:
                t_row = sess.query(BidCheckTask).filter_by(id=task_id).first()
                assert t_row is not None
                t_row.status = 'checking'
                sess.add(
                    BidFinding(
                        task_id=task_id,
                        source_module='BLIND_BID',
                        code='BLIND_BID_IDENTITY_LEAK',
                        severity='error',
                        category='暗标防废标核查',
                        clause_no='暗标合规核查',
                        message='暗标泄露投标人名称',
                        suggestion='开启暗标模式脱敏',
                        consequence='veto',
                    )
                )
                sess.commit()
            finally:
                sess.close()

            # 主任务运行中触发评分预估 -> 409 CHECK_TASK_RUNNING
            r_conflict = tc.post(f"/api/checks/{task_id}/score-estimate", headers=admin_headers)
            assert r_conflict.status_code == 409
            assert r_conflict.json()['detail']['code'] == 'CHECK_TASK_RUNNING'

            # 模拟进程重启遗留 running 状态 -> GET 自动懒恢复为 failed
            sess = db_mod.SessionLocal()
            try:
                t_row = sess.query(BidCheckTask).filter_by(id=task_id).first()
                assert t_row is not None
                t_row.status = 'delivered'
                t_row.score_estimate_json = {'status': 'running', 'progress': 35, 'stage_detail': '旧进程遗留'}
                sess.commit()
            finally:
                sess.close()

            r_stale = tc.get(f"/api/checks/{task_id}/score-estimate", headers=admin_headers)
            assert r_stale.status_code == 200
            assert r_stale.json()['status'] == 'failed'
            assert '重启' in (r_stale.json()['error_msg'] or '')

            # 同步执行评分预估（async_run=false）
            r_post = tc.post(
                f"/api/checks/{task_id}/score-estimate",
                params={'async_run': 'false'},
                headers=admin_headers,
            )
            assert r_post.status_code == 200, r_post.text
            body = r_post.json()
            assert body['status'] == 'completed'
            assert body['disclaimer'] == SCORE_DISCLAIMER_TEXT
            assert body['has_veto_risk'] is True
            assert any(v['source_module'] == 'BLIND_BID' and v['code'] == 'BLIND_BID_IDENTITY_LEAK' for v in body['veto_alerts'])
            assert body['price_pending_max_score'] == '30.00'
            assert len(body['items']) == 5

            # 导出诊断报告 .docx
            r_exp = tc.get(f"/api/checks/{task_id}/score-estimate/export", headers=admin_headers)
            assert r_exp.status_code == 200
            exp_docx = tmp_path / 'exported_api_score.docx'
            exp_docx.write_bytes(r_exp.content)
            doc = Document(str(exp_docx))
            assert any(SCORE_DISCLAIMER_TEXT in p.text for p in doc.paragraphs)
        finally:
            tc.delete(f"/api/checks/{task_id}", headers=admin_headers)


# ── 4. 实弹探针：m0_samples 真招标书 + sim_bids 仿真投标书（表格型 .docx + 段落型 .pdf） ──

def test_e42_live_probe_sim_bids_table_and_prose_pdf_tenders(tmp_path: Path):
    m0_dir = PROJECT_ROOT / 'data' / 'corpus' / 'm0_samples'
    sim_dir = PROJECT_ROOT / 'data' / 'corpus' / 'sim_bids'

    docx_tender = m0_dir / 'JSZC-320192-JZCG-G2026-0053_南京医科大四院数智病理信息系统.docx'
    docx_bid = sim_dir / '投标文件_南京医科大四院数智病理.docx'

    pdf_tender = m0_dir / 'BZSJ2026CG193_亳州学院智慧校园低代码平台软件开发.pdf'
    pdf_bid = sim_dir / '投标文件_亳州学院智慧校园低代码平台软件开发.docx'

    if not (docx_tender.is_file() and docx_bid.is_file() and pdf_tender.is_file() and pdf_bid.is_file()):
        pytest.skip('本地真语料或 sim_bids 不完整，跳过实弹探针')

    # 4a) 表格型 .docx 招标书 + 仿真投标书全流程
    res_docx = estimate_bid_score(docx_tender, docx_bid, client=None)
    assert res_docx['status'] == 'completed'
    assert res_docx['disclaimer'] == SCORE_DISCLAIMER_TEXT
    assert len(res_docx['items']) >= 4
    assert any(it['status'] == 'pending_open_bid' for it in res_docx['items'])
    out_a = export_score_estimate_docx(res_docx, tmp_path / 'probe_nanjing.docx', task_title='南京病理实弹探针')
    assert out_a.is_file()

    # 4b) 段落/PDF 型招标书 + 仿真投标书全流程（验证 include_price=True 与 PDF 评分项提取）
    collected_pdf = collect_scoring_criteria_items(pdf_tender)
    assert len(collected_pdf['criteria']) >= 3
    res_pdf = estimate_bid_score(pdf_tender, pdf_bid, client=None)
    assert res_pdf['status'] == 'completed'
    assert res_pdf['disclaimer'] == SCORE_DISCLAIMER_TEXT
    assert len(res_pdf['items']) >= 3
    out_b = export_score_estimate_docx(res_pdf, tmp_path / 'probe_bozhou.docx', task_title='亳州学院PDF实弹探针')
    assert out_b.is_file()
