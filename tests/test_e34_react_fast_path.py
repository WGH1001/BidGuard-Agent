# -*- coding: utf-8 -*-
"""E34 · F7 快速通道判定、Python 防偷懒单次回弹护栏、done 契约与持久化回放单测。"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
import tempfile

from docx import Document
from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.main import create_app
from core.bid_react_agent import (
    MAX_UNGROUNDED_BOUNCE_PER_TURN,
    ask_bid_expert,
    ask_bid_expert_stream,
)


@dataclass
class _MockResp:
    content: str


class _SeqMockClient:
    def __init__(self, handler):
        self.handler = handler
        self.calls = 0
        self.captured_messages = []

    def chat(self, messages, model_tier='flash', reasoning_effort=None, **kwargs):
        self.calls += 1
        self.captured_messages.append([dict(m) for m in messages])
        return _MockResp(content=self.handler(messages, self.calls))


def _save_temp_docx(paragraphs: list[str]) -> Path:
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    fd, path = tempfile.mkstemp(suffix='.docx')
    os.close(fd)
    doc.save(path)
    return Path(path)


# ── 1. 元问题首轮直答：0 工具，grounded=False / fast_path=True / quotes=[] ──

def test_e34_meta_question_fast_path_first_round():
    tender_p = _save_temp_docx(['第一章 招标公告：质保期 2 年。'])
    bid_p = _save_temp_docx(['商务响应：质保期 2 年。'])

    def _handler(messages, call_idx):
        assert call_idx == 1
        joined = '\n'.join(str(m.get('content', '')) for m in messages)
        assert 'direct_meta' in joined
        return json.dumps({
            'thought': '这是系统能力咨询类元问题，无需检索双书正文',
            'action': 'final',
            'action_input': {
                'route': 'direct_meta',
                'answer': '我可以协助核验招标红线、报价算术复算、质保期与响应偏离检索等。',
                'quotes': ['我伪造的一段不在原文里的引用'],
            },
        }, ensure_ascii=False)

    try:
        client = _SeqMockClient(_handler)
        res = ask_bid_expert(tender_p, bid_p, '你能帮我做什么？', client=client)
        assert client.calls == 1
        assert res['rounds_used'] == 1
        assert res['terminated_by_max_rounds'] is False
        assert res['grounded'] is False
        assert res['fast_path'] is True
        assert res['route'] == 'direct_meta'
        assert res['quotes'] == []
        assert len(res['steps']) == 1
        assert res['steps'][0]['action'] == 'final'
    finally:
        tender_p.unlink(missing_ok=True)
        bid_p.unlink(missing_ok=True)


# ── 2. 本项目核验问题企图首轮裸答（JSON 与 纯文本两种形式）均触发回弹 1 次 ───

def test_e34_project_question_bounces_once_json_and_prose_then_grounds():
    tender_p = _save_temp_docx(['第五章 商务条款：★1. 本项目免费质保期为 36 个月。'])
    bid_p = _save_temp_docx(['一、商务响应：我方完全响应 36 个月免费质保期。'])

    # Case A: 模型第 1 轮返回 JSON final 企图偷懒 -> 护栏弹回 -> 第 2 轮检索 -> 第 3 轮有据 final
    def _json_bounce_handler(messages, call_idx):
        if call_idx == 1:
            return json.dumps({
                'thought': '尝试凭空直接作答',
                'action': 'final',
                'action_input': {'answer': '通常质保期为 1 年。', 'quotes': []},
            }, ensure_ascii=False)
        if call_idx == 2:
            last_user = str(messages[-1]['content'])
            assert '尚未执行任何工具检索' in last_user
            return json.dumps({
                'thought': '被护栏纠正，立即检索双书原文',
                'actions': [
                    {'action': 'search_tender', 'action_input': '质保期'},
                    {'action': 'search_bid', 'action_input': '质保期'},
                ],
            }, ensure_ascii=False)
        return json.dumps({
            'thought': '拿到双书原文后有据收口',
            'action': 'final',
            'action_input': {
                'answer': '招标要求 36 个月免费质保，我方投标响应为 36 个月。',
                'quotes': ['第五章 商务条款：★1. 本项目免费质保期为 36 个月。'],
            },
        }, ensure_ascii=False)

    # Case B: 模型第 1 轮输出非 JSON 纯文本企图绕过护栏 -> 同样弹回 1 次 -> 第 2 轮检索
    def _prose_bounce_handler(messages, call_idx):
        if call_idx == 1:
            return '一般来说质保期为一年左右。'
        if call_idx == 2:
            last_user = str(messages[-1]['content'])
            assert '尚未执行任何工具检索' in last_user
            return json.dumps({'thought': '检索核实', 'action': 'search_tender', 'action_input': '质保期'})
        return json.dumps({
            'thought': '完成核实',
            'action': 'final',
            'action_input': {
                'answer': '招标要求 36 个月免费质保。',
                'quotes': ['第五章 商务条款：★1. 本项目免费质保期为 36 个月。'],
            },
        }, ensure_ascii=False)

    try:
        c_a = _SeqMockClient(_json_bounce_handler)
        res_a = ask_bid_expert(tender_p, bid_p, '质保期在哪响应的？要求几年？', client=c_a)
        assert c_a.calls == 3
        assert res_a['grounded'] is True
        assert res_a['fast_path'] is False
        assert res_a['rounds_used'] == 3
        assert len(res_a['quotes']) == 1 and res_a['quotes'][0]['quote_verified'] is True

        c_b = _SeqMockClient(_prose_bounce_handler)
        res_b = ask_bid_expert(tender_p, bid_p, '质保期几年？', client=c_b)
        assert c_b.calls == 3
        assert res_b['grounded'] is True
        assert res_b['fast_path'] is False
    finally:
        tender_p.unlink(missing_ok=True)
        bid_p.unlink(missing_ok=True)


# ── 3. 硬参数 MAX_UNGROUNDED_BOUNCE_PER_TURN = 1：弹回一次后仍直答必须放行 ────

def test_e34_max_bounce_cap_1_releases_second_round_and_respects_concept_gray_zone():
    tender_p = _save_temp_docx(['第一章 招标公告：质保期 2 年。'])
    bid_p = _save_temp_docx(['投标响应：质保期 2 年。'])

    assert MAX_UNGROUNDED_BOUNCE_PER_TURN == 1

    # 3a. 弹回 1 次后第 2 轮模型仍直答（含概念题误命中“质保”二字场景）-> 必须放行且标 grounded=False
    def _stubborn_direct_handler(messages, call_idx):
        return json.dumps({
            'thought': '这是概念问答，不查特定标书',
            'action': 'final',
            'action_input': {
                'route': 'direct_meta',
                'answer': '软件类项目行业通常质保期为 1 至 2 年（未查具体标书）。',
                'quotes': [],
            },
        }, ensure_ascii=False)

    # 3b. 无信号词通用概念题（“质疑期限一般是几天？”）-> 首轮直接放行 0 回弹
    def _no_signal_concept_handler(messages, call_idx):
        assert call_idx == 1
        return json.dumps({
            'thought': '通用法规概念题直答',
            'action': 'final',
            'action_input': {
                'route': 'direct_meta',
                'answer': '依据政府采购法规，供应商通常可在知道或应知权益受损之日起 7 个工作日内提出质疑。',
                'quotes': [],
            },
        }, ensure_ascii=False)

    try:
        c_stubborn = _SeqMockClient(_stubborn_direct_handler)
        res_5b = ask_bid_expert(tender_p, bid_p, '软件项目质保期一般怎么设置？', client=c_stubborn)
        assert c_stubborn.calls == 2  # 首轮被弹 1 次，第 2 轮严格放行绝不死弹
        assert res_5b['rounds_used'] == 2
        assert res_5b['grounded'] is False
        assert res_5b['fast_path'] is True

        c_5a = _SeqMockClient(_no_signal_concept_handler)
        res_5a = ask_bid_expert(tender_p, bid_p, '质疑期限一般是几天？', client=c_5a)
        assert c_5a.calls == 1
        assert res_5a['grounded'] is False
        assert res_5a['fast_path'] is True
    finally:
        tender_p.unlink(missing_ok=True)
        bid_p.unlink(missing_ok=True)


# ── 4. 历史重排（有 history 放行 vs 空 history 防绕过）────────────────────────

def test_e34_history_reorg_requires_prior_history():
    tender_p = _save_temp_docx(['第一章 招标公告：质保期 2 年，限价 50 万元。'])
    bid_p = _save_temp_docx(['商务响应：质保期 2 年，总报价 48 万元。'])

    def _reorg_handler(messages, call_idx):
        return json.dumps({
            'thought': '第1轮整理成表',
            'action': 'final',
            'action_input': {
                'route': 'direct_meta',
                'answer': '| 项 | 内容 |\n|---|---|\n| 质保 | 2 年 |',
                'quotes': [],
            },
        }, ensure_ascii=False)

    def _bounce_then_search(messages, call_idx):
        if call_idx == 1:
            return json.dumps({
                'thought': '偷懒想直接编表',
                'action': 'final',
                'action_input': {'answer': '假表格', 'quotes': []},
            })
        if call_idx == 2:
            assert '尚未执行任何工具检索' in str(messages[-1]['content'])
            return json.dumps({'thought': '查投标报价', 'action': 'search_bid', 'action_input': '报价'})
        return json.dumps({
            'thought': '实查后输出',
            'action': 'final',
            'action_input': {'answer': '总报价 48 万元。', 'quotes': ['商务响应：总报价 48 万元。']},
        })

    try:
        # 有历史对话 + "把上一轮结果整理成表格" -> 首轮放行
        prior = [
            {'role': 'user', 'content': '质保期要求和响应是多少？'},
            {'role': 'assistant', 'content': '招标和投标均为 2 年。'},
        ]
        c_with_hist = _SeqMockClient(_reorg_handler)
        res_hist = ask_bid_expert(
            tender_p,
            bid_p,
            '把上一轮关于质保期的结果整理成表格',
            client=c_with_hist,
            history=prior,
        )
        assert c_with_hist.calls == 1
        assert res_hist['grounded'] is False
        assert res_hist['fast_path'] is True

        # 空会话首问 "把投标文件里的质保期和报价整理成表格" -> 无 history，必须回弹！
        c_empty_hist = _SeqMockClient(_bounce_then_search)
        res_empty = ask_bid_expert(
            tender_p,
            bid_p,
            '把投标文件里的质保期和报价整理成表格',
            client=c_empty_hist,
            history=[],
        )
        assert c_empty_hist.calls == 3
        assert res_empty['grounded'] is True
    finally:
        tender_p.unlink(missing_ok=True)
        bid_p.unlink(missing_ok=True)


# ── 5. 带图附件不回弹、max_rounds=1 边界、BaseException 透传与消耗轮次语义 ──

def test_e34_image_attachment_max_rounds_1_base_exception_and_round_consumption():
    tender_p = _save_temp_docx(['第一章 招标：营业执照需在有效期内。'])
    bid_p = _save_temp_docx(['我方已附营业执照。'])

    def _direct_handler(messages, call_idx):
        return json.dumps({
            'thought': '直答',
            'action': 'final',
            'action_input': {'answer': '直观检查完毕，有效期至 2028 年。', 'quotes': []},
        }, ensure_ascii=False)

    class _CustomBaseCancel(BaseException):
        pass

    def _cancel_handler(messages, call_idx):
        raise _CustomBaseCancel('cancelled in round 1')

    def _consume_limit_handler(messages, call_idx):
        # limit=7 下第 1 轮尝试直答被弹 -> 若之后 6 轮全搜仍未 final，rounds_used=7 且不越界
        if call_idx == 1:
            return json.dumps({
                'thought': '尝试偷懒',
                'action': 'final',
                'action_input': {'answer': '瞎猜', 'quotes': []},
            })
        return json.dumps({'thought': f'第{call_idx}轮查', 'action': 'search_tender', 'action_input': '营业执照'})

    try:
        # 5.1 带图附件首轮直答 -> 不回弹，正常 1 轮返回
        c_img = _SeqMockClient(_direct_handler)
        res_img = ask_bid_expert(
            tender_p,
            bid_p,
            '这张图片里的营业执照何时到期？',
            client=c_img,
            attachments=[{'attachment_id': 'img1', 'filename': 'license.png', 'is_image': True, 'text': ''}],
        )
        assert c_img.calls == 1
        assert res_img['grounded'] is True
        assert res_img['fast_path'] is False

        # 5.2 max_rounds=1 单轮边界 -> 不弹回，直放并标 grounded=False
        c_r1 = _SeqMockClient(_direct_handler)
        res_r1 = ask_bid_expert(tender_p, bid_p, '质保期几年？', client=c_r1, max_rounds=1)
        assert c_r1.calls == 1
        assert res_r1['grounded'] is False
        assert res_r1['fast_path'] is True

        # 5.3 BaseException 透传不被吞
        with pytest.raises(_CustomBaseCancel):
            ask_bid_expert(tender_p, bid_p, '质保期几年？', client=_SeqMockClient(_cancel_handler))

        # 5.4 limit=7 弹回 1 次消耗 1 轮，后续循环最多再跑 6 次工具（总计 1+6=7 轮进 E32 收口）
        c_cap = _SeqMockClient(_consume_limit_handler)
        res_cap = ask_bid_expert(tender_p, bid_p, '质保期几年？', client=c_cap, max_rounds=7)
        assert res_cap['rounds_used'] == 7
        assert res_cap['terminated_by_max_rounds'] is True
        assert c_cap.calls == 8  # 7 轮内（1 弹回 + 6 搜索）+ 1 轮 E32 强制收口
    finally:
        tender_p.unlink(missing_ok=True)
        bid_p.unlink(missing_ok=True)


# ── 6. 后端路由 /ask、/ask/stream 与 messages_json 落库持久化全链路 ────────

def test_e34_router_ask_stream_and_messages_json_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'test_e34_qa.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    tender_p = _save_temp_docx(['第一章 招标：质保期 2 年。'])
    bid_p = _save_temp_docx(['我方响应：质保期 2 年。'])

    try:
        with TestClient(app) as tc:
            login_resp = tc.post('/api/auth/login', json={'username': 'liming', 'password': 'BidGuard2026!'})
            assert login_resp.status_code == 200
            hdrs = {'Authorization': f"Bearer {login_resp.json()['token']}"}

            sess_resp = tc.post(
                '/api/qa/sessions',
                headers=hdrs,
                json={'title': 'E34快速通道测试', 'tender_path': str(tender_p), 'bid_path': str(bid_p)},
            )
            assert sess_resp.status_code == 200
            sid = sess_resp.json()['id']

            import backend.routers.qa as qa_router_mod

            def _meta_handler(messages, call_idx):
                return json.dumps({
                    'thought': '系统功能说明',
                    'action': 'final',
                    'action_input': {
                        'route': 'direct_meta',
                        'answer': '我是 BidGuard 标书专家，支持报价核验与偏离检查。',
                        'quotes': [],
                    },
                }, ensure_ascii=False)

            meta_client = _SeqMockClient(_meta_handler)
            monkeypatch.setattr(qa_router_mod, 'resolve_effective_llm_client', lambda db, uid: (meta_client, {}))

            # 6.1 同步 /ask 返回 grounded=False & fast_path=True & route='direct_meta'
            r_sync = tc.post(f'/api/qa/sessions/{sid}/ask', headers=hdrs, json={'question': '你能干什么？'})
            assert r_sync.status_code == 200
            sync_data = r_sync.json()
            assert sync_data['grounded'] is False
            assert sync_data['fast_path'] is True
            assert sync_data['route'] == 'direct_meta'

            # 6.2 流式 /ask/stream done 帧携带同样元数据
            r_stream = tc.post(f'/api/qa/sessions/{sid}/ask/stream', headers=hdrs, json={'question': '你有哪些工具？'})
            assert r_stream.status_code == 200
            done_lines = [
                line[len('data: '):]
                for line in r_stream.text.splitlines()
                if line.startswith('data: ') and '"fast_path"' in line
            ]
            assert len(done_lines) == 1
            stream_done = json.loads(done_lines[0])
            assert stream_done['grounded'] is False
            assert stream_done['fast_path'] is True
            assert stream_done['route'] == 'direct_meta'

            # 6.3 刷新读取 GET /api/qa/sessions/{sid} -> messages_json 已持久保存元数据
            r_detail = tc.get(f'/api/qa/sessions/{sid}', headers=hdrs)
            assert r_detail.status_code == 200
            msgs = r_detail.json()['messages_json']
            assistant_msgs = [m for m in msgs if m.get('role') == 'assistant']
            assert len(assistant_msgs) == 2
            for am in assistant_msgs:
                assert am['grounded'] is False
                assert am['fast_path'] is True
                assert am['route'] == 'direct_meta'
    finally:
        tender_p.unlink(missing_ok=True)
        bid_p.unlink(missing_ok=True)
