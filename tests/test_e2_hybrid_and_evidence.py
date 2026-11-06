# -*- coding: utf-8 -*-
"""E2 · 智能核验与混合检索升维卡单元测试（升级点 3 缺证明材料探针 + 升级点 1 三级混合检索）。

覆盖任务卡 §3 全部测试要求（断言严格数数，严禁空循环假绿）：
1. 门控直通（Gate Filter）：条款号/标题直接命中场景 -> 断言未进入混合检索路径（hybrid_stats['total_calls'] == 0）；
2. 同义词改写召回（F4 + F7）：
   - F4：招标要求"项目负责人须具备信息系统项目管理师证书"，投标书写"拟派项目经理持有信息系统项目管理师证书（见附件2）"
         （同义词改写、不抄原句） -> 断言 F4 判 RESPONSE_OK 零误报漏答；
   - F7：提问或调用 search_bid("项目经理") 命中写有"拟派项目负责人"的段落；
   - BM25 Level 1 词库规模 >= 40 组且单次检索耗时在毫秒级；
3. MISSING_EVIDENCE 探针：
   - 投标书只喊"完全满足"无任何锚点 -> 100% 产出 MISSING_EVIDENCE(warning)，且 star_results 主状态保持 RESPONSE_OK + evidence_warning；
   - 带"见附件"锚点 -> 不产出 MISSING_EVIDENCE（保护既有单测零回归）；
4. F5 偏离表兼容：
   - 构造 MISSING_EVIDENCE 场景跑 export_deviation_table -> 偏离情况列保持"无偏离"，
     我方投标响应列末尾含"（注：请核实已附对应证明材料）"；
5. Level 3 离线无 Key 兜底：无 Key 环境下全链路零报错（embedding 不触发）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

from docx import Document

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.bid_react_agent import ask_bid_expert, search_bid, search_tender
from core.deviation_exporter import export_deviation_table
from core.hybrid_retriever import (
    BID_SYNONYM_GROUPS,
    get_hybrid_stats,
    hybrid_retrieve_windows,
    reset_hybrid_stats,
)
from core.response_checker import check_star_responses, run_response_check


def _save_temp_docx(paragraphs: list[str]) -> Path:
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    fd, path = tempfile.mkstemp(suffix='.docx')
    os.close(fd)
    doc.save(path)
    return Path(path)


# ── 1. 门控直通测试：直接命中场景 0 次调用 hybrid_retriever ────────────────

def test_gate_filter_direct_hit_bypasses_hybrid_retriever():
    reset_hybrid_stats()
    tender_text = (
        '第一章 技术要求\n'
        '★1. 系统服务端必须支持国产银河麒麟高级服务器操作系统 V10。\n'
        '上述为操作系统要求。\n'
        '★2. 日志采集模块单节点并发处理能力不得低于 5000 EPS。\n'
    )
    bid_text = (
        '技术响应表\n'
        '★1. 系统服务端必须支持国产银河麒麟高级服务器操作系统 V10。（我方完全满足）\n'
        '★2. 日志采集模块单节点并发处理能力不得低于 5000 EPS。（我方实测达 8000 EPS）\n'
    )

    out = check_star_responses(tender_text, bid_text, client=None)
    stats = get_hybrid_stats()

    assert out['alignment_summary']['total_requirements'] == 2
    assert out['alignment_summary']['aligned_hits'] == 2
    assert out['alignment_summary']['direct_gate_hits'] == 2
    assert out['alignment_summary']['hybrid_gate_hits'] == 0
    # 断言门控生效：直接命中的 2 条要求 100% 走直通路径，混合检索调用次数严格为 0
    assert stats['total_calls'] == 0


# ── 2. 同义词改写召回测试（F4 零误报漏答 + F7 跨同义词检索 + BM25 性能与词库规模）──

def test_synonym_rewrite_recall_in_f4_and_f7():
    # 断言同义词组数量 >= 40 组
    assert len(BID_SYNONYM_GROUPS) >= 40

    reset_hybrid_stats()
    # 招标要求写"拟派项目负责人"与"高可用灾备"，投标书不抄原句，改写为"项目经理"与"双活集群故障接管"
    tender_text = (
        '第三章 人员与架构要求\n'
        '★1. 投标人拟派项目负责人须具备信息系统项目管理师认证资格。\n'
        '上述为人员资格基线。\n'
        '★2. 核心业务节点必须具备完善的高可用灾备能力。\n'
    )
    bid_text = (
        '一、项目团队配置方案\n'
        '我司指派的项目经理张工持有信息系统项目管理师认证资格（证书编号：20250889）。\n'
        '二、系统架构设计\n'
        '平台底层采用双活集群故障接管架构，主备节点秒级切换。\n'
    )

    t0 = time.perf_counter()
    out = check_star_responses(tender_text, bid_text, client=None)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    # 断言 F4 通过混合检索全部召回并判为 RESPONSE_OK，零误报漏答
    assert out['alignment_summary']['total_requirements'] == 2
    assert out['alignment_summary']['aligned_hits'] == 2
    assert out['alignment_summary']['hybrid_gate_hits'] >= 1
    assert len(out['findings']) == 0
    assert all(r['status'] == 'RESPONSE_OK' for r in out['results'])
    assert get_hybrid_stats()['total_calls'] >= 1
    assert elapsed_ms < 500.0

    # 断言 F7 search_tender / search_bid 搜"项目经理"能命中写有"拟派项目负责人"的段落
    t_hits = search_tender('项目经理', tender_text=tender_text)
    assert len(t_hits) >= 1
    assert '项目负责人' in t_hits[0]['quote']

    b_hits = search_bid('拟派项目负责人', bid_text=bid_text)
    assert len(b_hits) >= 1
    assert '项目经理' in b_hits[0]['quote']


# ── 3. MISSING_EVIDENCE 探针测试（口号式响应 vs 带"见附件"锚点）────────────

def test_missing_evidence_probe_warning_and_anchor_exemption():
    tender_text = (
        '第一章 实质性要求\n'
        '★1. 数据库层必须原生兼容达梦数据库管理系统 DM8，并提供互认证书。\n'
        '上述为第一项要求。\n'
        '★2. 核心加密模块必须支持国密算法，并提供第三方权威机构出具的检测报告复印件。\n'
    )
    # 投标书：★1 带有"见附件"锚点（合法免报）；★2 只抄原句喊口号"（我方完全满足，无偏离）"，无任何附件或编号锚点
    bid_text = (
        '技术响应表\n'
        '★1. 数据库层必须原生兼容达梦数据库管理系统 DM8，并提供互认证书。（我方完全满足，证书见附件）\n'
        '★2. 核心加密模块必须支持国密算法，并提供第三方权威机构出具的检测报告复印件。（我方完全满足，无偏离）\n'
    )

    out = check_star_responses(tender_text, bid_text, client=None)
    assert len(out['results']) == 2

    # 两条主状态均保持 RESPONSE_OK
    assert out['results'][0]['status'] == 'RESPONSE_OK'
    assert out['results'][0]['evidence_warning'] is None

    assert out['results'][1]['status'] == 'RESPONSE_OK'
    assert out['results'][1]['evidence_warning'] == '（注：请核实已附对应证明材料）'

    # findings 中仅对 ★2 产出 1 条 MISSING_EVIDENCE (severity='warning')
    assert len(out['findings']) == 1
    f = out['findings'][0]
    assert f['code'] == 'MISSING_EVIDENCE'
    assert f['severity'] == 'warning'
    assert '检测报告' in f['message']


# ── 4. F5 偏离表兼容测试（偏离列保持"无偏离"，响应列末尾含核实注记）────────

def test_f5_deviation_table_compatibility_with_missing_evidence():
    tender_paras = [
        '第一章 采购需求',
        '★1. 核心安全网关须支持流量清洗，并提供公安部颁发的销售许可证扫描件。',
        '上述为安全网关资质要求。',
        '★2. 系统服务端须支持银河麒麟操作系统 V10。',
    ]
    # ★1 仅喊口号"我方完全满足"无任何附件锚点 -> 触发 MISSING_EVIDENCE
    bid_paras = [
        '技术响应表',
        '★1. 核心安全网关须支持流量清洗，并提供公安部颁发的销售许可证扫描件。（我方完全满足）',
        '★2. 系统服务端须支持银河麒麟操作系统 V10。（我方完全满足）',
    ]

    t_path = _save_temp_docx(tender_paras)
    b_path = _save_temp_docx(bid_paras)
    fd, out_str = tempfile.mkstemp(suffix='.docx')
    os.close(fd)
    out_path = Path(out_str)

    try:
        dev_res = export_deviation_table(t_path, b_path, out_path, client=None)
        assert dev_res['total_rows'] == 2
        assert dev_res['no_deviation_count'] == 2
        assert dev_res['negative_deviation_count'] == 0

        row1 = dev_res['rows'][0]
        assert row1['deviation'] == '无偏离'
        assert row1['bid_response'].endswith('（注：请核实已附对应证明材料）')

        # 打开生成的 Word 表格断言单元格真实文字
        gen_doc = Document(str(out_path))
        tbl = gen_doc.tables[0]
        assert tbl.rows[1].cells[4].text.strip() == '无偏离'
        assert tbl.rows[1].cells[3].text.strip().endswith('（注：请核实已附对应证明材料）')
    finally:
        t_path.unlink(missing_ok=True)
        b_path.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)


# ── 5. Level 3 无 Key 环境全链路零报错兜底测试 ──────────────────────────────

def test_level3_zero_key_fallback_no_error(monkeypatch):
    monkeypatch.delenv('ZHIPU_API_KEY', raising=False)
    monkeypatch.delenv('PRIVATE_API_KEY', raising=False)

    t_path = _save_temp_docx([
        '第一章 商务要求',
        '★1. 投标人拟派项目负责人须具备 5 年以上同类项目经验。',
        '上述为人员经验要求。',
    ])
    b_path = _save_temp_docx([
        '团队配置：我方指派的项目经理李工具有 8 年同类项目经验。',
    ])

    try:
        # F4 端到端无 Key 0-Token 运行
        r_check = run_response_check(t_path, b_path, client=None)
        assert r_check['alignment_summary']['aligned_hits'] == 1
        assert len(r_check['findings']) == 0

        # F7 端到端无 Key 0-Token 追问运行
        r_qa = ask_bid_expert(t_path, b_path, '项目经理要求几年经验？', client=None)
        assert r_qa['mode'] == 'keyword_fallback'
        assert len(r_qa['quotes']) >= 1

        # 直接调用 hybrid_retrieve_windows 无 client 走 Level 1
        hits = hybrid_retrieve_windows('项目经理经验', '我方拟派项目负责人具有8年经验。', client=None)
        assert len(hits) == 1
        assert hits[0]['level'] == 'level1_bm25_synonym'
    finally:
        t_path.unlink(missing_ok=True)
        b_path.unlink(missing_ok=True)