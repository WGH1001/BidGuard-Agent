# -*- coding: utf-8 -*-
"""F1 LLM 分段抽取层单元测试（mock 网关，全程 0 Token、不联网）。"""
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.tender_llm import (
    _expand_window,
    _parse_llm_json,
    extract_tender_with_llm,
    structure_candidate,
)
from tests.test_tender_extractor import SAMPLE


class MockResp:
    def __init__(self, content):
        self.content = content
        self.model = 'mock'
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cost_estimate = 0.0


class MockClient:
    """按调用序回放预设回复的假网关（记录 prompt 供断言）。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def chat(self, messages, model_tier='flash', **kwargs):
        self.prompts.append(messages[0]['content'])
        return MockResp(self.replies.pop(0))

    def total_usage(self):
        return {'calls': len(self.prompts)}


def test_parse_llm_json_tolerant():
    assert _parse_llm_json('{"a": 1}') == {'a': 1}
    assert _parse_llm_json('```json\n{"a": 1}\n```') == {'a': 1}   # markdown 包裹
    assert _parse_llm_json('前置废话 {"is_requirement": false} 后缀') == {'is_requirement': False}
    assert _parse_llm_json('完全没有 JSON') is None
    assert _parse_llm_json('') is None
    assert _parse_llm_json('[1,2]') is None          # 非对象
    assert _parse_llm_json('{"broken": ') is None    # 截断


def test_structure_candidate_quote_verified():
    """LLM 摘录的 full_text 逐字来自窗口 → 溯源通过、采信摘录。"""
    window = '第三章 采购需求\n★1) CPU 核数不低于 32 核，内存不低于 128G\n▲2) 质保三年\n'
    cand = {'mark': '★', 'text': '★1) CPU 核数不低于 32 核，内存不低于 128G',
            'start': 9, 'end': 37, 'clause_no': '', 'kind': 'requirement'}
    reply = json.dumps({
        'is_requirement': True,
        'clause_type': 'star_param',
        'clause_no': '★1)',
        'full_text': '★1) CPU 核数不低于 32 核，内存不低于 128G',
        'reason': '实质性硬件参数要求',
    }, ensure_ascii=False)
    out = structure_candidate(window, cand, MockClient([reply]))
    assert out['quote_verified'] is True
    assert out['full_text'] == '★1) CPU 核数不低于 32 核，内存不低于 128G'
    assert out['structured']['clause_type'] == 'star_param'


def test_structure_candidate_hallucination_rejected():
    """LLM 摘录的 full_text 在窗口中不存在（幻觉/改写）→ 溯源拦截，
    不采信摘录、回退候选原文，且 structured 内的脏 full_text 一并清除。"""
    window = '★1) CPU 核数不低于 32 核\n'
    cand = {'mark': '★', 'text': '★1) CPU 核数不低于 32 核',
            'start': 0, 'end': 16, 'clause_no': '', 'kind': 'requirement'}
    reply = json.dumps({
        'is_requirement': True,
        'clause_type': 'star_param',
        'clause_no': '★1)',
        'full_text': '★1) CPU 核数不低于 64 核，支持液冷',   # 改写数字 + 编造
        'reason': 'x',
    }, ensure_ascii=False)
    out = structure_candidate(window, cand, MockClient([reply]))
    assert out['quote_verified'] is False
    assert out['full_text'] == cand['text']          # 回退候选原文
    assert out['structured']['full_text'] == ''      # 脏摘录清除


def test_expand_window_bounds():
    text = 'x' * 50 + 'ABCD' + 'y' * 100
    win = _expand_window(text, 50, 54)
    assert win.startswith('x') and 'ABCD' in win and win.endswith('y')
    # 起点在文件头部时不越界
    assert _expand_window('ABC', 0, 3) == 'ABC'


def test_extract_tender_with_llm_end_to_end(tmp_path):
    """全流程（mock 网关回放"逐字摘录窗口前 40 字"的合规行为）：
    确定性候选 → 扩窗 → 结构化 → 溯源全过 → usage 记账。"""
    p = tmp_path / 'mini_tender.txt'
    p.write_text(SAMPLE, newline='\n', encoding='utf-8')

    def scripted_reply(prompt):
        window = prompt.split('【原文窗口】\n', 1)[1].split('【确定性提取的候选文本】', 1)[0]
        snippet = window.strip()[:40]
        return json.dumps({
            'is_requirement': True,
            'clause_type': 'other',
            'clause_no': '',
            'full_text': snippet,      # 逐字来自窗口 → 溯源必过
            'reason': '测试回放',
        }, ensure_ascii=False)

    client = MockClient([])  # replies 由 reply_fn 动态生成——改用子类
    client.chat = lambda messages, model_tier='flash', **kw: (
        client.prompts.append(messages[0]['content']),
        MockResp(scripted_reply(messages[0]['content']))
    )[1]

    result = extract_tender_with_llm(str(p), client=client)
    n_calls = len(result['llm_structured'])
    assert n_calls > 0                                  # ★▲要求 + 模式A/B 候选都送了
    assert all(c['quote_verified'] for c in result['llm_structured'])
    assert result['llm_usage']['calls'] == n_calls
    # 说明语/表格碎片不送 LLM（只送 requirement 与措辞型候选）
    assert all(c.get('kind') in (None, 'requirement') for c in result['llm_structured'])


def test_extract_tender_no_candidates_skips_llm(tmp_path):
    """无任何候选时零调用（0 Token 短路）。"""
    p = tmp_path / 'empty.txt'
    p.write_text('只是一段没有任何强制条款的普通文本。\n', newline='\n', encoding='utf-8')
    client = MockClient([])
    result = extract_tender_with_llm(str(p), client=client)
    assert result['llm_structured'] == []
    assert result['llm_usage']['calls'] == 0
    assert client.prompts == []                         # 网关未被触碰
