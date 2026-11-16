# -*- coding: utf-8 -*-
"""E33 · F7 并行工具调度、挡位上限解耦、预算与同源空转遥测、向量缓存线程安全单测。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

from docx import Document
from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import backend.database as db_mod
from backend.main import create_app
from backend.models import AuditLog, BidCheckTask, Tenant
from core.bid_react_agent import (
    MAX_PARALLEL_ACTIONS_PER_ROUND,
    _resolve_effort_and_rounds,
    ask_bid_expert,
    ask_bid_expert_stream,
)
import core.hybrid_retriever as hr_mod


@dataclass
class _MockResp:
    content: str


class _SeqMockClient:
    def __init__(self, handler):
        self.handler = handler
        self.calls = 0
        self.captured_messages = []
        self.captured_efforts = []

    def chat(self, messages, model_tier='flash', reasoning_effort=None, **kwargs):
        self.calls += 1
        self.captured_messages.append([dict(m) for m in messages])
        self.captured_efforts.append(reasoning_effort)
        return _MockResp(content=self.handler(messages, self.calls))


def _save_temp_docx(paragraphs: list[str]) -> Path:
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    fd, path = tempfile.mkstemp(suffix='.docx')
    os.close(fd)
    doc.save(path)
    return Path(path)


# ── 1. 并行工具调度、上限切片与混合 action+final 防早退 ──────────────────────

def test_e33_parallel_actions_tender_and_bid_batch_order_and_quotes():
    tender_p = _save_temp_docx([
        '第五章 商务要求',
        '★1. 质保期要求：系统整体提供不少于 36 个月免费运维质保服务。',
    ])
    bid_p = _save_temp_docx([
        '一、商务响应偏离表',
        '我公司完全满足并响应提供自验收通过之日起 36 个月免费运维质保服务。',
    ])

    def _handler(messages, call_idx):
        if call_idx == 1:
            return json.dumps({
                'thought': '首轮并行检索招标书与投标书中的质保期条款',
                'actions': [
                    {'action': 'search_tender', 'action_input': '质保期'},
                    {'action': 'search_bid', 'action_input': '质保期'},
                ],
            }, ensure_ascii=False)
        # 验证第 2 轮已一次收到两条并行工具结果及预算进度提示
        joined = '\n'.join(str(m.get('content', '')) for m in messages)
        assert '工具批量返回结果（本轮并行 2 项）' in joined
        assert '[1] search_tender 返回结果' in joined
        assert '[2] search_bid 返回结果' in joined
        assert '思考回合进度：第 1/' in joined
        return json.dumps({
            'thought': '两份文档原文均已齐备，直接 final 收口',
            'action': 'final',
            'action_input': {
                'answer': '招标要求 36 个月免费运维质保，投标书响应 36 个月，完全满足。',
                'quotes': [
                    '★1. 质保期要求：系统整体提供不少于 36 个月免费运维质保服务。',
                    '我公司完全满足并响应提供自验收通过之日起 36 个月免费运维质保服务。',
                ],
            },
        }, ensure_ascii=False)

    try:
        client = _SeqMockClient(_handler)
        res = ask_bid_expert(tender_p, bid_p, '质保期是否满足？', client=client)
        assert client.calls == 2
        assert res['rounds_used'] == 2
        assert res['terminated_by_max_rounds'] is False
        assert len(res['steps']) == 3
        assert res['steps'][0]['round'] == 1
        assert res['steps'][0]['action'] == 'search_tender'
        assert res['steps'][0]['batch_index'] == 1
        assert res['steps'][0]['batch_size'] == 2
        assert res['steps'][1]['round'] == 1
        assert res['steps'][1]['action'] == 'search_bid'
        assert res['steps'][1]['batch_index'] == 2
        assert res['steps'][1]['batch_size'] == 2
        assert res['steps'][2]['round'] == 2
        assert res['steps'][2]['action'] == 'final'
        assert len(res['quotes']) == 2
        assert all(q.get('quote_verified') is True for q in res['quotes'])
    finally:
        tender_p.unlink(missing_ok=True)
        bid_p.unlink(missing_ok=True)


def test_e33_parallel_cap_4_mixed_final_guard_and_partial_error_self_heal():
    tender_p = _save_temp_docx(['★1. 投标总报价限价为 500000 元。'])
    bid_p = _save_temp_docx(['投标总价 480000 元，质保期 2 年。'])

    def _handler(messages, call_idx):
        if call_idx == 1:
            # 塞入 6 项（其中混入 1 个非法格式 recalc_price 与 1 个混入 final），严格按前 4 项工具执行
            return json.dumps({
                'thought': '超限并行试探 + 混合 final 与故障参数',
                'actions': [
                    {'action': 'search_tender', 'action_input': '限价'},
                    {'action': 'recalc_price', 'action_input': 'bad_string_not_list'},
                    {'action': 'search_bid', 'action_input': '总价'},
                    {'action': 'locate_chapter', 'action_input': {'chapter_title': '限价', 'doc': 'tender'}},
                    {'action': 'final', 'action_input': {'answer': '提前跳过'}},
                    {'action': 'search_bid', 'action_input': '质保'},
                ],
            }, ensure_ascii=False)
        return json.dumps({
            'thought': '收到批次观察后正常结束',
            'action': 'final',
            'action_input': {
                'answer': '已查核限价 500000 元与投标总价 480000 元。',
                'quotes': ['★1. 投标总报价限价为 500000 元。'],
            },
        }, ensure_ascii=False)

    try:
        assert MAX_PARALLEL_ACTIONS_PER_ROUND == 4
        client = _SeqMockClient(_handler)
        res = ask_bid_expert(tender_p, bid_p, '报价和限价如何？', client=client)
        assert res['rounds_used'] == 2
        round1_steps = [s for s in res['steps'] if s['round'] == 1]
        assert len(round1_steps) == 4
        assert [s['action'] for s in round1_steps] == [
            'search_tender',
            'recalc_price',
            'search_bid',
            'locate_chapter',
        ]
        # 故障工具转 observation.error 自愈，其余正常命中
        assert 'error' in round1_steps[1]['observation']
        assert isinstance(round1_steps[0]['observation'], list) and len(round1_steps[0]['observation']) >= 1
    finally:
        tender_p.unlink(missing_ok=True)
        bid_p.unlink(missing_ok=True)


def test_e33_parallel_base_exception_cancellation_propagates(monkeypatch):
    tender_p = _save_temp_docx(['第一章 招标公告：交付周期 30 天。'])
    bid_p = _save_temp_docx(['我方响应 30 天内完成交付。'])

    class _MockBaseCancel(BaseException):
        pass

    import core.bid_react_agent as agent_mod

    orig_search = agent_mod._search_text_windows

    def _boom_on_bid(text, kw, src, **kwargs):
        if src == 'bid':
            raise _MockBaseCancel('user cancelled during parallel batch')
        return orig_search(text, kw, src, **kwargs)

    monkeypatch.setattr(agent_mod, '_search_text_windows', _boom_on_bid)

    def _handler(messages, call_idx):
        return json.dumps({
            'thought': '并行触发取消',
            'actions': [
                {'action': 'search_tender', 'action_input': '交付'},
                {'action': 'search_bid', 'action_input': '交付'},
            ],
        })

    try:
        client = _SeqMockClient(_handler)
        with pytest.raises(_MockBaseCancel):
            ask_bid_expert(tender_p, bid_p, '交付期几天？', client=client)
    finally:
        tender_p.unlink(missing_ok=True)
        bid_p.unlink(missing_ok=True)


# ── 2. 向量缓存按 doc_sha 加锁原子写入 + 法规库并行会话压测 ─────────────────

def test_e33_hybrid_retriever_parallel_same_doc_cache_lock_and_atomic_save(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '0')
    monkeypatch.setattr(hr_mod, '_CACHE_DIR', tmp_path / 'emb_cache')

    embed_doc_calls = 0
    embed_lock = threading.Lock()

    class _VecClient:
        models = {'embedding': 'embedding-3'}

        def embed(self, texts: list[str]):
            nonlocal embed_doc_calls
            if len(texts) > 1:
                with embed_lock:
                    embed_doc_calls += 1
                time.sleep(0.03)
            # 为每个文本片段确定性生成 4 维向量
            out = []
            for t in texts:
                s = sum(ord(ch) for ch in t)
                out.append([1.0, float((s % 17) + 1), float((s % 13) + 1), 0.5])
            return out

    doc_text = '\n'.join([
        f"第{i}条 供应商应具备完善的售后服务体系与本地技术支持保障能力。"
        for i in range(1, 20)
    ])
    client = _VecClient()

    def _run_query(q: str):
        return hr_mod.hybrid_retrieve_windows(q, doc_text, client=client, top_k=2, min_score=0.0)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_run_query, ['售后服务', '技术支持', '运维响应', '维保能力']))

    assert len(results) == 4
    assert all(isinstance(r, list) and len(r) >= 1 for r in results)
    # 同一 doc_sha 并发查 4 路，doc_lock 串行复检机制保证整篇切块向量只计算 1 次（非 4 次）
    assert embed_doc_calls == 1
    npy_files = list((tmp_path / 'emb_cache').glob('*.emb.npy'))
    assert len(npy_files) == 1
    tmp_leftovers = list((tmp_path / 'emb_cache').glob('*.tmp'))
    assert tmp_leftovers == []


def test_e33_parallel_cite_regulation_sqlite_sessions_no_lock_error(tmp_path, monkeypatch):
    db_file = tmp_path / 'test_e33_regs.db'
    eng = db_mod.init_db(f"sqlite:///{db_file.as_posix()}", seed=True)
    monkeypatch.setattr(db_mod, 'SessionLocal', db_mod.get_session_factory(eng))
    tender_p = _save_temp_docx(['第一章 招标公告：遵守招投标法相关规定。'])
    bid_p = _save_temp_docx(['我方响应：严格遵守政府采购法与招标投标法。'])

    def _handler(messages, call_idx):
        if call_idx == 1:
            return json.dumps({
                'thought': '同轮并发检索两部法规原文与招标正文',
                'actions': [
                    {'action': 'cite_regulation', 'action_input': {'keyword': '串通投标'}},
                    {'action': 'cite_regulation', 'action_input': {'article_no': '第一条'}},
                    {'action': 'search_tender', 'action_input': '招投标法'},
                ],
            }, ensure_ascii=False)
        return json.dumps({
            'thought': '完成并发法条核对',
            'action': 'final',
            'action_input': {
                'answer': '已并行检索法规条文与招标文件条款。',
                'quotes': ['第一章 招标公告：遵守招投标法相关规定。'],
            },
        }, ensure_ascii=False)

    try:
        client = _SeqMockClient(_handler)
        res = ask_bid_expert(tender_p, bid_p, '串通投标怎么规定的？', client=client)
        assert res['rounds_used'] == 2
        r1_steps = [s for s in res['steps'] if s['round'] == 1]
        assert len(r1_steps) == 3
        for reg_step in r1_steps[:2]:
            assert isinstance(reg_step['observation'], dict)
            assert 'error' not in reg_step['observation']
            assert reg_step['observation'].get('status') != 'empty_library'
    finally:
        tender_p.unlink(missing_ok=True)
        bid_p.unlink(missing_ok=True)
        eng.dispose()


def test_e33_locate_chapter_start_resets_stagnation_streak():
    tender_p = _save_temp_docx(['第一章 招标邀请', '第二章 采购需求\n设备交付周期 30 日。'])
    bid_p = _save_temp_docx(['商务响应表：完全响应采购需求。'])

    def _handler(messages, call_idx):
        if call_idx == 1:
            return json.dumps({'thought': '空搜一次', 'action': 'search_bid', 'action_input': '不存在ABC1'})
        if call_idx == 2:
            # 用 locate_chapter 命中新章节点（返回字段为 start 而非 offset）-> 必须清零 stagnation_streak
            return json.dumps({'thought': '章节定位', 'action': 'locate_chapter', 'action_input': {'chapter_title': '采购需求', 'doc': 'tender'}})
        if call_idx == 3:
            last_user = str(messages[-1]['content'])
            assert '检索去重提示' not in last_user
            return json.dumps({'thought': '空搜一次', 'action': 'search_bid', 'action_input': '不存在ABC2'})
        last_user = str(messages[-1]['content'])
        # 仅连续 1 次空检，不应误弹去重提示
        assert '检索去重提示' not in last_user
        return json.dumps({
            'thought': '收口',
            'action': 'final',
            'action_input': {'answer': '采购需求要求 30 日交付。', 'quotes': ['设备交付周期 30 日。']},
        }, ensure_ascii=False)

    try:
        client = _SeqMockClient(_handler)
        res = ask_bid_expert(tender_p, bid_p, '采购需求怎么说？', client=client)
        assert res['rounds_used'] == 4
        assert res['terminated_by_max_rounds'] is False
    finally:
        tender_p.unlink(missing_ok=True)
        bid_p.unlink(missing_ok=True)


# ── 3. 挡位天花板解耦（E21 low=7 兼容 + medium=8 / high=12）& 预算/去重遥测 ──

def test_e33_effort_rounds_decoupling_e21_compat_and_stagnation_early_stop():
    # 1. 严格保持 E21 显式传 7 契约不动，同时对 None 放开 7 / 8 / 12
    assert _resolve_effort_and_rounds('low', 7) == ('low', 7)
    assert _resolve_effort_and_rounds('medium', 7) == ('high', 7)
    assert _resolve_effort_and_rounds('high', 7) == ('max', 7)

    assert _resolve_effort_and_rounds('low', None) == ('low', 7)
    assert _resolve_effort_and_rounds('medium', None) == ('high', 8)
    assert _resolve_effort_and_rounds('high', None) == ('max', 12)
    assert _resolve_effort_and_rounds(None, None) == (None, 7)

    # 2. 停滞去重提示：连续 2 轮命中同一文本窗或空返回 -> 自动触发【检索去重提示】促早停
    tender_p = _save_temp_docx(['第五章 质保条款：免费质保 2 年。'])
    bid_p = _save_temp_docx(['二、过往业绩：湖州项目质保期 3 年，2024 年完成。'])

    def _stagnation_handler(messages, call_idx):
        if call_idx == 1:
            return json.dumps({
                'thought': '第 1 轮检索质保期',
                'actions': [
                    {'action': 'search_tender', 'action_input': '质保'},
                    {'action': 'search_bid', 'action_input': '质保'},
                ],
            })
        if call_idx == 2:
            # 确认第 2 轮仅收到进度条，尚无停滞提示
            last_user = str(messages[-1]['content'])
            assert '【思考回合进度：第 1/12 轮（剩余 11 轮可用）' in last_user
            assert '检索去重提示' not in last_user
            # 同轮换空词搜索，故意返回 [] 且重复命中同一区间 -> 停滞第 1 次
            return json.dumps({
                'thought': '换词保修续搜',
                'action': 'search_bid',
                'action_input': '不存在的保修词XYZ',
            })
        if call_idx == 3:
            # 第 3 轮再次重复命中湖州旧区间 -> stagnation_streak 达到 2，必须在第 3 轮后触发停滞去重提示
            return json.dumps({
                'thought': '再搜质保期还是老地方',
                'action': 'search_bid',
                'action_input': '质保期',
            })
        # 第 4 轮检查已注入停滞去重提示，模型决定早停 final（仅用 4 < 12 轮）
        last_user = str(messages[-1]['content'])
        assert '【检索去重提示：已连续 2 轮空检或命中同一原文区间' in last_user
        return json.dumps({
            'thought': '停止无意义换词，早停如实回答',
            'action': 'final',
            'action_input': {
                'answer': '招标要求质保 2 年；投标书仅检索到湖州过往业绩质保 3 年，未见本项目响应。',
                'quotes': ['第五章 质保条款：免费质保 2 年。'],
            },
        }, ensure_ascii=False)

    try:
        client = _SeqMockClient(_stagnation_handler)
        res = ask_bid_expert(
            tender_p,
            bid_p,
            '质保期怎么规定的？',
            client=client,
            reasoning_effort='high',  # 上限 12 轮，但模型在第 4 轮早停
        )
        assert client.captured_efforts == ['max', 'max', 'max', 'max']
        assert res['rounds_used'] == 4
        assert res['terminated_by_max_rounds'] is False
    finally:
        tender_p.unlink(missing_ok=True)
        bid_p.unlink(missing_ok=True)


# ── 4. /api/qa 路由层 max_rounds=None/10 放通与 high 挡跑穿 >8 轮验证 ────────

def test_e33_qa_router_max_rounds_12_limit_and_deep_reasoning_execution(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'test_e33_qa.db'
    db_url = f"sqlite:///{db_file.as_posix()}"
    app = create_app(database_url=db_url, auto_init_db=True)

    tender_p = _save_temp_docx(['第一章 招标公告：质保期不少于 3 年。'])
    bid_p = _save_temp_docx(['投标响应：我方提供 3 年质保期。'])

    try:
        with TestClient(app) as tc:
            login_resp = tc.post(
                '/api/auth/login',
                json={'username': 'liming', 'password': 'BidGuard2026!'},
            )
            assert login_resp.status_code == 200
            hdrs = {'Authorization': f"Bearer {login_resp.json()['token']}"}

            create_sess_resp = tc.post(
                '/api/qa/sessions',
                headers=hdrs,
                json={
                    'title': 'E33深度追问测试',
                    'tender_path': str(tender_p),
                    'bid_path': str(bid_p),
                },
            )
            assert create_sess_resp.status_code == 200, create_sess_resp.text
            sid = create_sess_resp.json()['id']

            # 超过 le=12 上限仍被 Pydantic 拦截 422
            bad_resp = tc.post(
                f'/api/qa/sessions/{sid}/ask',
                headers=hdrs,
                json={'question': '质保期？', 'max_rounds': 13},
            )
            assert bad_resp.status_code == 422

            # mock LLM 客户端跑第 1..9 轮工具 + 第 10 轮 final（突破旧版 8 轮封顶）
            import backend.routers.qa as qa_router_mod

            def _deep_handler(messages, call_idx):
                if call_idx < 10:
                    return json.dumps({
                        'thought': f'深度多跳第 {call_idx} 轮检索',
                        'action': 'search_tender' if call_idx % 2 else 'search_bid',
                        'action_input': '质保期',
                    })
                return json.dumps({
                    'thought': '第 10 轮收口',
                    'action': 'final',
                    'action_input': {
                        'answer': '招标与投标均为 3 年质保期。',
                        'quotes': ['第一章 招标公告：质保期不少于 3 年。'],
                    },
                })

            deep_client = _SeqMockClient(_deep_handler)
            monkeypatch.setattr(
                qa_router_mod,
                'resolve_effective_llm_client',
                lambda db, uid: (deep_client, {}),
            )

            # 不传 max_rounds + reasoning_effort='high' -> 自动启用 12 轮上限，成功跑完 10 轮
            ok_resp = tc.post(
                f'/api/qa/sessions/{sid}/ask',
                headers=hdrs,
                json={'question': '深度核查质保期？', 'reasoning_effort': 'high'},
            )
            assert ok_resp.status_code == 200, ok_resp.text
            data = ok_resp.json()
            assert data['rounds_used'] == 10
            assert data['terminated_by_max_rounds'] is False
            assert deep_client.captured_efforts[0] == 'max'
    finally:
        tender_p.unlink(missing_ok=True)
        bid_p.unlink(missing_ok=True)
