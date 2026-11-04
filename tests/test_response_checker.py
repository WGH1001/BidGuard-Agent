# -*- coding: utf-8 -*-
"""F4 ★条款逐条响应核验 + 标书前后矛盾检测单元测试（0 Token 离线可跑）。

覆盖任务卡全部验收点：
1. mock 投标书 docx（对 3 条 ★ 全文照抄响应、漏答 1 条、质保期前后矛盾 1 组：两年 vs 三年）：
   要求漏答/矛盾 100% 检出、正常响应零误报；
2. 强制 quote_verifier 溯源：MockClient 返回伪造/改写摘录时，自动拦截并清空 bid_quote + quote_verified=False；
3. 纪律核验：usage_note 与 table_marker 绝不送入核验；client=None 时 0 Token 降级正常工作；
4. 页码与偏移核验：docx 页码为 None（绝不编造页码），双方字符偏移准确。
"""
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from docx import Document

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.response_checker import (
    check_bid_self_contradictions,
    check_star_responses,
    run_response_check,
)


@dataclass
class _MockResp:
    content: str


class MockLLMClient:
    """离线测试用 Mock 大模型客户端（按预设函数或队列返回 JSON）。"""

    def __init__(self, handler):
        self.handler = handler
        self.calls = 0

    def chat(self, messages, model_tier='flash', **kwargs):
        self.calls += 1
        prompt = messages[-1]['content']
        return _MockResp(content=self.handler(prompt, self.calls))


def _save_temp_docx(paragraphs: list[str]) -> Path:
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    fd, path = tempfile.mkstemp(suffix='.docx')
    os.close(fd)
    doc.save(path)
    return Path(path)


# ── 1. 核心验收场景：3条★照抄响应 + 1条★漏答 + 1组质保期前后矛盾（两年 vs 三年）──

def test_mock_bid_docx_three_ok_one_missing_one_contradiction():
    tender_paras = [
        '第一章 采购需求说明',
        '本文件中打“★”内容为实质性要求，投标人必须逐条作出明确响应。',  # usage_note，应被自动排除
        '核心平台技术参数要求如下：',
        '★1. 系统服务端必须支持国产银河麒麟高级服务器操作系统 V10。',
        '上述为运行环境基线要求。',
        '★2. 数据库层必须原生兼容达梦数据库管理系统 DM8，并提供互认证书。',
        '数据库选型须满足国产化要求。',
        '★3. 日志采集模块单节点并发处理能力不得低于 5000 EPS。',
        '采集性能按单节点实测计。',
        '★4. 投标人必须配备至少 2 名持有 CISP 认证证书的驻场安全工程师。',
    ]
    # 投标书：前 3 条 ★ 全文照抄响应；漏答 ★4（CISP 驻场）；且质保期前文写三年、后文写两年
    bid_paras = [
        '一、商务响应表',
        '我司郑重承诺：本项目提供自终验合格之日起三年的免费质保期服务。',
        '二、技术要求逐条响应',
        '★1. 系统服务端必须支持国产银河麒麟高级服务器操作系统 V10。（我方完全满足，正响应）',
        '★2. 数据库层必须原生兼容达梦数据库管理系统 DM8，并提供互认证书。（我方完全满足，证书见附件）',
        '★3. 日志采集模块单节点并发处理能力不得低于 5000 EPS。（我方实测达 8000 EPS，正响应）',
        '三、售后服务方案',
        '7.1 免费维保期限：我司对本系统提供自验收之日起两年的免费质保期，期满后收取维保费。',
    ]

    tender_p = _save_temp_docx(tender_paras)
    bid_p = _save_temp_docx(bid_paras)

    try:
        # 先测 client=None (0 Token 确定性链路)
        out_det = run_response_check(tender_p, bid_p, client=None)
        codes_det = [f['code'] for f in out_det['findings']]

        # 4 条真 ★ 要求（说明语被过滤），命中 3 条，漏答 1 条（★4）
        assert out_det['alignment_summary']['total_requirements'] == 4
        assert out_det['alignment_summary']['aligned_hits'] == 3
        assert codes_det.count('MISSING_RESPONSE') == 1
        assert codes_det.count('CONTRADICTION') == 1

        missing_f = next(f for f in out_det['findings'] if f['code'] == 'MISSING_RESPONSE')
        assert 'CISP' in missing_f['tender_quote']
        assert missing_f['bid_quote'] == ''
        assert missing_f['tender_page'] is None  # docx 不伪造页码
        assert isinstance(missing_f['tender_start'], int)

        contra_f = next(f for f in out_det['findings'] if f['code'] == 'CONTRADICTION')
        assert '三年' in contra_f['tender_quote']
        assert '两年' in contra_f['bid_quote']
        assert contra_f['quote_verified'] is True

        # 正常响应的 3 条 ★ 均在 star_results 中且 status == 'RESPONSE_OK'（零误报）
        ok_items = [r for r in out_det['star_results'] if r['status'] == 'RESPONSE_OK']
        assert len(ok_items) == 3
    finally:
        tender_p.unlink(missing_ok=True)
        bid_p.unlink(missing_ok=True)


# ── 2. 大模型① 逐条核验 + quote_verifier 防幻觉拦截测试 ──────────────────

def test_llm_response_check_and_hallucination_rejection():
    tender_text = (
        '★1. 平台必须支持国密 SM2/SM3/SM4 算法加密传输。\n'
        '安全要求之外，服务要求如下：\n'
        '★2. 免费质保期自终验合格之日起不少于 36 个月。\n'
    )
    bid_text = (
        '响应 ★1：本平台内置国密模块，完全支持国密 SM2/SM3/SM4 算法加密传输。\n'
        '响应 ★2：本项目免费质保期为 24 个月。\n'
    )

    def _handler(prompt, call_idx):
        # 注意：相邻条款窗口可能互相包含对方关键词，按调用序号分流（首个=★1，第二个=★2）
        if call_idx == 1:
            return json.dumps({
                'status': 'RESPONSE_OK',
                'tender_quote': '★1. 平台必须支持国密 SM2/SM3/SM4 算法加密传输。',
                'bid_quote': '完全支持国密 SM2/SM3/SM4 算法加密传输',
                'reason': '正面响应满足要求',
            }, ensure_ascii=False)
        # 第二条返回 CONTRADICTION，但故意给一段不在投标书原文中的伪造 bid_quote
        return json.dumps({
            'status': 'CONTRADICTION',
            'tender_quote': '★2. 免费质保期自终验合格之日起不少于 36 个月。',
            'bid_quote': '我公司只能提供一年半的质保服务（此句为大模型幻觉改写）',
            'reason': '招标要求36个月，投标仅24个月',
        }, ensure_ascii=False)

    mock_client = MockLLMClient(_handler)
    res = check_star_responses(tender_text, bid_text, client=mock_client)

    assert len(res['results']) == 2
    # 第一条 RESPONSE_OK 不进 findings
    assert res['results'][0]['status'] == 'RESPONSE_OK'
    assert res['results'][0]['quote_verified'] is True

    # 第二条进 findings，且伪造的 bid_quote 被 quote_verifier 拦截清空
    assert len(res['findings']) == 1
    f = res['findings'][0]
    assert f['code'] == 'CONTRADICTION'
    assert f['quote_verified'] is False
    assert f['bid_quote'] == ''


# ── 3. usage_note 与 table_marker 过滤纪律核验 ──────────────────────────

def test_usage_note_and_table_marker_are_excluded():
    reqs = [
        {'mark': '★', 'text': '本文件中的“★”内容为实质性要求', 'start': 0, 'end': 20, 'kind': 'usage_note'},
        {'mark': '★', 'text': '工业★', 'start': 25, 'end': 28, 'kind': 'table_marker'},
        {'mark': '★', 'text': '★1. 核心交换机背板带宽 ≥ 2.56Tbps。', 'start': 30, 'end': 55, 'kind': 'requirement'},
    ]
    tender_text = '本文件中的“★”内容为实质性要求\n工业★\n★1. 核心交换机背板带宽 ≥ 2.56Tbps。'
    bid_text = '我方响应：★1. 核心交换机背板带宽 ≥ 2.56Tbps，实测为 6.4Tbps。'

    out = check_star_responses(tender_text, bid_text, requirements=reqs, client=None)
    assert out['alignment_summary']['total_requirements'] == 1
    assert out['alignment_summary']['aligned_hits'] == 1
    assert out['findings'] == []


# ── 4. 标书内部多参数自相矛盾检测（总价/响应时间/保证金比例） ────────────

def test_bid_internal_multi_param_contradictions():
    bid_text = (
        '开标一览表：投标总报价：1,200,000.00元。\n'
        '分项说明：投标总报价：1,250,000.00元。\n'
        '服务承诺：接到故障报修后 15 分钟内响应。\n'
        '运维章节：接到故障报修后 2 小时内响应。\n'
    )
    findings = check_bid_self_contradictions(bid_text, client=None)
    params = {f['param'] for f in findings}
    assert 'bid_total_amount' in params
    assert 'response_minutes' in params
    for f in findings:
        assert f['code'] == 'CONTRADICTION'
        assert f['severity'] == 'error'
        assert f['quote_verified'] is True