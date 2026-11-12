# -*- coding: utf-8 -*-
"""E23 · 开标日期全链路精准提取、大盘/F5/F7工作区扩屏与工具解包修复卡单元测试。

覆盖任务卡 §3 全部验收标准：
1. 开标日期抽取（11/11 真语料形态 + 仓内 data/corpus/m0_samples 实跑验证）：
   - 覆盖跨换行、列表序号 '1.'、'1.时间：'、PDF 页码 '\\n5\\n' 及芜湖前文资格条件与获取时间干扰排除；
   - 若 data/corpus/m0_samples/ 存在 11 份真语料，逐份实跑断言 11/11 全命中且与真值表完全一致；
2. 全链路开标日期透出与优先级：
   - worker 优先级：任务手填 > 标书原文自动提取 > 租户默认规则日期 > DEFAULT_FALLBACK_OPEN_DATE；
   - /api/extract 返回 open_date；
   - /api/radar 扫描后将 open_date 存入 task.config_json 并随 RadarScanOut 返回；
   - /api/dashboard 倒计时优先置顶有 open_date 的任务（上限 8 条），且 F6 任务不盲借 pre_check 默认规则日；
3. F7 工具参数字符串化 JSON 自动解包与 recalc_price 缺失 total_price 护栏：
   - LLM 返回字符串化 JSON action_input='{\"items\": [...]}' 时自动解包调通 recalc_price / cite_regulation；
   - recalc_price 缺失 total_price 时返回明确错误提示而非按 0 误报算术差错。
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from docx import Document
from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.database import Base, get_engine, get_session_factory
from backend.main import create_app
from backend.models import BidCheckTask, BidFinding, BidRuleConfig, Tenant
from backend.worker import BidTaskWorker
from core.bid_react_agent import _coerce_action_input, ask_bid_expert, recalc_price
from core.parser import parse_document
from core.quote_verifier import verify_quote
from core.tender_extractor import (
    extract_open_date_from_tender_text,
    extract_open_date_with_evidence,
)

ROOT = Path(__file__).resolve().parents[1]

# 11 份真实招标书语料文件名与期望开标日真值表
EXPECTED_11_CORPUS_OPEN_DATES: dict[str, str] = {
    'BZSJ2026CG193_亳州学院智慧校园低代码平台软件开发.pdf': '2026-10-29',
    'JSZC-320192-JZCG-G2026-0053_南京医科大四院数智病理信息系统.docx': '2026-11-10',
    'JSZC-320303-ZJZB-G2026-0116_徐州智慧文旅信息系统集成_采购文件.docx': '2026-09-29',
    'JSZC-320700-JZCG-G2026-0143_连云港数智公积金信息系统.docx': '2026-11-25',
    'czsjcg202610-014_滁州物业维修资金系统整合迁移.pdf': '2026-11-05',
    'czsjcg202610-032_滁州城职院数智托育数字化转型.docx': '2026-11-17',
    'JSZC-320000-SCZX-G2026-0484_江苏省科技型企业一件事软件开发.docx': '2026-10-16',
    'JSZC-320000-SCZX-G2026-0525_江苏省固废管理信息系统对标提升.docx': '2026-11-19',
    'JSZC-320213-JZCG-G2026-0013_无锡梁溪数字档案馆系统建设.docx': '2026-11-25',
    'JSZC-320214-CXZX-G2026-0016_无锡新吴自然资源规划系统国产化改造.docx': '2026-11-16',
    'WH09CG2026FW0137_芜湖无为市档案数字化加工_第一册专用部分.pdf': '2026-10-09',
}


def _make_docx_bytes(lines: list[str]) -> bytes:
    doc = Document()
    for ln in lines:
        doc.add_paragraph(ln)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ── 1. 11 种真实语料原文形态与 m0_samples 11/11 实跑测试 ──────────────────

@pytest.mark.parametrize(
    'sample_text,expected_date',
    [
        ('开标时间：2026年10月29日 09:00', '2026-10-29'),
        ('提交投标文件截止时间：2026年11月10日 09:30:00', '2026-11-10'),
        ('响应文件提交截止时间：2026年09月29日14:00', '2026-09-29'),
        ('提交投标文件截止时间：2026年11月25日09:00', '2026-11-25'),
        ('四、提交投标文件截止时间、开标时间和地点\n2026 年11 月5 日8 点30 分', '2026-11-05'),
        ('四、提交投标文件截止时间、开标时间和地点\n2026年11月17日08点30分', '2026-11-17'),
        ('四、提交投标文件截止时间、开标时间和地点\n1.2026年10月16日09时30分', '2026-10-16'),
        ('四、提交投标文件截止时间、开标时间和地点\n1.2026年11月19日09点30分', '2026-11-19'),
        ('四、提交投标文件截止时间、开标时间和地点\n1.时间：2026年11月25日09点30分', '2026-11-25'),
        ('四、提交投标文件截止时间、开标时间和地点\n1.时间：2026年11月16日13时30分', '2026-11-16'),
        (
            '二、申请人的资格要求：截止提交投标文件截止时间前未被列入失信被执行人。\n'
            '三、获取招标文件 时间：2026 年09 月12 日至2026 年09 月18 日\n'
            '四、提交投标文件截止时间、开标时间和地点\n\n5\n时间：2026 年10 月09 日09 点15 分',
            '2026-10-09',
        ),
    ],
)
def test_e23_extract_open_date_all_eleven_real_patterns(sample_text: str, expected_date: str):
    assert extract_open_date_from_tender_text(sample_text) == expected_date


def test_e23_m0_samples_corpus_eleven_out_of_eleven_hit():
    corpus_dir = ROOT / 'data' / 'corpus' / 'm0_samples'
    if not corpus_dir.is_dir():
        pytest.skip('本地未放置 data/corpus/m0_samples 语料目录')

    present_files = [
        (fname, expected)
        for fname, expected in EXPECTED_11_CORPUS_OPEN_DATES.items()
        if (corpus_dir / fname).is_file()
    ]
    if not present_files:
        pytest.skip('m0_samples 下未找到招标文件样本')

    for fname, expected in present_files:
        doc = parse_document(str(corpus_dir / fname))
        actual = extract_open_date_from_tender_text(doc.text)
        assert actual == expected, f"{fname} 期望开标日 {expected}，实际提取为 {actual}"


# ── 2. Worker 开标日优先级（手填 > 标书自动提取 > 规则页默认日）与 Extract/Radar/Dashboard 全链路 ──

def test_e23_worker_prefers_tender_extracted_date_over_default_rule_date(tmp_path, monkeypatch):
    from datetime import date as dt_date

    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'test_e23_worker_prio.db'
    eng = get_engine(f"sqlite:///{db_file.as_posix()}")
    Base.metadata.create_all(bind=eng)
    SessionMaker = get_session_factory(eng)

    t_path = tmp_path / 'tender_1016.docx'
    b_path = tmp_path / 'bid_1016.docx'
    t_path.write_bytes(
        _make_docx_bytes([
            '四、提交投标文件截止时间、开标时间和地点',
            '1.2026年10月16日09时30分',
            '★1. 免费质保期3年。',
        ])
    )
    b_path.write_bytes(_make_docx_bytes(['★1. 免费质保期3年。（完全满足）']))

    sess = SessionMaker()
    try:
        tenant = Tenant(name='E23优先级租户', is_active=True)
        sess.add(tenant)
        sess.flush()

        # 租户默认规则预置了 2026-11-05，但任务未手填 open_date -> 应优先采用招标书提取出的 2026-10-16
        rule = BidRuleConfig(
            tenant_id=tenant.id,
            name='默认规则',
            open_date=dt_date(2026, 11, 5),
            is_default=True,
        )
        task = BidCheckTask(
            tenant_id=tenant.id,
            title='标书提取优先于默认规则测试',
            tender_path=str(t_path),
            bid_path=str(b_path),
            config_json={},
        )
        sess.add_all([rule, task])
        sess.commit()
        tid = task.id
    finally:
        sess.close()

    worker = BidTaskWorker(session_factory=SessionMaker, max_workers=1)
    try:
        worker.submit_check_task(tid, sync=True)
        s_check = SessionMaker()
        try:
            row = s_check.query(BidCheckTask).filter_by(id=tid).first()
            assert row is not None
            assert row.config_json.get('open_date') == '2026-10-16'
        finally:
            s_check.close()
    finally:
        worker.shutdown(wait=True)
        eng.dispose()


def test_e23_extract_radar_and_dashboard_open_date_pipeline(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'test_e23_api.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    t_with_date = _make_docx_bytes([
        '四、提交投标文件截止时间、开标时间和地点',
        '1.时间：2026年11月25日09点30分',
        '★1. 免费质保期不少于36个月。',
    ])
    t_no_date = _make_docx_bytes(['第一章 技术要求：普通通用参数要求。'])

    with TestClient(app) as tc:
        r_login = tc.post('/api/auth/login', json={'username': 'admin', 'password': 'BidGuard2026!'})
        hdrs = {'Authorization': f"Bearer {r_login.json()['token']}"}

        # ① /api/extract 返回 open_date
        r_ext = tc.post(
            '/api/extract',
            headers=hdrs,
            files={'file': ('tender_wx.docx', t_with_date, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')},
        )
        assert r_ext.status_code == 200
        assert r_ext.json()['open_date'] == '2026-11-25'
        assert r_ext.json()['open_date_source'] == 'tender_extracted'
        assert '2026年11月25日' in str(r_ext.json()['open_date_quote'] or '')

        # ② /api/radar：有日期的招标书入库 open_date，无日期的招标书 open_date 为 None（不盲借默认规则日）
        r_rad_dated = tc.post(
            '/api/radar',
            headers=hdrs,
            files={'tender_file': ('rad_dated.docx', t_with_date, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')},
            data={'title': '有开标日雷达项目'},
        )
        assert r_rad_dated.status_code == 200
        assert r_rad_dated.json()['open_date'] == '2026-11-25'
        assert r_rad_dated.json()['open_date_source'] == 'tender_extracted'
        assert '2026年11月25日' in str(r_rad_dated.json()['open_date_quote'] or '')
        dated_tid = r_rad_dated.json()['task_id']

        r_rad_undated = tc.post(
            '/api/radar',
            headers=hdrs,
            files={'tender_file': ('rad_undated.docx', t_no_date, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')},
            data={'title': '无开标日雷达项目'},
        )
        assert r_rad_undated.status_code == 200
        assert r_rad_undated.json()['open_date'] is None
        assert r_rad_undated.json()['open_date_source'] is None
        undated_tid = r_rad_undated.json()['task_id']

        # ③ /api/dashboard：有 open_date 的项目排在无 open_date 的项目之前，且保留 task_type='radar'
        r_dash = tc.get('/api/dashboard', headers=hdrs)
        assert r_dash.status_code == 200
        alerts = r_dash.json()['countdown_alerts']
        alert_ids = [a['task_id'] for a in alerts]
        assert dated_tid in alert_ids and undated_tid in alert_ids
        assert alert_ids.index(dated_tid) < alert_ids.index(undated_tid)
        undated_alert = next(a for a in alerts if a['task_id'] == undated_tid)
        assert undated_alert['task_type'] == 'radar'
        assert undated_alert['open_date'] is None


# ── 3. F7 字符串化 JSON action_input 解包与 recalc_price 缺失 total_price 提示 ──

def test_e23_react_agent_unwraps_stringified_json_action_input_and_guards_total_price(tmp_path):
    # (a) 验证 _coerce_action_input 解包字符串化 JSON
    raw_str = '{"items": [{"name": "整理服务", "qty": "300000", "unit_price": "0.35", "total_price": "105000.00"}], "bid_total": "105000.00"}'
    coerced = _coerce_action_input(raw_str)
    assert isinstance(coerced, dict) and len(coerced['items']) == 1

    # (b) 验证缺少 total_price 时返回明确字段缺失提示，而非按 0 误算
    missing_tp_res = recalc_price([{'name': '扫描服务', 'qty': '100', 'unit_price': '2.5'}])
    assert missing_tp_res['passed'] is False
    assert '缺少 total_price' in missing_tp_res['error']

    # (c) 验证在 ask_bid_expert 中 LLM 传回字符串化 JSON action_input 时能正常调通 recalc_price
    t_path = tmp_path / 't_str.docx'
    b_path = tmp_path / 'b_str.docx'
    t_path.write_bytes(_make_docx_bytes(['★1. 预算 20 万元。']))
    b_path.write_bytes(_make_docx_bytes(['分项1 整理服务 300000页 × 0.35元 = 105000.00元。']))

    class _StringifiedInputMockClient:
        def __init__(self):
            self.turn = 0

        def chat(self, messages, model_tier='flash', reasoning_effort=None):
            self.turn += 1
            if self.turn == 1:
                return SimpleNamespace(
                    content=json.dumps({
                        'thought': '调用报价沙盒复算',
                        'action': 'recalc_price',
                        'action_input': raw_str,  # 故意传字符串化 JSON
                    })
                )
            return SimpleNamespace(
                content=json.dumps({
                    'thought': '复算通过',
                    'action': 'final',
                    'action_input': {
                        'answer': '报价算术复算完全一致（105000.00元）。',
                        'quotes': ['分项1 整理服务 300000页 × 0.35元 = 105000.00元。'],
                    },
                })
            )

    res = ask_bid_expert(t_path, b_path, '请复算报价是否正确', client=_StringifiedInputMockClient())
    assert res['mode'] == 'react'
    assert res['steps'][0]['action'] == 'recalc_price'
    assert res['steps'][0]['observation']['passed'] is True


# ── 4. 通用泛化扩充、手填日期强校验与双向 OPEN_DATE_MISMATCH 告警 ────────────

@pytest.mark.parametrize(
    'sample_text,expected_date',
    [
        ('五、截标日期：2026年12月08日 09:00', '2026-12-08'),
        ('递交响应文件截止时间：2026-12-18 14:30', '2026-12-18'),
        ('报价截止时间\n时间：2026/12/22', '2026-12-22'),
        ('四、开标时间和地点\n二〇二六年十一月五日', '2026-11-05'),
        ('开标日期：二零二六年十二月二十一日', '2026-12-21'),
    ],
)
def test_e23_generalized_open_date_synonyms_and_cn_numerals(sample_text: str, expected_date: str):
    ev = extract_open_date_with_evidence(sample_text)
    assert ev is not None
    assert ev['open_date'] == expected_date
    assert verify_quote(ev['quote'], sample_text)


@pytest.mark.parametrize('bad_date', ['abc', '2026/11/05', '2026-13-01', '2026-02-30'])
def test_e23_create_check_task_rejects_invalid_manual_open_date(tmp_path, bad_date: str):
    db_file = tmp_path / f'test_e23_bad_date_{bad_date.replace("/", "_")}.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)
    doc_bytes = _make_docx_bytes(['★1. 质保期 2 年。'])

    with TestClient(app) as tc:
        r_login = tc.post('/api/auth/login', json={'username': 'admin', 'password': 'BidGuard2026!'})
        hdrs = {'Authorization': f"Bearer {r_login.json()['token']}"}
        r_bad = tc.post(
            '/api/checks',
            headers=hdrs,
            files={
                'tender_file': ('t.docx', doc_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                'bid_file': ('b.docx', doc_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
            },
            data={'title': '非法日期拦截测试', 'open_date': bad_date},
        )
        assert r_bad.status_code == 422
        assert r_bad.json()['detail']['code'] == 'INVALID_OPEN_DATE'


def test_e23_manual_vs_tender_open_date_mismatch_warning(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'test_e23_mismatch.db'
    eng = get_engine(f"sqlite:///{db_file.as_posix()}")
    Base.metadata.create_all(bind=eng)
    SessionMaker = get_session_factory(eng)

    t_path = tmp_path / 'tender_mismatch.docx'
    b_path = tmp_path / 'bid_mismatch.docx'
    t_path.write_bytes(
        _make_docx_bytes([
            '四、提交投标文件截止时间、开标时间和地点',
            '1.时间：2026年11月25日09时30分',
            '★1. 免费质保期2年。',
        ])
    )
    b_path.write_bytes(_make_docx_bytes(['★1. 免费质保期2年。（满足）']))

    sess = SessionMaker()
    try:
        tenant = Tenant(name='E23日期冲突测试租户', is_active=True)
        sess.add(tenant)
        sess.flush()

        t_mismatch = BidCheckTask(
            tenant_id=tenant.id,
            title='手填与标书不一致任务',
            tender_path=str(t_path),
            bid_path=str(b_path),
            config_json={'open_date': '2026-12-01'},
        )
        t_match = BidCheckTask(
            tenant_id=tenant.id,
            title='手填与标书一致任务',
            tender_path=str(t_path),
            bid_path=str(b_path),
            config_json={'open_date': '2026-11-25'},
        )
        sess.add_all([t_mismatch, t_match])
        sess.commit()
        id_mismatch, id_match = t_mismatch.id, t_match.id
    finally:
        sess.close()

    worker = BidTaskWorker(session_factory=SessionMaker, max_workers=1)
    try:
        worker.submit_check_task(id_mismatch, sync=True)
        worker.submit_check_task(id_match, sync=True)

        s_check = SessionMaker()
        try:
            row_mm = s_check.query(BidCheckTask).filter_by(id=id_mismatch).first()
            assert row_mm is not None and row_mm.config_json.get('open_date') == '2026-12-01'

            mm_finding = (
                s_check.query(BidFinding)
                .filter_by(task_id=id_mismatch, code='OPEN_DATE_MISMATCH')
                .first()
            )
            assert mm_finding is not None
            assert mm_finding.severity == 'warning'
            assert mm_finding.quote_verified is True
            assert '2026-12-01' in mm_finding.message
            assert '2026-11-25' in mm_finding.message

            ok_finding = (
                s_check.query(BidFinding)
                .filter_by(task_id=id_match, code='OPEN_DATE_MISMATCH')
                .first()
            )
            assert ok_finding is None
        finally:
            s_check.close()
    finally:
        worker.shutdown(wait=True)
        eng.dispose()


# ── 5. 正则未命中时局部窗 LLM 兜底与 open_date_source 来源可观测 ────────────


def test_e23_llm_open_date_fallback_local_window_and_verify_quote(tmp_path):
    """正则未命中时局部窗 LLM 兜底：必须限窗不传全文，且严格过 verify_quote + 日期一致性双锁。"""
    import json
    from types import SimpleNamespace
    from core.tender_llm import extract_open_date_fallback_with_llm, extract_tender_with_llm

    prefix = '无关前置招标说明文本。\n' * 300
    target_clause = '第二部分 特别约定：采购人决定于二〇二六年十一月十八日上午九点整正式举行开标会，请各投标代表按时参会。'
    suffix = '\n附表规范条目详情。\n' * 400
    long_text = prefix + target_clause + suffix
    assert extract_open_date_from_tender_text(long_text) is None

    class _RecordingClient:
        def __init__(self, reply_obj: dict):
            self.reply_obj = reply_obj
            self.prompts: list[str] = []

        def chat(self, messages, model_tier='flash'):
            self.prompts.append(messages[0]['content'])
            return SimpleNamespace(content=json.dumps(self.reply_obj, ensure_ascii=False))

        def total_usage(self):
            return {'calls': len(self.prompts)}

    # (1) 合法局部窗 + 真实逐字摘录 -> 采信并限制窗口大小（绝不传整本）
    ok_client = _RecordingClient({
        'open_date': '2026-11-18',
        'quote': '决定于二〇二六年十一月十八日上午九点整正式举行开标会',
    })
    ev = extract_open_date_fallback_with_llm(long_text, ok_client)
    assert ev is not None
    assert ev['open_date'] == '2026-11-18'
    assert ev['source'] == 'tender_llm'
    assert ev['quote_verified'] is True
    assert len(ok_client.prompts) == 1
    assert len(ok_client.prompts[0]) < len(long_text)

    # (2) 编造 quote（未过 verify_quote）-> 一律拒收
    bad_quote_client = _RecordingClient({
        'open_date': '2026-11-18',
        'quote': '编造的不存在条款：开标时间2026年11月18日',
    })
    assert extract_open_date_fallback_with_llm(long_text, bad_quote_client) is None

    # (3) 借用真句但篡改日期（quote 内日期与 open_date 不一致）-> 一律拒收
    mismatch_date_client = _RecordingClient({
        'open_date': '2026-12-31',
        'quote': '决定于二〇二六年十一月十八日上午九点整正式举行开标会',
    })
    assert extract_open_date_fallback_with_llm(long_text, mismatch_date_client) is None

    # (4) 无任何招标时间线索 -> 0 Token 零调用短路
    empty_clue_client = _RecordingClient({'open_date': '2026-11-18', 'quote': '普通说明'})
    assert extract_open_date_fallback_with_llm('仅有一句纯技术说明文档内容。', empty_clue_client) is None
    assert empty_clue_client.prompts == []

    # (5) F1 主入口 open_date_fallback=True 能消费同一兜底通道
    docx_p = tmp_path / 'unusual_open_date.docx'
    docx_p.write_bytes(_make_docx_bytes([target_clause]))
    f1_client = _RecordingClient({
        'open_date': '2026-11-18',
        'quote': '决定于二〇二六年十一月十八日上午九点整正式举行开标会',
    })
    f1_res = extract_tender_with_llm(str(docx_p), client=f1_client, open_date_fallback=True)
    assert f1_res['open_date'] == '2026-11-18'
    assert f1_res['open_date_source'] == 'tender_llm'
    assert '二〇二六年十一月十八日' in str(f1_res['open_date_quote'])


def test_e23_worker_and_api_open_date_source_observability(tmp_path, monkeypatch):
    """验证 worker 运行后 config_json.open_date_source 显式落盘四种来源。"""
    from datetime import date as dt_date

    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'test_e23_sources.db'
    eng = get_engine(f"sqlite:///{db_file.as_posix()}")
    Base.metadata.create_all(bind=eng)
    SessionMaker = get_session_factory(eng)

    t_with_date = tmp_path / 'src_tender.docx'
    t_no_date = tmp_path / 'src_nodate.docx'
    bid_p = tmp_path / 'src_bid.docx'
    t_with_date.write_bytes(
        _make_docx_bytes([
            '四、提交投标文件截止时间、开标时间和地点',
            '1.时间：2026年10月16日09时30分',
            '★1. 免费质保期2年。',
        ])
    )
    t_no_date.write_bytes(_make_docx_bytes(['★1. 免费质保期2年。']))
    bid_p.write_bytes(_make_docx_bytes(['★1. 免费质保期2年。（满足）']))

    sess = SessionMaker()
    try:
        t_has_rule = Tenant(name='有规则日期租户', is_active=True)
        t_no_rule = Tenant(name='无规则日期租户', is_active=True)
        sess.add_all([t_has_rule, t_no_rule])
        sess.flush()

        sess.add(
            BidRuleConfig(
                tenant_id=t_has_rule.id,
                name='默认规则',
                is_default=True,
                open_date=dt_date(2026, 12, 28),
            )
        )

        # (a) 标书正文自动识别 -> tender_extracted
        task_det = BidCheckTask(
            tenant_id=t_has_rule.id,
            task_type='pre_check',
            title='正文提取来源',
            status='queued',
            tender_path=str(t_with_date),
            bid_path=str(bid_p),
            config_json={},
        )
        # (b) 用户手填 -> manual
        task_man = BidCheckTask(
            tenant_id=t_has_rule.id,
            task_type='pre_check',
            title='用户手填来源',
            status='queued',
            tender_path=str(t_with_date),
            bid_path=str(bid_p),
            config_json={'open_date': '2026-10-16', 'open_date_source': 'manual'},
        )
        # (c) 规则默认 -> rule_default
        task_rule = BidCheckTask(
            tenant_id=t_has_rule.id,
            task_type='pre_check',
            title='规则默认来源',
            status='queued',
            tender_path=str(t_no_date),
            bid_path=str(bid_p),
            config_json={},
        )
        # (d) 无正文且无规则 -> fallback_default
        task_fb = BidCheckTask(
            tenant_id=t_no_rule.id,
            task_type='pre_check',
            title='兜底来源',
            status='queued',
            tender_path=str(t_no_date),
            bid_path=str(bid_p),
            config_json={},
        )
        sess.add_all([task_det, task_man, task_rule, task_fb])
        sess.commit()
        ids = (task_det.id, task_man.id, task_rule.id, task_fb.id)
    finally:
        sess.close()

    worker = BidTaskWorker(session_factory=SessionMaker, max_workers=1)
    try:
        for tid in ids:
            worker.submit_check_task(tid, sync=True)

        s_chk = SessionMaker()
        try:
            r_det, r_man, r_rule, r_fb = (
                s_chk.query(BidCheckTask).filter_by(id=tid).first() for tid in ids
            )
            assert r_det.config_json.get('open_date') == '2026-10-16'
            assert r_det.config_json.get('open_date_source') == 'tender_extracted'
            assert '2026年10月16日' in str(r_det.config_json.get('open_date_quote') or '')

            assert r_man.config_json.get('open_date') == '2026-10-16'
            assert r_man.config_json.get('open_date_source') == 'manual'

            assert r_rule.config_json.get('open_date_source') == 'rule_default'
            assert r_rule.config_json.get('open_date_effective') == '2026-12-28'
            assert r_rule.config_json.get('open_date') is None

            assert r_fb.config_json.get('open_date_source') == 'fallback_default'
            assert r_fb.config_json.get('open_date_effective') == '2026-11-05'
        finally:
            s_chk.close()
    finally:
        worker.shutdown(wait=True)
        eng.dispose()

