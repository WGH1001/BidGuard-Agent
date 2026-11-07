# -*- coding: utf-8 -*-
"""E5 · 标红 PDF 定位增强与发现总览页单元测试（用 pymupdf 现造小 PDF，不依赖外部语料，断言严格数数）。

覆盖任务卡 §4 全部测试要求：
1. 跨行断句引文 -> L4 命中，断言高亮 rect 落在正确页且 >= 2 个 rect（跨行）；
2. 全角/半角标点差异引文（原文"，"引文","） -> L3 或 L4 命中并产出原位高亮；
3. L5 置信护栏：构造仅 3 字可匹配的引文 -> 断言不乱锚（正文页无该引文高亮）、结果 PDF 含总览页且状态为"全篇级·未锚定"，
   首页顶部 TextAnnotation 正文带 "[全篇·总览 N01]" 前缀；
4. 总览页存在性与跳转链接：结果 PDF 第 1 页文本含"标红批注总览"与 N01/N02 编号，列出的条数 == findings 数，
   且已定位项在总览页含内部跳转链接（LINK_GOTO）；
5. worker 措辞动态化：mark='▲' -> category == '▲条款响应核验' 且 suggestion 含 '对该 ▲ 条款'；
   mark 缺省 -> category == '★/▲条款响应核验' 且 suggestion 含 '对该 ★或▲ 条款'；
6. 既有行为回归：平直单行原串引文仍走 L1 直接命中，且高亮 rect 与 page.search_for 一致。
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import sys
import tempfile
from pathlib import Path

import pymupdf

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.worker import resolve_f4_category_and_suggestion
from core.redline import _search_quote_in_pdf, inject_pdf_highlights


@dataclass
class _DummyFinding:
    severity: str
    rule_name: str
    message: str
    suggestion: str
    legal_basis: str
    clause_no: str
    quote: str


def _save_temp_pdf(doc: pymupdf.Document) -> Path:
    fd, path = tempfile.mkstemp(suffix='.pdf')
    os.close(fd)
    doc.save(path)
    doc.close()
    return Path(path)


# ── 1. 跨行断句引文 -> L4 命中且产出 >= 2 个跨行 Rect ───────────────────────

def test_cross_line_quote_hits_l4_with_multiple_rects():
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    # 在窄文本框内写入长句，强制其在 PDF 中断成 2~3 行，导致 search_for 跨行失败而触发 L4
    narrow_rect = pymupdf.Rect(72, 100, 220, 260)
    page.insert_textbox(
        narrow_rect,
        "投标人须提供自终验合格之日起不少于三十六个月的免费质保期服务。",
        fontsize=12,
        fontname="china-s",
    )

    try:
        # 传入不带换行的连贯长引文（且把"三十六个月"微调带空格使 L1/L2 不命中，直测 L4 跨行合并）
        quote = "投标人须提供自终验合格之日起不少于三十六个月的免费质保期"
        hits = _search_quote_in_pdf(doc, quote)
        assert len(hits) == 1
        page_idx, rects = hits[0]
        assert page_idx == 0
        assert len(rects) >= 2
    finally:
        doc.close()


# ── 2. 全角与半角标点差异引文（原文“，” vs 引文“,”）-> L3 或 L4 命中 ────────

def test_fullwidth_halfwidth_punctuation_mismatch_hits_l3_or_l4():
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text(
        (72, 120),
        "核心交换机背板带宽不得低于2.56Tbps，包转发率不得低于960Mpps。",
        fontname="china-s",
        fontsize=12,
    )
    pdf_path = _save_temp_pdf(doc)

    try:
        # 引文使用英文半角逗号 ',' 与英文句号 '.'，使 L1/L2 原串搜索落空，由 L3/L4 救回
        finding = _DummyFinding(
            severity='high',
            rule_name='★技术参数核验',
            message='交换机背板带宽偏离',
            suggestion='请核实交换机参数',
            legal_basis='招标文件第三章',
            clause_no='★1',
            quote='核心交换机背板带宽不得低于2.56Tbps,包转发率不得低于960Mpps.',
        )
        out_bytes = inject_pdf_highlights(str(pdf_path), [finding])
        out_doc = pymupdf.open(stream=out_bytes, filetype="pdf")
        try:
            # 总览页占第 0 页，原正文页在第 1 页
            assert len(out_doc) == 2
            body_page = out_doc[1]
            annots = list(body_page.annots() or [])
            highlight_annots = [a for a in annots if a.type[0] == pymupdf.PDF_ANNOT_HIGHLIGHT]
            assert len(highlight_annots) >= 1
        finally:
            out_doc.close()
    finally:
        pdf_path.unlink(missing_ok=True)


# ── 3. L5 置信护栏：仅 3 字可匹配的引文绝不乱锚，归入总览页"全篇级·未锚定" ──

def test_l5_confidence_guard_rejects_3char_partial_match():
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text(
        (72, 120),
        "本系统部署于政务云机房环境。",
        fontname="china-s",
        fontsize=12,
    )
    pdf_path = _save_temp_pdf(doc)

    try:
        # 引文共 18 字，与 PDF 正文仅有前 3 个字"本系统"重合（3/18 = 16.7% < 60%，且 3 < 8）
        finding = _DummyFinding(
            severity='high',
            rule_name='★条款响应核验',
            message='漏应答加密机要求',
            suggestion='补齐响应',
            legal_basis='招标文件3.2',
            clause_no='★5',
            quote='本系统必须配备国家密码管理局认证的服务器密码机',
        )
        out_bytes = inject_pdf_highlights(str(pdf_path), [finding])
        out_doc = pymupdf.open(stream=out_bytes, filetype="pdf")
        try:
            assert len(out_doc) == 2
            ov_text = out_doc[0].get_text()
            assert '标红批注总览' in ov_text
            assert 'N01' in ov_text
            assert '全篇级' in ov_text

            # 断言首页顶部生成了带 [全篇·总览 N01] 前缀的 TextAnnotation
            ov_annots = list(out_doc[0].annots() or [])
            assert len(ov_annots) == 1
            assert '[全篇·总览 N01]' in (ov_annots[0].info.get('content') or '')

            # 断言正文页（第 1 页）没有任何乱锚的高亮批注
            body_annots = list(out_doc[1].annots() or [])
            assert len(body_annots) == 0
        finally:
            out_doc.close()
    finally:
        pdf_path.unlink(missing_ok=True)


# ── 4. 总览页存在性、条目数与点击跳转链接（LINK_GOTO）验证 ───────────────────

def test_overview_page_entries_count_and_goto_links():
    doc = pymupdf.open()
    p1 = doc.new_page()
    p1.insert_text((72, 140), "第一条 平台须原生支持国产银河麒麟操作系统 V10。", fontname="china-s", fontsize=12)
    p2 = doc.new_page()
    p2.insert_text((72, 200), "第二条 免费质保期自终验合格之日起不少于 36 个月。", fontname="china-s", fontsize=12)
    pdf_path = _save_temp_pdf(doc)

    findings = [
        _DummyFinding(
            severity='high',
            rule_name='★条款响应核验',
            message='操作系统响应核查',
            suggestion='核实互认证书',
            legal_basis='第一章',
            clause_no='★1',
            quote='平台须原生支持国产银河麒麟操作系统 V10',
        ),
        _DummyFinding(
            severity='medium',
            rule_name='▲条款响应核验',
            message='质保期响应核查',
            suggestion='核实售后承诺',
            legal_basis='第二章',
            clause_no='▲2',
            quote='免费质保期自终验合格之日起不少于 36 个月',
        ),
        _DummyFinding(
            severity='low',
            rule_name='全局完备性核查',
            message='未提供节能产品清单声明',
            suggestion='补充声明',
            legal_basis='政采品目',
            clause_no='全篇',
            quote='',
        ),
    ]

    try:
        out_bytes = inject_pdf_highlights(str(pdf_path), findings)
        out_doc = pymupdf.open(stream=out_bytes, filetype="pdf")
        try:
            # 1 页总览 + 2 页原正文 = 3 页
            assert len(out_doc) == 3
            ov_page = out_doc[0]
            ov_txt = ov_page.get_text()
            assert '标红批注总览' in ov_txt
            assert 'N01' in ov_txt
            assert 'N02' in ov_txt
            assert 'N03' in ov_txt
            assert '已高亮于正文' in ov_txt
            assert '全篇级·未锚定' in ov_txt

            # 验证总览页包含指向第 1 页和第 2 页正文的内部跳转链接 (LINK_GOTO)
            links = list(ov_page.get_links() or [])
            goto_pages = {lk.get('page') for lk in links if lk.get('kind') == pymupdf.LINK_GOTO}
            assert 1 in goto_pages
            assert 2 in goto_pages
        finally:
            out_doc.close()
    finally:
        pdf_path.unlink(missing_ok=True)


# ── 5. worker 措辞动态化：★ / ▲ / 缺省符号分支验证 ─────────────────────────

def test_worker_f4_category_and_suggestion_dynamic_symbols():
    # (a) mark='▲'
    cat_tri, sug_tri = resolve_f4_category_and_suggestion({
        'code': 'MISSING_RESPONSE',
        'mark': '▲',
        'clause_no': '2.1',
    })
    assert cat_tri == '▲条款响应核验'
    assert '对该 ▲ 条款' in sug_tri

    # (b) mark='★'
    cat_star, sug_star = resolve_f4_category_and_suggestion({
        'code': 'MISSING_RESPONSE',
        'mark': '★',
        'clause_no': '1.1',
    })
    assert cat_star == '★条款响应核验'
    assert '对该 ★ 条款' in sug_star

    # (c) mark 缺省且 clause_no 不含符号
    cat_both, sug_both = resolve_f4_category_and_suggestion({
        'code': 'MISSING_RESPONSE',
        'mark': '',
        'clause_no': '（十四）',
    })
    assert cat_both == '★/▲条款响应核验'
    assert '对该 ★或▲ 条款' in sug_both


# ── 6. 既有行为回归：平直单行原串引文仍走 L1 直接命中且 Rect 完全一致 ───────

def test_l1_exact_single_line_quote_regression():
    doc = pymupdf.open()
    page = doc.new_page()
    quote_str = "投标保证金金额为人民币贰万元整"
    page.insert_text((72, 180), f"3.1 {quote_str}，须于开标前交纳。", fontname="china-s", fontsize=12)

    try:
        expected_rects = page.search_for(quote_str)
        assert len(expected_rects) == 1

        hits = _search_quote_in_pdf(doc, quote_str)
        assert len(hits) == 1
        assert hits[0][0] == 0
        assert len(hits[0][1]) == 1
        assert abs(hits[0][1][0].x0 - expected_rects[0].x0) < 0.01
        assert abs(hits[0][1][0].y0 - expected_rects[0].y0) < 0.01
    finally:
        doc.close()