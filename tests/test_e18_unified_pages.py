# -*- coding: utf-8 -*-
"""E18 · 全系统双轨页码大一统卡单元测试（F1 / F6 / F7 三模块 docx 与 pdf 双格式页码验证）。

覆盖任务卡 §3 全部验收标准：
1. F1（core/tender_extractor.py）：含分页符的多页 .docx 招标书跑 build_review_checklist ->
   marked_clauses / invalid_bid_clauses / chapters 的 page 字段非空且准确命中第 2 页；
2. F6（core/rigging_radar.py）：含分页符的多页 .docx 招标书跑 scan_rigging_radar ->
   排他性参数 suspicious_params 的 page 字段非空且准确命中第 2 页；
3. F7（core/bid_react_agent.py）：
   - ask_bid_expert 与 search_tender / search_bid 对含分页符的 .docx 文档返回非空 page；
   - search_tender 第 4 形参 page_spans 同时兼容新式 unified_ctx (dict) 与旧式 [(start, end, page)] (list)；
4. PDF 回归：三模块对多页 .pdf 输入的 page 计算保持准确无误。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
import pymupdf

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.bid_react_agent import ask_bid_expert, search_bid, search_tender
from core.response_checker import build_unified_page_context
from core.rigging_radar import scan_rigging_radar
from core.tender_extractor import build_review_checklist


def _append_page_break(paragraph) -> None:
    """向 python-docx 段落注入 <w:br w:type="page"/> 分页锚点。"""
    run = paragraph.add_run()
    br = OxmlElement('w:br')
    br.set(qn('w:type'), 'page')
    run._r.append(br)


def _create_two_page_docx() -> Path:
    doc = Document()
    p1 = doc.add_paragraph('第一章 投标邀请：本项目为政务云升级改造采购。')
    _append_page_break(p1)

    doc.add_paragraph('第二章 采购需求与技术规范')
    doc.add_paragraph('★1. 质保期自终验合格之日起不少于 36 个月，否则作无效投标处理。')
    doc.add_paragraph('2. 核心交换机须参照华为 S12700E 系列产品并必須提供原厂针对本项目唯一授权函。')

    fd, path = tempfile.mkstemp(suffix='.docx')
    os.close(fd)
    doc.save(path)
    return Path(path)


def _create_two_page_pdf() -> Path:
    fd, path = tempfile.mkstemp(suffix='.pdf')
    os.close(fd)
    pdf_doc = pymupdf.open()

    p1 = pdf_doc.new_page()
    p1.insert_text((72, 100), '第一章 投标邀请：通用商务说明。', fontname='china-s', fontsize=12)

    p2 = pdf_doc.new_page()
    p2.insert_text(
        (72, 100),
        '第二章 技术要求\n★1. 免费质保期不少于三年，否则作无效投标处理。\n2. 核心防火墙指定采用华为 USG6600 系列型号。',
        fontname='china-s',
        fontsize=12,
    )
    pdf_doc.save(path)
    pdf_doc.close()
    return Path(path)


# ── 1. F1 / F6 / F7 对多页 .docx 输入的双轨页码定位测试 ────────────────────

def test_e18_docx_unified_pages_across_f1_f6_f7():
    docx_p = _create_two_page_docx()
    try:
        # ① F1 build_review_checklist：第 2 页的 ★ 条款与模式 B 废标条款 page == 2
        f1_res = build_review_checklist(str(docx_p))
        reqs = [c for c in f1_res['marked_clauses'] if c.get('kind') == 'requirement']
        assert len(reqs) >= 1
        assert reqs[0]['page'] == 2
        assert len(f1_res['invalid_bid_clauses']) >= 1
        assert f1_res['invalid_bid_clauses'][0]['page'] == 2

        # ② F6 scan_rigging_radar：第 2 页的排他性参数 page == 2
        f6_res = scan_rigging_radar(docx_p, client=None)
        params = f6_res['suspicious_params']
        assert len(params) >= 1
        assert all(p['page'] == 2 for p in params)

        # ③ F7 search_tender / ask_bid_expert：对 docx 返回 page == 2，且兼容旧 list 形参
        ctx = build_unified_page_context(docx_p)
        hits_new = search_tender('质保期', tender_text=open_docx_text(docx_p), page_spans=ctx)
        assert len(hits_new) >= 1
        assert hits_new[0]['page'] == 2

        # 验证旧式 list[(start, end, page_no)] 传参 100% 向后兼容
        hits_legacy = search_tender(
            '质保期',
            tender_text=open_docx_text(docx_p),
            page_spans=[(0, 9999, 7)],
        )
        assert len(hits_legacy) >= 1
        assert hits_legacy[0]['page'] == 7

        qa_res = ask_bid_expert(docx_p, docx_p, '请问质保期是多久？', client=None)
        assert len(qa_res['quotes']) >= 1
        assert qa_res['quotes'][0]['page'] == 2
    finally:
        docx_p.unlink(missing_ok=True)


def open_docx_text(p: Path) -> str:
    from core.parser import parse_document
    return parse_document(str(p)).text


# ── 2. F1 / F6 / F7 对多页 .pdf 输入的页码回归测试 ────────────────────────

def test_e18_pdf_unified_pages_regression_across_f1_f6_f7():
    pdf_p = _create_two_page_pdf()
    try:
        # ① F1 PDF 页码
        f1_res = build_review_checklist(str(pdf_p))
        reqs = [c for c in f1_res['marked_clauses'] if c.get('kind') == 'requirement']
        assert len(reqs) >= 1
        assert reqs[0]['page'] == 2

        # ② F6 PDF 页码
        f6_res = scan_rigging_radar(pdf_p, client=None)
        assert len(f6_res['suspicious_params']) >= 1
        assert f6_res['suspicious_params'][0]['page'] == 2

        # ③ F7 PDF 页码
        ctx = build_unified_page_context(pdf_p)
        hits = search_bid('质保期', bid_text=open_docx_text(pdf_p), page_spans=ctx)
        assert len(hits) >= 1
        assert hits[0]['page'] == 2
    finally:
        pdf_p.unlink(missing_ok=True)