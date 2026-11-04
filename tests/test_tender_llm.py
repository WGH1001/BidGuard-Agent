# -*- coding: utf-8 -*-
"""F1 LLM 分段抽取层单元测试（mock 网关，全程 0 Token、不联网）。"""
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.tender_llm import (
    _expand_window,
    _parse_llm_json,
    extract_scoring_with_llm,
    extract_tender_with_llm,
    structure_candidate,
)
from core.tender_extractor import extract_scoring_tables
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
        'consequence': 'veto',
        'clause_no': '★1)',
        'full_text': '★1) CPU 核数不低于 32 核，内存不低于 128G',
        'reason': '实质性硬件参数要求',
    }, ensure_ascii=False)
    out = structure_candidate(window, cand, MockClient([reply]))
    assert out['quote_verified'] is True
    assert out['full_text'] == '★1) CPU 核数不低于 32 核，内存不低于 128G'
    assert out['structured']['clause_type'] == 'star_param'
    assert out['structured']['consequence'] == 'veto'   # 评分后果字段透传


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


def test_extract_scoring_with_llm_prose(tmp_path):
    """段落式评分抽取（mock 网关）：锚点开窗 → 逐窗提取 → detail 逐项溯源。
    溯源通过的 detail 采信；编造的 detail 清空待人工。"""
    body = (
        '第五章 评标方法与评标标准\n'
        '评分办法：综合评分法，满分100分。\n'
        '价格分：满足招标文件要求且投标价格最低的投标报价为评标基准价，其价格分为满分10分。\n'
        '技术分：技术方案完整可行的得8分，每有一处缺陷扣1分。\n'
        '业绩分：提供近三年类似合同业绩的每份得2分，须提供合同复印件，最高得6分。\n'
    )
    p = tmp_path / 'prose_tender.txt'
    p.write_text(body, newline='\n', encoding='utf-8')

    def scripted_reply(prompt):
        window = prompt.split('【原文窗口】\n', 1)[1].split('每项字段', 1)[0]
        return json.dumps([
            {'factor': '价格分', 'detail': '价格分：满足招标文件要求且投标价格最低的投标报价为评标基准价，其价格分为满分10分。',
             'score': '10', 'score_max': 10, 'evidence_material': '', 'consequence': 'none'},
            {'factor': '业绩分', 'detail': '编造的评分标准不在窗口里', 'score': '6',
             'score_max': 6, 'evidence_material': '合同复印件', 'consequence': 'none'},
        ], ensure_ascii=False)

    class FnClient(MockClient):
        def chat(self, messages, model_tier='flash', **kw):
            self.prompts.append(messages[0]['content'])
            return MockResp(scripted_reply(messages[0]['content']))

    client = FnClient([])
    result = extract_scoring_with_llm(str(p), client=client)
    prose = result['scoring_prose']
    assert len(prose) == 2
    assert prose[0]['quote_verified'] is True and prose[0]['score_max'] == 10
    assert prose[1]['quote_verified'] is False and prose[1]['detail'] == ''
    assert prose[1]['detail_raw'].startswith('编造')     # 脏摘录留痕待人工
    assert result['scoring_tables'] == []                # txt 无表格
    assert result['scoring_llm_usage']['calls'] >= 1


def test_scoring_tables_docx(tmp_path):
    """表格型评分表 0 Token 解析：分节行归组、分值范围取上限、列名变体兼容。"""
    from docx import Document
    doc = Document()
    tb = doc.add_table(rows=5, cols=4)
    for i, h in enumerate(('序号', '评审因素', '评分标准', '分值')):
        tb.rows[0].cells[i].text = h
    tb.rows[1].cells[0].text = '1.价格'                      # 分节行（单格非空）
    row = tb.rows[2].cells
    row[0].text, row[1].text, row[2].text, row[3].text = '1.1', '价格分', '满足要求得满分', '20'
    tb.rows[3].cells[0].text = '1.2'
    tb.rows[3].cells[1].text = '偏离扣分'
    tb.rows[3].cells[2].text = '每有一处负偏离扣1分'
    tb.rows[3].cells[3].text = '0-5'                          # 分值范围 → 取上限 5
    p = tmp_path / 'tbl_tender.docx'
    doc.save(str(p))
    items = extract_scoring_tables(str(p))
    assert len(items) == 2
    assert items[0]['section'] == '1.价格' and items[1]['section'] == '1.价格'
    assert items[0]['score_max'] == 20
    assert items[1]['score_max'] == 5                         # 范围取上限
    assert '负偏离' in items[1]['detail']
