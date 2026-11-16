# -*- coding: utf-8 -*-
"""E31 · F7 算术沙盒容错、ReAct 工具异常自愈与体检任务重命名卡 专项验收测试（0 Token 离线可跑）。

覆盖任务卡 §3 全部 4 组验收标准：
1. test_e31_price_sandbox_formatted_strings_and_invalid_format_degradation：
   - 逗号/单位/万元/量词安全解析（"1,400,000元"、"140万元"、"400万元"、"1套"）；
   - 占位符与非法字符串返回结构化 INVALID_PRICE_FORMAT，不抛 InvalidOperation；
   - qty=1 单价缺合价自动补齐后仍可准确检出与总价的 5000 元差额；
2. test_e31_recalc_price_aliases_derived_flag_and_react_tool_self_healing：
   - recalc_price 兼容中文/英文字段别名；qty=1 单双价自动互补必须标注 derived / derived_fields / derived_warning；
   - ask_bid_expert_stream 端到端走通「限价 400 万 + 总价 140 万 + 分项一致性」；
   - 单工具异常转为 observation.error 交回模型下一轮自修正，BaseException 取消信号不吞；
3. test_e31_qa_error_classification_gateway_vs_execution：
   - /ask 与 /ask/stream 精准区分 gateway(502 QA_GATEWAY_ERROR) 与 tool_or_internal(500 QA_EXECUTION_ERROR)；
4. test_e31_check_task_rename_and_qa_session_sync：
   - PATCH /api/checks/{id} 改名、空标题 422、跨租户/不存在 404、联动默认 F7 会话标题、保留自定义会话名、审计留痕。
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
import backend.routers.qa as qa_router_mod
import core.bid_react_agent as react_mod
from backend.main import create_app
from backend.models import AuditLog, BidCheckTask, QaSession, Tenant
from core.price_sandbox import PriceSandbox, amount_to_upper


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


# ── 1. PriceSandbox 格式化金额解析与非法串结构化降级 ─────────────────────────

def test_e31_price_sandbox_formatted_strings_and_invalid_format_degradation():
    assert amount_to_upper('140万元') == '壹佰肆拾万元整'
    assert amount_to_upper('￥1,400,000.00元整') == '壹佰肆拾万元整'
    with pytest.raises(ValueError, match='无法将金额'):
        amount_to_upper('abc')

    # 大模型从 OCR/标书摘录的带单位与逗号原始串应当直接通过四大核验
    sb_ok = PriceSandbox(
        items=[
            {'name': '核心平台', 'qty': '1套', 'unit_price': '1,000,000元', 'total_price': '100万元'},
            {'name': '定制开发', 'qty': '2人月', 'unit_price': '20万元', 'total_price': '400,000.00元'},
        ],
        table_total='-',
        bid_total='140万元',
        bid_total_upper='人民币壹佰肆拾万元正',
        price_cap='400万元',
    )
    assert sb_ok.run() == []

    # 非法格式字符串严禁抛 InvalidOperation/ConversionSyntax，必须转为 INVALID_PRICE_FORMAT finding
    sb_bad = PriceSandbox(
        items=[
            {'name': '坏格式项', 'qty': '1套', 'unit_price': '待协商', 'total_price': 'abc'},
            {'name': '好格式项但算术错', 'qty': 2, 'unit_price': '100元', 'total_price': '201元'},
        ],
        table_total='201元',
        bid_total='not-a-number',
        price_cap='bad-cap',
    )
    findings = sb_bad.run()
    bad_codes = [f['code'] for f in findings]
    assert 'INVALID_PRICE_FORMAT' in bad_codes
    assert 'ITEM_ARITHMETIC' in bad_codes
    bad_msgs = ' | '.join(f['message'] for f in findings if f['code'] == 'INVALID_PRICE_FORMAT')
    assert '坏格式项' in bad_msgs
    assert 'not-a-number' in bad_msgs
    assert 'bad-cap' in bad_msgs

    # 兼容 Decimal.normalize() 产出的科学计数法数量串（如 300000 -> '3E+5'）
    sb_sci = PriceSandbox(
        items=[
            {'name': '档案整理', 'qty': '3E+5', 'unit_price': '0.35', 'total_price': '105,000.00'},
            {'name': '扫描加工', 'qty': '1.74E+6', 'unit_price': '0.33', 'total_price': '574,200.00'},
        ],
        table_total='679,200.00',
        bid_total='679,200.00元',
    )
    assert sb_sci.run() == []

    # 回归场景：单价 139.5 万元（qty=1 自动补合价）vs 总价 140 万元 -> 必须准确检出 5000 元差额
    res_diff = react_mod.recalc_price(
        items=[{'name': '主系统', 'qty': '1套', 'unit_price': '139.5万元'}],
        bid_total='140万元',
        price_cap='400万元',
    )
    assert res_diff['passed'] is False
    assert res_diff.get('derived') is True
    assert any(f['code'] == 'TOTAL_MISMATCH' and f['diff'] == '-5000.00' for f in res_diff['findings'])


# ── 2. recalc_price 别名/derived 标注 + ReAct 单工具异常自愈 ────────────────

def test_e31_recalc_price_aliases_derived_flag_and_react_tool_self_healing(tmp_path, monkeypatch):
    # (a) 别名与 qty=1 单双价自动互补必须带 derived 标注
    res_derived = react_mod.recalc_price(
        items=[
            {'名称': '工业物性数据平台', '数量': '1套', 'price': '1,400,000元'},
        ],
        bid_total='140万元',
        bid_total_upper='壹佰肆拾万元整',
        price_cap='400万元',
    )
    assert res_derived['passed'] is True
    assert res_derived['findings'] == []
    assert res_derived['sum_of_items'] == '1400000.00'
    assert res_derived['item_count'] == 1
    assert res_derived['derived'] is True
    assert len(res_derived['derived_fields']) == 1
    assert res_derived['derived_fields'][0]['derived_field'] == 'total_price'
    assert '推算' in res_derived['derived_warning']

    # 只传合价时（qty=1）自动反推 unit_price 同样标注 derived
    res_derived_unit = react_mod.recalc_price(
        items=[{'name': '数据库软件', 'qty': '1项', 'amount': '140万元'}],
        bid_total='1,400,000元',
    )
    assert res_derived_unit['passed'] is True
    assert res_derived_unit['derived'] is True
    assert res_derived_unit['derived_fields'][0]['derived_field'] == 'unit_price'

    # (b) 端到端 ReAct 流：首步调用抛异常的工具 -> observation 返回 error -> 模型下一轮自修正带单位调用 recalc_price 收口
    tender_p = _make_docx_file(
        tmp_path / 'tender_e31.docx',
        ['第一章 招标公告', '本项目最高限价为400万元。'],
    )
    bid_p = _make_docx_file(
        tmp_path / 'bid_e31.docx',
        ['第一章 开标一览表', '投标总报价：140万元（大写：壹佰肆拾万元整）。', '核心软件 1套 单价1,400,000元 合价1,400,000元'],
    )

    recalc_calls = [0]
    orig_recalc = react_mod.recalc_price

    def _flaky_recalc(*args, **kwargs):
        recalc_calls[0] += 1
        if recalc_calls[0] == 1:
            raise RuntimeError('simulated unexpected parser glitch')
        return orig_recalc(*args, **kwargs)

    monkeypatch.setattr(react_mod, 'recalc_price', _flaky_recalc)

    class _SelfHealingLLMClient:
        def __init__(self):
            self.round = 0

        def chat(self, messages, model_tier='flash', **kwargs):
            self.round += 1
            if self.round == 1:
                payload = {
                    'thought': '先续检招标文件限价（故意传带字中文偏移测 _safe_int_offset）',
                    'action': 'search_tender',
                    'action_input': {'keyword': '最高限价', 'after_offset': '第0字'},
                }
            elif self.round == 2:
                payload = {
                    'thought': '首调 recalc_price（触发模拟工具异常，检验不崩流）',
                    'action': 'recalc_price',
                    'action_input': {
                        'items': [{'name': '核心软件', 'qty': '1套', 'unit_price': '1,400,000元', 'total_price': '1,400,000元'}],
                        'bid_total': '140万元',
                        'price_cap': '400万元',
                    },
                }
            elif self.round == 3:
                assert 'simulated unexpected parser glitch' in messages[-1]['content']
                payload = {
                    'thought': '工具上次异常已通过 observation 感知，重新用带单位字段复算 400万限价与140万总价',
                    'action': 'recalc_price',
                    'action_input': {
                        'items': [{'name': '核心软件', 'qty': '1套', 'price': '1,400,000元'}],
                        'bid_total': '140万元',
                        'price_cap': '400万元',
                    },
                }
            else:
                assert '"passed": true' in messages[-1]['content'].lower()
                payload = {
                    'thought': '复算全部通过，输出完整核验结论',
                    'action': 'final',
                    'answer': '本项目最高限价为400万元，我方投标总报价140万元未超限价，且与分项合计一致（注：单价与合价一致性已核验）。',
                    'quotes': [
                        {'source': 'tender', 'quote': '本项目最高限价为400万元'},
                        {'source': 'bid', 'quote': '投标总报价：140万元'},
                    ],
                }
            return type('Resp', (), {'content': json.dumps(payload, ensure_ascii=False)})()

    events = list(react_mod.ask_bid_expert_stream(
        tender_path=tender_p,
        bid_path=bid_p,
        question='本项目限价400万，我方投标总价140万与分项合计是否一致？',
        client=_SelfHealingLLMClient(),
        max_rounds=7,
    ))
    step_events = [e['data'] for e in events if e['type'] == 'step']
    done_events = [e['data'] for e in events if e['type'] == 'done']
    assert len(done_events) == 1
    assert recalc_calls[0] == 2
    assert 'simulated unexpected parser glitch' in str(step_events[1]['observation']['error'])
    assert step_events[2]['observation']['passed'] is True
    assert step_events[2]['observation']['derived'] is True
    assert '140万元' in done_events[0]['answer']

    # (c) BaseException（如中断信号）必须穿透工具隔离块，严禁被 except Exception 吞掉
    class _CancelBaseExc(BaseException):
        pass

    def _cancel_recalc(*args, **kwargs):
        raise _CancelBaseExc('cancel signal')

    monkeypatch.setattr(react_mod, 'recalc_price', _cancel_recalc)

    class _TriggerOneToolLLM:
        def chat(self, *args, **kwargs):
            return type('Resp', (), {
                'content': json.dumps({'thought': 't', 'action': 'recalc_price', 'action_input': {'items': [{'name': 'A', 'qty': 1, 'unit_price': 10, 'total_price': 10}]}}, ensure_ascii=False)
            })()

    with pytest.raises(_CancelBaseExc):
        list(react_mod.ask_bid_expert_stream(
            tender_path=tender_p,
            bid_path=bid_p,
            question='测试取消穿透',
            client=_TriggerOneToolLLM(),
        ))


# ── 3. 前后端错误归类解耦：网关(502) vs 工具/内部异常(500) ────────────────────

def test_e31_qa_error_classification_gateway_vs_execution(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'e31_qa_err.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    t_bytes = _make_docx_bytes(['第一章 招标', '最高限价：400万元。'])
    b_bytes = _make_docx_bytes(['第一章 投标', '投标总价：140万元。'])

    with TestClient(app) as tc:
        r_login = tc.post('/api/auth/login', json={'username': 'admin', 'password': 'BidGuard2026!'})
        hdrs = {'Authorization': f"Bearer {r_login.json()['token']}"}

        r_check = tc.post(
            '/api/checks',
            headers=hdrs,
            files={
                'tender_file': ('t.docx', t_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                'bid_file': ('b.docx', b_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
            },
            data={'title': 'QA错误归类测试', 'auto_run': 'false'},
        )
        assert r_check.status_code == 200
        tid = r_check.json()['id']

        r_sess = tc.post('/api/qa/sessions', headers=hdrs, json={'task_id': tid})
        assert r_sess.status_code == 200
        sid = r_sess.json()['id']

        try:
            monkeypatch.setattr(qa_router_mod, 'resolve_effective_llm_client', lambda _db, _uid: (object(), 'active'))

            # 场景 A：内部工具/解析抛出 ValueError -> 500 QA_EXECUTION_ERROR（绝不甩锅网关或提 TUN）
            def _raise_tool_value_err(*args, **kwargs):
                raise ValueError("Invalid decimal syntax in recalc")

            monkeypatch.setattr(qa_router_mod, 'ask_bid_expert', _raise_tool_value_err)
            monkeypatch.setattr(qa_router_mod, 'ask_bid_expert_stream', _raise_tool_value_err)

            r_ask_500 = tc.post(f"/api/qa/sessions/{sid}/ask", headers=hdrs, json={'question': '报价核验'})
            assert r_ask_500.status_code == 500
            d500 = r_ask_500.json()['detail']
            assert d500['code'] == 'QA_EXECUTION_ERROR'
            assert d500['error_type'] == 'tool_or_internal'
            assert 'TUN' not in d500['message']

            r_stream_tool_err = tc.post(f"/api/qa/sessions/{sid}/ask/stream", headers=hdrs, json={'question': '报价核验'})
            assert r_stream_tool_err.status_code == 200
            assert '"error_type": "tool_or_internal"' in r_stream_tool_err.text
            assert 'ValueError' in r_stream_tool_err.text

            # 场景 B：真实网关/网络超时异常 -> 保持 502 QA_GATEWAY_ERROR + error_type='gateway'
            def _raise_gateway_timeout(*args, **kwargs):
                raise TimeoutError("upstream gateway timed out")

            monkeypatch.setattr(qa_router_mod, 'ask_bid_expert', _raise_gateway_timeout)
            monkeypatch.setattr(qa_router_mod, 'ask_bid_expert_stream', _raise_gateway_timeout)

            r_ask_502 = tc.post(f"/api/qa/sessions/{sid}/ask", headers=hdrs, json={'question': '报价核验'})
            assert r_ask_502.status_code == 502
            d502 = r_ask_502.json()['detail']
            assert d502['code'] == 'QA_GATEWAY_ERROR'
            assert d502['error_type'] == 'gateway'

            r_stream_gw_err = tc.post(f"/api/qa/sessions/{sid}/ask/stream", headers=hdrs, json={'question': '报价核验'})
            assert r_stream_gw_err.status_code == 200
            assert '"error_type": "gateway"' in r_stream_gw_err.text
        finally:
            tc.delete(f"/api/checks/{tid}", headers=hdrs)


# ── 4. 体检任务重命名闭环（Q1）与联动默认 F7 会话标题 ─────────────────────────

def test_e31_check_task_rename_and_qa_session_sync(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'e31_rename_task.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    t_bytes = _make_docx_bytes(['第一章 招标', '★1. 质保期不少于12个月。'])
    b_bytes = _make_docx_bytes(['第一章 投标', '★1. 质保期不少于12个月。'])

    with TestClient(app) as tc:
        r_login = tc.post('/api/auth/login', json={'username': 'admin', 'password': 'BidGuard2026!'})
        hdrs = {'Authorization': f"Bearer {r_login.json()['token']}"}

        # 不传 title 时默认取投标文件名去后缀
        r_create = tc.post(
            '/api/checks',
            headers=hdrs,
            files={
                'tender_file': ('tender_04.docx', t_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                'bid_file': ('安易—04包-工业物性数据应用开发平台.docx', b_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
            },
            data={'auto_run': 'false'},
        )
        assert r_create.status_code == 200
        tid = r_create.json()['id']
        old_title = r_create.json()['title']
        assert old_title == '安易—04包-工业物性数据应用开发平台'

        # 建立 1 个默认命名会话 + 1 个用户人工命名会话
        r_s_default = tc.post('/api/qa/sessions', headers=hdrs, json={'task_id': tid})
        r_s_custom = tc.post('/api/qa/sessions', headers=hdrs, json={'task_id': tid, 'title': '报价复算专项'})
        assert r_s_default.status_code == 200 and r_s_custom.status_code == 200
        default_sid = r_s_default.json()['id']
        custom_sid = r_s_custom.json()['id']
        assert r_s_default.json()['title'] == f"{old_title} · 专家追问"

        try:
            # 正常重命名（带首尾空格自动裁剪）
            r_rename = tc.patch(f"/api/checks/{tid}", headers=hdrs, json={'title': '  安易—04包  '})
            assert r_rename.status_code == 200
            assert r_rename.json()['title'] == '安易—04包'

            # 校验联动效果与审计留痕
            with db_mod.SessionLocal() as sess:
                t_row = sess.query(BidCheckTask).filter_by(id=tid).first()
                assert t_row is not None and t_row.title == '安易—04包'

                qs_def = sess.query(QaSession).filter_by(id=default_sid).first()
                qs_cus = sess.query(QaSession).filter_by(id=custom_sid).first()
                assert qs_def is not None and qs_def.title == '安易—04包 · 专家追问'
                assert qs_cus is not None and qs_cus.title == '报价复算专项'

                audit = (
                    sess.query(AuditLog)
                    .filter_by(action='rename_check_task', target=f"task:{tid}")
                    .first()
                )
                assert audit is not None
                assert audit.detail_json['old_title'] == old_title
                assert audit.detail_json['new_title'] == '安易—04包'

            # 纯空格标题返回 422 EMPTY_TASK_TITLE
            r_blank = tc.patch(f"/api/checks/{tid}", headers=hdrs, json={'title': '   '})
            assert r_blank.status_code == 422
            assert r_blank.json()['detail']['code'] == 'EMPTY_TASK_TITLE'

            # 不存在的任务 & 跨租户任务返回 404
            assert tc.patch('/api/checks/999999', headers=hdrs, json={'title': '新名'}).status_code == 404
            with db_mod.SessionLocal() as sess:
                other_t = Tenant(name='其他租户_E31', is_active=True)
                sess.add(other_t)
                sess.flush()
                other_task = BidCheckTask(
                    tenant_id=other_t.id,
                    task_type='pre_check',
                    title='他租户任务',
                    status='created',
                    tender_filename='t.docx',
                    bid_filename='b.docx',
                )
                sess.add(other_task)
                sess.commit()
                other_tid = other_task.id
            assert tc.patch(f"/api/checks/{other_tid}", headers=hdrs, json={'title': '越权改名'}).status_code == 404
        finally:
            tc.delete(f"/api/checks/{tid}", headers=hdrs)
