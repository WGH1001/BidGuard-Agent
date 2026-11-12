# -*- coding: utf-8 -*-
"""E21 · F7 标书专家多轮上下文 + 流式思考过程与工具轨迹直播卡单元测试（断言严格数数，禁空循环假绿）。

覆盖任务卡 §3 全部验收标准：
1. 多轮上下文传递：
   - 在 /api/qa/sessions/{sid}/ask 连续提问第 1 轮（"质保期几年？"）与第 2 轮（"刚刚上一轮我问了什么？"），
     断言第 2 轮调用 client.chat(messages) 时 messages 中真实包含第 1 轮的 user 与 assistant 历史记录；
   - 0-Token 离线降级模式下，第 2 轮含指代词（"刚刚上一轮我问了什么？"）也能结合第 1 轮上下文关键词检索并返回命中；
2. 历史截断保护：
   - 传入 20 轮超长 history（含超 1000 字单条消息与 loading 占位），_format_history_messages 仅保留最近 6 轮（12 条）
     且单条截断在 801 字符以内，绝不撑爆上下文窗口；
3. 流式主入口 ask_bid_expert_stream 与 SSE 端点 POST /api/qa/sessions/{sid}/ask/stream：
   - 验证响应 Content-Type 为 text/event-stream；
   - 事件帧严格按 step -> answer_delta -> done 顺序吐出；
   - 终帧 quotes 强制通过 verify_quote 校验，虚假编造摘录被清空（quote='' 且 quote_verified=False）；
   - 流式结束后 GET /api/qa/sessions/{sid} 可查到已持久化落库的完整问答与 steps；
4. 三挡推理深度映射与 7 轮上限（6 工具 + 1 final 收口）：
   - low -> ('low', 7)、medium -> ('high', 7)、high -> ('max', 7)，绝不向网关透传非法的 'medium'；
   - 即使在极速挡（low），也可顺序调用全部 6 个工具并在第 7 轮完成 final 收口。
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from docx import Document
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.main import create_app
import backend.routers.qa as qa_router_mod
from core.bid_react_agent import (
    _format_history_messages,
    _resolve_effort_and_rounds,
    ask_bid_expert_stream,
)


def _make_docx_bytes(lines: list[str]) -> bytes:
    doc = Document()
    for ln in lines:
        doc.add_paragraph(ln)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ── 1. 多轮上下文在 /ask 路由与 0-Token 降级中的传递验证 ──────────────────

def test_e21_multi_turn_history_injected_in_sync_and_zero_token_fallback(tmp_path, monkeypatch):
    db_file = tmp_path / 'test_e21_ctx.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    t_bytes = _make_docx_bytes(['第一章 技术要求', '★1. 本项目免费质保期不少于36个月。'])
    b_bytes = _make_docx_bytes(['第一章 投标响应', '★1. 我方承诺免费质保期为36个月。'])

    captured_messages_per_call: list[list[dict]] = []

    class _HistorySpyClient:
        def chat(self, messages, model_tier='flash', reasoning_effort=None):
            captured_messages_per_call.append([dict(m) for m in messages])
            return SimpleNamespace(
                content=json.dumps({
                    'thought': '已结合历史上下文核实',
                    'action': 'final',
                    'action_input': {
                        'answer': '免费质保期不少于36个月。',
                        'quotes': ['★1. 本项目免费质保期不少于36个月。'],
                    },
                })
            )

    spy_client = _HistorySpyClient()
    monkeypatch.setattr(qa_router_mod, 'resolve_effective_llm_client', lambda db, uid: (spy_client, 'done'))

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
            data={'title': 'E21多轮上下文测试', 'auto_run': 'false'},
        )
        tid = r_task.json()['id']

        r_sess = tc.post('/api/qa/sessions', headers=hdrs, json={'task_id': tid})
        sid = r_sess.json()['id']

        # 第 1 轮提问
        r_ask1 = tc.post(f"/api/qa/sessions/{sid}/ask", headers=hdrs, json={'question': '质保期几年？'})
        assert r_ask1.status_code == 200
        assert len(captured_messages_per_call) == 1
        assert len(captured_messages_per_call[0]) == 2  # system + 当前 user

        # 第 2 轮追问："刚刚上一轮我问了什么？" -> 断言 messages 中已注入第 1 轮的 user 与 assistant 消息
        r_ask2 = tc.post(f"/api/qa/sessions/{sid}/ask", headers=hdrs, json={'question': '刚刚上一轮我问了什么？'})
        assert r_ask2.status_code == 200
        assert len(captured_messages_per_call) == 2
        second_msgs = captured_messages_per_call[1]
        assert len(second_msgs) == 4  # system + round1_user + round1_assistant + round2_user
        assert second_msgs[1]['role'] == 'user' and '质保期几年' in second_msgs[1]['content']
        assert second_msgs[2]['role'] == 'assistant' and '36个月' in second_msgs[2]['content']

        # 验证 0-Token 离线降级也能结合上一轮问题检索
        monkeypatch.setattr(qa_router_mod, 'resolve_effective_llm_client', lambda db, uid: (None, 'skipped'))
        r_ask3 = tc.post(f"/api/qa/sessions/{sid}/ask", headers=hdrs, json={'question': '那上面这个怎么规定的？'})
        assert r_ask3.status_code == 200
        assert r_ask3.json()['mode'] == 'keyword_fallback'
        assert '36个月' in r_ask3.json()['answer']


# ── 2. 20 轮超长 history 截断保护与三挡映射验证 ───────────────────────────

def test_e21_history_truncation_and_three_tier_mapping():
    raw_history = []
    for i in range(20):
        raw_history.append({'role': 'user', 'content': f"第{i}轮提问：" + ('长文本' * 400)})
        raw_history.append({'role': 'assistant', 'content': f"第{i}轮回答：" + ('答复内容' * 400)})
    raw_history.append({'role': 'assistant', 'content': '', 'loading': True})

    formatted = _format_history_messages(raw_history, max_turns=6, max_chars_per_msg=800)
    assert len(formatted) == 12
    assert all(len(m['content']) <= 801 for m in formatted)
    assert '第14轮提问' in formatted[0]['content']
    assert '第19轮回答' in formatted[-1]['content']

    # 验证三挡映射：绝不透传 'medium'，且三挡均开放至 7 轮（仅调模型思考深度，同时尊重显式更小 max_rounds）
    assert _resolve_effort_and_rounds('low', 7) == ('low', 7)
    assert _resolve_effort_and_rounds('medium', 7) == ('high', 7)
    assert _resolve_effort_and_rounds('high', 7) == ('max', 7)
    assert _resolve_effort_and_rounds('low', 3) == ('low', 3)
    assert _resolve_effort_and_rounds('invalid', 7) == (None, 7)


def test_e21_low_tier_allows_all_six_tools_in_seven_rounds(tmp_path):
    """验证默认 7 轮（6 工具 + 1 final 收口）下，即使极速挡（low）也能完整调用全部 6 个工具并正常收口。"""
    from core.bid_react_agent import ALLOWED_REACT_TOOLS, ask_bid_expert

    t_path = tmp_path / 't6.docx'
    b_path = tmp_path / 'b6.docx'
    t_path.write_bytes(_make_docx_bytes(['第一章 评标办法', '★1. 质保期不少于36个月。']))
    b_path.write_bytes(_make_docx_bytes(['第一章 投标响应', '★1. 质保期36个月。营业执照有效期至2028-12-31。']))

    class _SixToolMockClient:
        def __init__(self):
            self.turn = 0
            self.efforts: list[str | None] = []

        def chat(self, messages, model_tier='flash', reasoning_effort=None):
            self.efforts.append(reasoning_effort)
            self.turn += 1
            if self.turn == 1:
                payload = {'thought': '1.查招标', 'action': 'search_tender', 'action_input': '质保期'}
            elif self.turn == 2:
                payload = {'thought': '2.查投标', 'action': 'search_bid', 'action_input': '质保期'}
            elif self.turn == 3:
                payload = {'thought': '3.定章节', 'action': 'locate_chapter', 'action_input': '评标办法'}
            elif self.turn == 4:
                payload = {
                    'thought': '4.算报价',
                    'action': 'recalc_price',
                    'action_input': {'items': [{'name': '设备', 'qty': '1', 'unit_price': '100', 'amount': '100'}], 'bid_total': '100'},
                }
            elif self.turn == 5:
                payload = {
                    'thought': '5.核证照',
                    'action': 'check_cert_expiry',
                    'action_input': {'cert_text': '有效期至2028-12-31', 'open_date': '2026-11-11'},
                }
            elif self.turn == 6:
                payload = {'thought': '6.查法规', 'action': 'cite_regulation', 'action_input': {'keyword': '招标'}}
            else:
                payload = {
                    'thought': '7.汇总收口',
                    'action': 'final',
                    'action_input': {
                        'answer': '全部6工具核验完成，质保期不少于36个月。',
                        'quotes': ['★1. 质保期不少于36个月。'],
                    },
                }
            return SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))

    mock_client = _SixToolMockClient()
    res = ask_bid_expert(
        tender_path=t_path,
        bid_path=b_path,
        question='请用全部工具全面核验',
        client=mock_client,
        reasoning_effort='low',
    )
    assert res['rounds_used'] == 7
    assert res['terminated_by_max_rounds'] is False
    core_six_tools = [t for t in ALLOWED_REACT_TOOLS if t != 'search_attachment']
    assert len(core_six_tools) == 6
    assert [s['action'] for s in res['steps']] == [*core_six_tools, 'final']
    assert mock_client.efforts == ['low'] * 7


# ── 3. ask_bid_expert_stream 与 POST /api/qa/sessions/{sid}/ask/stream 验证 ──

def test_e21_sse_ask_stream_event_order_quote_verification_and_persistence(tmp_path, monkeypatch):
    db_file = tmp_path / 'test_e21_stream.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    t_bytes = _make_docx_bytes(['第一章 商务要求', '★1. 投标保证金金额为人民币贰万元整。'])
    b_bytes = _make_docx_bytes(['第一章 商务响应', '★1. 我方已按要求缴纳投标保证金人民币贰万元整。'])

    class _TwoStepStreamMockClient:
        def __init__(self):
            self.turn = 0

        def chat(self, messages, model_tier='flash', reasoning_effort=None):
            self.turn += 1
            if self.turn == 1:
                return SimpleNamespace(
                    content=json.dumps({
                        'thought': '先在招标文件中检索保证金要求',
                        'action': 'search_tender',
                        'action_input': '投标保证金',
                    })
                )
            return SimpleNamespace(
                content=json.dumps({
                    'thought': '已拿到原文证据，准备汇总结论',
                    'action': 'final',
                    'action_input': {
                        'answer': '招标要求投标保证金为人民币贰万元整。',
                        'quotes': [
                            '★1. 投标保证金金额为人民币贰万元整。',
                            '这是一条故意编造的不存在虚假引用句',
                        ],
                    },
                })
            )

    monkeypatch.setattr(
        qa_router_mod,
        'resolve_effective_llm_client',
        lambda db, uid: (_TwoStepStreamMockClient(), 'done'),
    )

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
            data={'title': 'E21流式SSE测试', 'auto_run': 'false'},
        )
        tid = r_task.json()['id']

        r_sess = tc.post('/api/qa/sessions', headers=hdrs, json={'task_id': tid})
        sid = r_sess.json()['id']

        r_stream = tc.post(
            f"/api/qa/sessions/{sid}/ask/stream",
            headers=hdrs,
            json={'question': '投标保证金是多少？', 'max_rounds': 6, 'reasoning_effort': 'medium'},
        )
        assert r_stream.status_code == 200
        assert 'text/event-stream' in r_stream.headers.get('content-type', '')

        events: list[tuple[str, dict]] = []
        for block in r_stream.text.split('\n\n'):
            if not block.strip():
                continue
            ev_name = ''
            ev_data = ''
            for line in block.splitlines():
                if line.startswith('event:'):
                    ev_name = line.split(':', 1)[1].strip()
                elif line.startswith('data:'):
                    ev_data = line.split(':', 1)[1].strip()
            if ev_name and ev_data:
                events.append((ev_name, json.loads(ev_data)))

        ev_types = [e[0] for e in events]
        # 严格验证事件序列：先有 step，再有 answer_delta，最后以 done 收尾
        assert 'step' in ev_types
        assert 'answer_delta' in ev_types
        assert ev_types[-1] == 'done'
        assert ev_types.index('step') < ev_types.index('answer_delta') < ev_types.index('done')

        done_data = events[-1][1]
        assert done_data['mode'] == 'react'
        assert len(done_data['steps']) == 2
        assert done_data['steps'][0]['action'] == 'search_tender'

        # 验证虚假 quote 被强制清空且 quote_verified=False，真实 quote 通过校验
        quotes = done_data['quotes']
        assert len(quotes) == 2
        assert quotes[0]['quote_verified'] is True
        assert '贰万元整' in quotes[0]['quote']
        assert quotes[1]['quote_verified'] is False
        assert quotes[1]['quote'] == ''

        # 验证流式结束后会话消息已持久化入库
        r_get = tc.get(f"/api/qa/sessions/{sid}", headers=hdrs)
        assert r_get.status_code == 200
        saved_msgs = r_get.json()['messages_json']
        assert len(saved_msgs) == 2
        assert saved_msgs[0]['role'] == 'user' and saved_msgs[0]['content'] == '投标保证金是多少？'
        assert saved_msgs[1]['role'] == 'assistant'
        assert len(saved_msgs[1]['steps']) == 2
