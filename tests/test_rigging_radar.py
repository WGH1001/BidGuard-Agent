# -*- coding: utf-8 -*-
"""F6 投前控标雷达单元测试（0 Token 离线可跑）。

覆盖任务卡验收点：
1. mock 招标书埋入 2 个排他参数（"参照XX品牌 ZXR10 型号"、"仅限具备 XX 原厂授权"）
   + 1 个合法国产化要求（不得误报），★/▲ 条款间插入非标记中性行 -> 疑似清单 100% 命中、正常项零误报；
2. 所有条目必须带"疑似"字样标注，conclusion.recommendation 只做参考意见；
3. LLM 模式 + verify_quote 强制溯源：编造 quote 直接丢弃；
4. 11 份真实语料 0 Token 扫描跑通统计报告。
"""
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from docx import Document

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.rigging_radar import dry_run_corpus_radar, scan_rigging_radar


@dataclass
class _MockResp:
    content: str


class MockRadarLLMClient:
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


# ── 1. 核心验收场景：2 个排他参数 100% 命中 + 1 个合法国产化要求零误报 ──────

def test_rigging_radar_hits_two_exclusive_and_ignores_legitimate_domestic():
    # 注意遵循 F5 轮固定纪律：各 ★ 条款之间插入非标记中性行，防止连续同标记行合并
    tender_paras = [
        '第三章 采购需求与技术规格要求',
        '★1. 核心交换机设备须参照中兴品牌 ZXR10 型号的技术指标与硬件架构进行配置。',
        '上述为网络交换设备的基础选型说明。',
        '★2. 投标人针对本项目所投核心安全网关，仅限具备奇安信原厂授权的代理商参与投标，须提供原厂唯一授权书。',
        '上述为售后服务与渠道资质说明。',
        '★3. 系统平台须满足国家信创与国产化自主可控要求，全面适配银河麒麟操作系统 V10 与达梦数据库 DM8。',
        '上述为运行环境基线要求。',
    ]
    tender_p = _save_temp_docx(tender_paras)

    try:
        res = scan_rigging_radar(tender_p, client=None)
        assert res['llm_status'] == 'skipped'
        params = res['suspicious_params']

        # 精确命中前 2 条排他参数，第 3 条合法国产化要求零误报
        assert len(params) == 2
        all_quotes = ' | '.join(p['quote'] for p in params)
        assert 'ZXR10' in all_quotes
        assert '原厂' in all_quotes
        assert '银河麒麟' not in all_quotes
        assert '自主可控' not in all_quotes

        # 所有检出条目及结论必须带"疑似"字样
        types = {p['exclusive_type'] for p in params}
        assert '品牌绑定' in types
        assert '资质定向' in types
        for p in params:
            assert '疑似' in p['reason']
            assert p['quote_verified'] is True
            assert isinstance(p['offset'], int)

        assert res['conclusion']['recommendation'] == '谨慎投'
        assert '疑似' in res['conclusion']['summary']
    finally:
        tender_p.unlink(missing_ok=True)


# ── 2. LLM 层核验 + verify_quote 拦截丢弃编造 quote ──────────────────────

def test_rigging_radar_llm_layer_discards_hallucinated_quote():
    tender_paras = [
        '★1. 接入交换机须参照中兴品牌 ZXR10 型号提供同档次设备。',
        '上述为第一项设备参数说明。',
        '★2. 投标人仅限具备某特定厂商原厂授权资质的单位。',
        '上述为第二项资质要求说明。',
    ]
    tender_p = _save_temp_docx(tender_paras)

    def _handler(prompt, call_idx):
        # 注意：候选窗口可能包含邻近条款文本（★2 的窗口会带 ★1 的 ZXR10 字样），
        # 按关键词分流会误路由——按调用序号分流（第 1 个候选=★1，第 2 个=★2）
        if call_idx == 1:
            return json.dumps({
                'is_exclusive': True,
                'exclusive_type': '品牌绑定',
                'confidence': 0.95,
                'quote': '★1. 接入交换机须参照中兴品牌 ZXR10 型号提供同档次设备。',
                'reason': '疑似指定中兴 ZXR10 特定型号控标',
            }, ensure_ascii=False)
        # 第二条故意返回一段原文中不存在的编造 quote，应被 verify_quote 拦截丢弃
        return json.dumps({
            'is_exclusive': True,
            'exclusive_type': '资质定向',
            'confidence': 0.90,
            'quote': '投标人必须具有宇宙第一安全厂商颁发的特级独家代理证（此句为编造）',
            'reason': '疑似原厂授权排他',
        }, ensure_ascii=False)

    try:
        client = MockRadarLLMClient(_handler)
        res = scan_rigging_radar(tender_p, client=client)
        assert res['llm_status'] == 'done'
        # 编造的第二条被直接丢弃，只保留通过 verify_quote 的第一条
        assert len(res['suspicious_params']) == 1
        assert 'ZXR10' in res['suspicious_params'][0]['quote']
        assert '疑似' in res['suspicious_params'][0]['reason']
    finally:
        tender_p.unlink(missing_ok=True)


# ── 3. 11 份真实语料 0 Token 扫描统计报告验证 ────────────────────────────

def test_corpus_11_samples_zero_token_radar_scan():
    report = dry_run_corpus_radar('data/corpus/m0_samples')
    if report:
        assert len(report) == 11
        for row in report:
            assert row['recommendation'] in ('值得投', '谨慎投', '疑似控标高')
            assert isinstance(row['suspicious_count'], int)