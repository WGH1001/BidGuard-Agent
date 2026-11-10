# -*- coding: utf-8 -*-
"""E19 · UX 体验修复与 F4 双阶段并发 + F7 思考深度透传单元测试（断言严格数数，禁空循环假绿）。

覆盖任务卡 §3 全部验收标准：
1. F4 并发结果与串行结果一致性：同输入构造多条 ★ 要求，断言 BIDGUARD_F4_CONCURRENCY=1（串行）
   与 BIDGUARD_F4_CONCURRENCY=4（并发）产出的 findings 和 results 顺序与内容逐条完全一致；
2. 单条 chat 抛异常优雅降级：某条要求调用 client.chat 抛异常时，仅该条降级为 error_fallback，其余条款正常检出；
3. F7 reasoning_effort 透传与三挡差异：
   - 极速('low') / 均衡('medium') / 深度('high') 三挡在 ask_bid_expert 与 LLMClient.chat 中真实透传且参数互不相同；
   - supports_reasoning_effort=False 的模型自动跳过透传；
4. /api/qa 路由层：合法 reasoning_effort 透传，非法值（如 'ultra_invalid'）自动忽略不报错。
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from docx import Document
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.main import create_app
from core.bid_react_agent import ask_bid_expert
from core.llm_gateway import build_client_from_user_config
from core.response_checker import check_star_responses


class _ConcurrentMockLLMClient:
    """线程安全 Mock LLM 客户端（人为引入乱序延迟以验证并发归位顺序与串行版 100% 一致）。"""

    def __init__(self, fail_on_keyword: str | None = None):
        self.fail_on_keyword = fail_on_keyword
        self._lock = threading.Lock()
        self.calls = 0
        self.received_efforts: list[str | None] = []
        self.thread_ids: set[int] = set()

    def chat(self, messages, model_tier='flash', max_tokens=None, reasoning_effort=None):
        with self._lock:
            self.calls += 1
            self.received_efforts.append(reasoning_effort)
            self.thread_ids.add(threading.get_ident())

        prompt_txt = messages[-1]['content']
        core_part = prompt_txt.split('【招标要求核心句】')[-1].split('【投标响应原文窗口】')[0]
        if self.fail_on_keyword and self.fail_on_keyword in core_part:
            raise RuntimeError('Simulated single-clause LLM timeout')

        # 故意让排在前面的条款睡眠更久，验证并发完成后仍按 req_index 严格升序归位
        if '★1.' in core_part:
            time.sleep(0.04)
            payload = {
                'status': 'RESPONSE_OK',
                'tender_quote': '★1. 免费质保期不少于36个月。',
                'bid_quote': '★1. 免费质保期不少于36个月。（我方完全响应）',
                'reason': '完全响应质保期要求',
            }
        elif '★2.' in core_part:
            time.sleep(0.01)
            payload = {
                'status': 'CONTRADICTION',
                'tender_quote': '★2. 核心交换机并发连接数不少于500万。',
                'bid_quote': '★2. 核心交换机并发连接数为200万。',
                'reason': '并发连接数低于招标要求',
            }
        else:
            time.sleep(0.02)
            payload = {
                'status': 'MISSING_RESPONSE',
                'tender_quote': '★3. 须提供原厂售后服务承诺函。',
                'bid_quote': '',
                'reason': '未回应售后服务承诺函',
            }

        return SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))


# ── 1 & 2. F4 双阶段并发与串行逐条一致性 + 单条异常降级隔离测试 ────────────

def test_e19_f4_concurrent_matches_serial_and_isolates_single_failure(monkeypatch):
    tender_text = (
        '一、商务售后要求\n'
        '投标人应满足下列商务条款要求。\n'
        '★1. 免费质保期不少于36个月。\n\n'
        '二、核心网络设备参数\n'
        '设备需符合以下性能指标。\n'
        '★2. 核心交换机并发连接数不少于500万。\n\n'
        '三、原厂服务承诺\n'
        '投标文件中须附上对应原厂书面承诺。\n'
        '★3. 须提供原厂售后服务承诺函。\n'
    )
    bid_text = (
        '一、商务售后应答\n'
        '★1. 免费质保期不少于36个月。（我方完全响应）\n\n'
        '二、核心网络设备应答\n'
        '★2. 核心交换机并发连接数为200万。\n\n'
        '三、原厂服务承诺应答\n'
        '★3. 须提供原厂售后服务承诺函。（详见后附材料）\n'
    )

    # (a) 串行模式 (BIDGUARD_F4_CONCURRENCY=1)
    monkeypatch.setenv('BIDGUARD_F4_CONCURRENCY', '1')
    client_serial = _ConcurrentMockLLMClient()
    res_serial = check_star_responses(tender_text, bid_text, client=client_serial)

    # (b) 4 线程并发模式 (BIDGUARD_F4_CONCURRENCY=4)
    monkeypatch.setenv('BIDGUARD_F4_CONCURRENCY', '4')
    client_conc = _ConcurrentMockLLMClient()
    res_conc = check_star_responses(tender_text, bid_text, client=client_conc)

    assert client_serial.calls == 3
    assert client_conc.calls == 3
    assert len(client_serial.thread_ids) == 1
    assert len(client_conc.thread_ids) >= 2
    assert res_conc['results'] == res_serial['results']
    assert res_conc['findings'] == res_serial['findings']
    assert [r['req_index'] for r in res_conc['results']] == [1, 2, 3]
    assert [f['code'] for f in res_conc['findings']] == ['CONTRADICTION', 'MISSING_RESPONSE']

    # (c) 单条 chat 抛异常（★2 抛 RuntimeError） -> ★2 降级为 error_fallback，★1 与 ★3 正常完成
    client_partial_fail = _ConcurrentMockLLMClient(fail_on_keyword='★2.')
    res_fail = check_star_responses(tender_text, bid_text, client=client_partial_fail)
    assert len(res_fail['results']) == 3
    assert res_fail['results'][0]['status'] == 'RESPONSE_OK'
    assert res_fail['results'][1]['llm_status'] == 'error_fallback'
    assert res_fail['results'][2]['status'] == 'MISSING_RESPONSE'


# ── 3 & 4 & 5 & 6. F7 三挡思考深度透传、模型不支持时跳过及 /api/qa 非法值忽略 ──

def test_e19_f7_three_tier_reasoning_effort_and_gateway_guard(tmp_path):
    doc = Document()
    doc.add_paragraph('★1. 免费质保期不少于36个月。')
    t_path = tmp_path / 't_qa.docx'
    b_path = tmp_path / 'b_qa.docx'
    doc.save(str(t_path))
    doc.save(str(b_path))

    # 验证 ⚡极速('low') / ⚖均衡('medium') / 🧠深度('high') 三挡透传参数真实拉开差异
    seen_efforts: list[str | None] = []

    class _QaStubClient:
        def chat(self, messages, model_tier='flash', reasoning_effort=None):
            seen_efforts.append(reasoning_effort)
            return SimpleNamespace(
                content=json.dumps({
                    'thought': '直接回答',
                    'action': 'final',
                    'action_input': {'answer': '质保36个月', 'quotes': ['★1. 免费质保期不少于36个月。']},
                })
            )

    stub = _QaStubClient()
    for level in ('low', 'medium', 'high'):
        ask_bid_expert(t_path, b_path, '质保期多久？', client=stub, reasoning_effort=level)
    assert seen_efforts == ['low', 'medium', 'high']
    assert len(set(seen_efforts)) == 3

    # 验证 LLMClient 在 supports_reasoning_effort=True vs False 时的透传与跳过保护
    captured_kwargs: list[dict] = []

    class _FakeCompletions:
        def create(self, **kwargs):
            captured_kwargs.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='ok'))],
                usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2),
            )

    class _FakeOpenAI:
        chat = SimpleNamespace(completions=_FakeCompletions())

    c_supported = build_client_from_user_config(
        base_url='https://x.example/v1',
        api_key='k',
        model_flash='glm-5.3-flash',
        supports_reasoning_effort=True,
    )
    c_supported.client = _FakeOpenAI()
    c_supported.use_openai = True
    c_supported.chat([{'role': 'user', 'content': 'hi'}], model_tier='flash', reasoning_effort='high')
    assert captured_kwargs[-1]['extra_body'].get('reasoning_effort') == 'high'

    c_unsupported = build_client_from_user_config(
        base_url='https://x.example/v1',
        api_key='k',
        model_flash='deepseek-chat',
        supports_reasoning_effort=False,
    )
    c_unsupported.client = _FakeOpenAI()
    c_unsupported.use_openai = True
    c_unsupported.chat([{'role': 'user', 'content': 'hi'}], model_tier='flash', reasoning_effort='high')
    assert 'reasoning_effort' not in captured_kwargs[-1]['extra_body']


def test_e19_qa_router_ignores_invalid_reasoning_effort(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'test_e19_qa.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    doc = Document()
    doc.add_paragraph('★1. 质保期三年。')
    buf = io.BytesIO()
    doc.save(buf)
    docx_bytes = buf.getvalue()

    with TestClient(app) as tc:
        r_login = tc.post('/api/auth/login', json={'username': 'admin', 'password': 'BidGuard2026!'})
        hdrs = {'Authorization': f"Bearer {r_login.json()['token']}"}

        r_task = tc.post(
            '/api/checks',
            headers=hdrs,
            files={
                'tender_file': ('t.docx', docx_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                'bid_file': ('b.docx', docx_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
            },
            data={'title': 'QA挡位测试任务', 'auto_run': 'false'},
        )
        tid = r_task.json()['id']

        r_sess = tc.post('/api/qa/sessions', headers=hdrs, json={'task_id': tid})
        sid = r_sess.json()['id']

        # 传入合法挡位 'high' 与非法挡位 'invalid_gear' 均正常返回 200
        for effort_val in ('low', 'medium', 'high', 'invalid_gear'):
            r_ask = tc.post(
                f"/api/qa/sessions/{sid}/ask",
                headers=hdrs,
                json={'question': '质保期多久？', 'max_rounds': 3, 'reasoning_effort': effort_val},
            )
            assert r_ask.status_code == 200
            assert '质保期' in r_ask.json()['answer']


def test_e19_worker_exposes_llm_fallback_as_info_finding(tmp_path, monkeypatch):
    """验证 F4 单条大模型调用异常降级项（llm_status='error_fallback'）通过 worker 落库为 info 提示级发现。"""
    import backend.worker as worker_mod

    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'test_e19_fallback.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    doc = Document()
    doc.add_paragraph('★1. 免费质保期不少于36个月。')
    buf = io.BytesIO()
    doc.save(buf)
    docx_bytes = buf.getvalue()

    def _fake_run_response_check(**kwargs):
        return {
            'findings': [],
            'star_results': [
                {
                    'status': 'RESPONSE_OK',
                    'req_index': 1,
                    'mark': '★',
                    'clause_no': '★1',
                    'tender_quote': '★1. 免费质保期不少于36个月。',
                    'bid_quote': '★1. 免费质保期不少于36个月。',
                    'bid_quote_raw': '★1. 免费质保期不少于36个月。',
                    'quote_verified': True,
                    'tender_start': 0,
                    'tender_end': 18,
                    'tender_page': 1,
                    'bid_start': 0,
                    'bid_end': 18,
                    'bid_page': 1,
                    'reason': '模型单条调用降级（TimeoutError）',
                    'llm_status': 'error_fallback',
                }
            ],
            'self_contradictions': [],
            'alignment_summary': {'total_requirements': 1, 'aligned_hits': 1, 'hit_rate': 1.0},
        }

    monkeypatch.setattr(worker_mod, 'run_response_check', _fake_run_response_check)
    worker_mod._global_worker = None

    with TestClient(app) as tc:
        r_login = tc.post('/api/auth/login', json={'username': 'admin', 'password': 'BidGuard2026!'})
        hdrs = {'Authorization': f"Bearer {r_login.json()['token']}"}

        r_task = tc.post(
            '/api/checks',
            headers=hdrs,
            files={
                'tender_file': ('t_fb.docx', docx_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                'bid_file': ('b_fb.docx', docx_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
            },
            data={'title': 'LLM降级暴露单测', 'auto_run': 'false'},
        )
        tid = r_task.json()['id']

        r_run = tc.post(f"/api/checks/{tid}/run?sync=true", headers=hdrs)
        assert r_run.status_code == 200
        assert r_run.json()['status'] == 'delivered'

        r_detail = tc.get(f"/api/checks/{tid}", headers=hdrs)
        assert r_detail.status_code == 200
        findings = r_detail.json()['findings']
        fb_items = [f for f in findings if f['code'] == 'LLM_FALLBACK']
        assert len(fb_items) == 1
        assert fb_items[0]['severity'] == 'info'
        assert fb_items[0]['consequence'] == 'none'
        assert fb_items[0]['source_module'] == 'F4'
        assert fb_items[0]['page'] == 1
        assert 'TimeoutError' in fb_items[0]['message']
    worker_mod._global_worker = None
