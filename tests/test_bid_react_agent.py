# -*- coding: utf-8 -*-
"""F7 标书专家 ReAct 追问引擎单元测试（0 Token 离线可跑）。

覆盖任务卡验收点：
1. mock 双书 + MockClient 走完整 ReAct 两轮（1 次工具调用 + final）-> 答案附 quote 全部溯源通过，
   携带准确字符偏移（docx 页码为 None 绝不编造）；
2. MockClient 返回幻觉 quote 被 verify_quote 拦截清空并标注；
3. client=None 关键词降级模式（mode='keyword_fallback'）可用；
4. max_rounds=6 轮上限防死循环生效；
5. 四类挂载工具（search_tender/search_bid/locate_chapter/recalc_price/check_cert_expiry）独立验证。
"""
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from docx import Document

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.bid_react_agent import (
    ask_bid_expert,
    check_cert_expiry,
    locate_chapter,
    recalc_price,
    search_bid,
    search_tender,
)


@dataclass
class _MockResp:
    content: str


class MockReActLLMClient:
    def __init__(self, handler):
        self.handler = handler
        self.calls = 0

    def chat(self, messages, model_tier='flash', **kwargs):
        self.calls += 1
        return _MockResp(content=self.handler(messages, self.calls))


def _save_temp_docx(paragraphs: list[str]) -> Path:
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    fd, path = tempfile.mkstemp(suffix='.docx')
    os.close(fd)
    doc.save(path)
    return Path(path)


# ── 1. 完整两轮 ReAct（第1轮调工具检索 + 第2轮 final 回答）且 quote 100% 溯源通过 ──

def test_react_two_rounds_tool_and_final_verified():
    tender_paras = [
        '第一章 投标邀请',
        '★1. 本项目免费质保期要求：自终验合格之日起不少于 36 个月。',
        '上述为商务质保基线要求。',
        '第五章 评标办法与评分标准',
        '技术部分满分 50 分，商务部分满分 20 分，价格分满分 30 分。',
    ]
    bid_paras = [
        '一、商务条款响应表',
        '我公司郑重承诺：本项目提供自终验合格之日起 36 个月的免费质保期服务。',
        '上述为我方售后维保承诺。',
    ]
    tender_p = _save_temp_docx(tender_paras)
    bid_p = _save_temp_docx(bid_paras)

    def _handler(messages, call_idx):
        if call_idx == 1:
            return json.dumps({
                'thought': '先在招标文件和投标文件中检索“质保期”相关规定与响应',
                'action': 'search_tender',
                'action_input': '质保期',
            }, ensure_ascii=False)
        return json.dumps({
            'thought': '已拿到招标文件质保期原文，输出最终答案',
            'action': 'final',
            'action_input': {
                'answer': '招标文件要求免费质保期不少于 36 个月，我方投标书响应为 36 个月，完全满足。',
                'quotes': [
                    '★1. 本项目免费质保期要求：自终验合格之日起不少于 36 个月。',
                    '我公司郑重承诺：本项目提供自终验合格之日起 36 个月的免费质保期服务。',
                ],
            },
        }, ensure_ascii=False)

    try:
        client = MockReActLLMClient(_handler)
        res = ask_bid_expert(tender_p, bid_p, '质保期招标要求几年？我们响应在哪？', client=client)

        assert res['mode'] == 'react'
        assert res['rounds_used'] == 2
        assert res['terminated_by_max_rounds'] is False
        assert len(res['steps']) == 2
        assert res['steps'][0]['action'] == 'search_tender'
        assert res['steps'][1]['action'] == 'final'

        assert '36 个月' in res['answer']
        assert len(res['quotes']) == 2
        for q in res['quotes']:
            assert q['quote_verified'] is True
            assert q['quote'] != ''
            assert isinstance(q['offset'], int)
            assert q['page'] is None  # docx 不编造页码
    finally:
        tender_p.unlink(missing_ok=True)
        bid_p.unlink(missing_ok=True)


# ── 2. 幻觉 quote 被 verify_quote 拦截清空并标注 ──────────────────────────

def test_react_hallucinated_quote_is_cleared_and_flagged():
    tender_p = _save_temp_docx(['★1. 投标保证金金额为人民币 20000 元。', '上述为保证金要求。'])
    bid_p = _save_temp_docx(['我方已按要求缴纳投标保证金 20000 元。'])

    def _handler(messages, call_idx):
        return json.dumps({
            'thought': '直接回答',
            'action': 'final',
            'action_input': {
                'answer': '保证金为 20000 元。',
                'quotes': [
                    '★1. 投标保证金金额为人民币 20000 元。',
                    '本项目不需要缴纳任何投标保证金（此句为大模型幻觉捏造）',
                ],
            },
        }, ensure_ascii=False)

    try:
        client = MockReActLLMClient(_handler)
        res = ask_bid_expert(tender_p, bid_p, '投标保证金是多少？', client=client)
        assert len(res['quotes']) == 2
        # 第 1 条真实引用通过
        assert res['quotes'][0]['quote_verified'] is True
        assert '20000' in res['quotes'][0]['quote']
        # 第 2 条幻觉引用被清空并标注
        assert res['quotes'][1]['quote_verified'] is False
        assert res['quotes'][1]['quote'] == ''
        assert '未通过原文溯源' in res['quotes'][1]['note']
    finally:
        tender_p.unlink(missing_ok=True)
        bid_p.unlink(missing_ok=True)


# ── 3. client=None 时 0 Token 关键词降级可用 ─────────────────────────────

def test_keyword_fallback_when_client_is_none():
    tender_p = _save_temp_docx(['★1. 免费质保期不少于三年。', '上述为商务要求。'])
    bid_p = _save_temp_docx(['售后响应：我方提供三年免费质保期。'])

    try:
        res = ask_bid_expert(tender_p, bid_p, '质保期要求多久？', client=None)
        assert res['mode'] == 'keyword_fallback'
        assert len(res['quotes']) >= 1
        assert '质保期' in res['answer']
        for q in res['quotes']:
            assert q['quote_verified'] is True
            assert isinstance(q['offset'], int)
    finally:
        tender_p.unlink(missing_ok=True)
        bid_p.unlink(missing_ok=True)


# ── 4. max_rounds=6 上限生效（防死循环）──────────────────────────────────

def test_react_max_rounds_cap_prevents_infinite_loop():
    tender_p = _save_temp_docx(['第一章 招标公告：质保期三年。'])
    bid_p = _save_temp_docx(['投标响应：质保期三年。'])

    def _endless_tool_handler(messages, call_idx):
        return json.dumps({
            'thought': f'第 {call_idx} 轮继续检索',
            'action': 'search_tender',
            'action_input': '质保期',
        }, ensure_ascii=False)

    try:
        client = MockReActLLMClient(_endless_tool_handler)
        res = ask_bid_expert(tender_p, bid_p, '质保期多久？', client=client, max_rounds=6)
        # E32：达到 max_rounds 后会追加 1 轮强制收口 LLM 调用（本桩持续返 tool 故收口仍记 1 次调用）
        assert client.calls == 7
        assert res['rounds_used'] == 6
        assert res['terminated_by_max_rounds'] is True
        assert res['forced_final'] is False
        assert len(res['steps']) == 6
    finally:
        tender_p.unlink(missing_ok=True)
        bid_p.unlink(missing_ok=True)


# ── 5. 四类确定性工具独立调用验证 ─────────────────────────────────────────

def test_deterministic_tools_standalone():
    t_txt = '第一章 投标邀请\n本系统质保期为36个月。\n第五章 评标办法\n价格分30分。'
    b_txt = '我方响应质保期为36个月，ISO证书有效期至2026-10-01。'

    assert len(search_tender('质保期', tender_text=t_txt)) == 1
    assert len(search_bid('质保期', bid_text=b_txt)) == 1
    ch = locate_chapter(t_txt, '评标办法')
    assert len(ch) >= 1 and ch[0]['index'] == '第五章'

    p_res = recalc_price([{'name': 'A', 'qty': 2, 'unit_price': 100, 'total_price': 205}])
    assert p_res['passed'] is False
    assert p_res['findings'][0]['code'] == 'ITEM_ARITHMETIC'

    c_res = check_cert_expiry(b_txt, open_date='2026-11-04')
    assert c_res['passed'] is False
    assert c_res['findings'][0]['code'] == 'CERT_EXPIRED'