# -*- coding: utf-8 -*-
"""F5 《技术与商务偏离表.docx》一键对齐生成器单元测试（0 Token 离线可跑）。

覆盖任务卡全部验收点：
1. mock 双书 docx（3 条正响应 + 1 条漏答）-> 生成的 docx 重新用 python-docx 打开，
   断言表格行列数（5行×6列）、六列表头、四行偏离判定与各格内容逐格正确；
2. 负偏离（响应冲突 CONTRADICTION）与 structured.consequence（veto/deduction/bonus）括注格式核验；
3. usage_note 与 table_marker 严格排除，star_results 与过滤后要求严格同序 zip 配对；
4. 位置索引列纪律：docx 不编造页码（输出"招标书@字符X | 投标书@字符Y"与"投标书未检出响应"），
   PDF 输出真实页码（"招标书第 X 页 | 投标书第 Y 页"）；
5. 偏离情况列着色断言（负偏离红色加粗 RGBColor(0xDC,0x26,0x26)、无偏离绿色 RGBColor(0x16,0xA3,0x4A)）。
"""
import os
import sys
import tempfile
from pathlib import Path
from docx import Document
from docx.shared import RGBColor

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.deviation_exporter import DEVIATION_TABLE_HEADERS, export_deviation_table


def _save_temp_docx(paragraphs: list[str]) -> Path:
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    fd, path = tempfile.mkstemp(suffix='.docx')
    os.close(fd)
    doc.save(path)
    return Path(path)


# ── 1. 核心验收场景：mock 双书 docx（3 条响应 + 1 条漏答）重新打开逐格断言 ──

def test_export_deviation_table_three_ok_one_missing():
    tender_paras = [
        '第一章 采购需求',
        '本文件中的“★”内容为实质性要求。',  # usage_note，应被过滤不进偏离表
        '核心平台参数要求如下：',
        '★1. 平台服务端须原生适配银河麒麟高级服务器操作系统 V10。',
        '上述为运行环境基线要求。',
        '★2. 数据库须兼容达梦数据库管理系统 DM8，并提供互认证书。',
        '数据库须满足国产化适配要求。',
        '★3. 日志采集单节点并发处理能力不得低于 5000 EPS。',
        '采集性能按单节点实测计。',
        '★4. 投标人须配备至少 2 名持有 CISP 证书的驻场安全工程师。',
    ]
    bid_paras = [
        '技术要求响应表',
        '★1. 平台服务端须原生适配银河麒麟高级服务器操作系统 V10。（我方完全满足）',
        '★2. 数据库须兼容达梦数据库管理系统 DM8，并提供互认证书。（完全满足，见附件3）',
        '★3. 日志采集单节点并发处理能力不得低于 5000 EPS。（实测达 8500 EPS，正响应）',
    ]

    tender_p = _save_temp_docx(tender_paras)
    bid_p = _save_temp_docx(bid_paras)
    fd, out_str = tempfile.mkstemp(suffix='.docx')
    os.close(fd)
    out_p = Path(out_str)

    try:
        res = export_deviation_table(tender_p, bid_p, out_p, client=None)
        assert res['total_rows'] == 4
        assert res['no_deviation_count'] == 3
        assert res['negative_deviation_count'] == 1
        assert res['missing_count'] == 1
        assert out_p.exists()

        # 重新用 python-docx 打开生成的 docx 逐格核验
        gen_doc = Document(str(out_p))
        full_heading_txt = '\n'.join(p.text for p in gen_doc.paragraphs)
        assert '技术与商务偏离表' in full_heading_txt
        assert '共核验实质性要求 4 项' in full_heading_txt

        assert len(gen_doc.tables) == 1
        tbl = gen_doc.tables[0]
        assert tbl.style.name == 'Table Grid'
        assert len(tbl.rows) == 5  # 1 表头 + 4 数据行
        assert len(tbl.columns) == 6

        # 断言表头六列
        actual_headers = tuple(c.text.strip() for c in tbl.rows[0].cells)
        assert actual_headers == DEVIATION_TABLE_HEADERS

        # 断言前 3 行（无偏离）
        for r_idx in (1, 2, 3):
            cells = [c.text.strip() for c in tbl.rows[r_idx].cells]
            assert cells[0] == str(r_idx)
            assert '★' in cells[1]
            assert cells[4] == '无偏离'
            assert '招标书@字符' in cells[5]
            assert '投标书@字符' in cells[5]
            # 断言无偏离为绿色字体
            dev_run = tbl.rows[r_idx].cells[4].paragraphs[0].runs[0]
            assert dev_run.font.color.rgb == RGBColor(0x16, 0xA3, 0x4A)

        # 断言第 4 行（漏应答 -> 负偏离）
        row4_cells = [c.text.strip() for c in tbl.rows[4].cells]
        assert row4_cells[0] == '4'
        assert 'CISP' in row4_cells[2]
        assert row4_cells[3] == '（未检出响应内容）'
        assert row4_cells[4] == '负偏离（漏应答）'
        assert '招标书@字符' in row4_cells[5]
        assert '投标书未检出响应' in row4_cells[5]

        # 断言负偏离为红色加粗字体
        neg_run = tbl.rows[4].cells[4].paragraphs[0].runs[0]
        assert neg_run.bold is True
        assert neg_run.font.color.rgb == RGBColor(0xDC, 0x26, 0x26)
    finally:
        tender_p.unlink(missing_ok=True)
        bid_p.unlink(missing_ok=True)
        out_p.unlink(missing_ok=True)


# ── 2. 响应冲突（CONTRADICTION）与 consequence（veto/deduction/bonus）括注核验 ──

def test_consequence_annotations_and_contradiction_row():
    tender_paras = [
        '★1. 本项目免费质保期不少于 36 个月。',
        '▲2. 系统須支持移动端微信小程序消息推送。',
        '▲3. 提供原厂 7×24 小时驻场服务。',
    ]
    bid_paras = [
        '★1. 本项目免费质保期为 24 个月。',  # 与 36 个月冲突 -> CONTRADICTION
        '▲2. 系统支持移动端微信小程序消息推送。',  # RESPONSE_OK
        # ▲3 漏应答 -> MISSING_RESPONSE
    ]
    tender_p = _save_temp_docx(tender_paras)
    bid_p = _save_temp_docx(bid_paras)
    fd, out_str = tempfile.mkstemp(suffix='.docx')
    os.close(fd)
    out_p = Path(out_str)

    # 传入带 structured.consequence 的要求清单
    t_full = '\n'.join(tender_paras)
    s1 = t_full.find(tender_paras[0])
    s2 = t_full.find(tender_paras[1])
    s3 = t_full.find(tender_paras[2])
    custom_reqs = [
        {
            'mark': '★', 'clause_no': '1', 'text': tender_paras[0],
            'start': s1, 'end': s1 + len(tender_paras[0]), 'kind': 'requirement',
            'structured': {'is_requirement': True, 'consequence': 'veto'},
        },
        {
            'mark': '▲', 'clause_no': '2', 'text': tender_paras[1],
            'start': s2, 'end': s2 + len(tender_paras[1]), 'kind': 'requirement',
            'structured': {'is_requirement': True, 'consequence': 'bonus'},
        },
        {
            'mark': '▲', 'clause_no': '3', 'text': tender_paras[2],
            'start': s3, 'end': s3 + len(tender_paras[2]), 'kind': 'requirement',
            'structured': {'is_requirement': True, 'consequence': 'deduction'},
        },
    ]

    try:
        res = export_deviation_table(tender_p, bid_p, out_p, requirements=custom_reqs, client=None)
        assert res['total_rows'] == 3
        assert res['contradiction_count'] == 1
        assert res['missing_count'] == 1
        assert res['no_deviation_count'] == 1

        gen_doc = Document(str(out_p))
        tbl = gen_doc.tables[0]
        assert tbl.rows[1].cells[4].text.strip() == '负偏离（响应冲突），★一票否决项'
        assert tbl.rows[2].cells[4].text.strip() == '无偏离，▲重要加分项'
        assert tbl.rows[3].cells[4].text.strip() == '负偏离（漏应答），▲重要扣分项'
        assert tbl.rows[3].cells[3].text.strip() == '（未检出响应内容）'
    finally:
        tender_p.unlink(missing_ok=True)
        bid_p.unlink(missing_ok=True)
        out_p.unlink(missing_ok=True)


# ── 3. PDF 页码换算验证（PDF 输出“投标书第 X 页”）────────────────────────

def test_pdf_location_index_uses_real_page_numbers():
    import pymupdf

    fd_t, t_pdf_str = tempfile.mkstemp(suffix='.pdf')
    os.close(fd_t)
    fd_b, b_pdf_str = tempfile.mkstemp(suffix='.pdf')
    os.close(fd_b)
    fd_o, out_str = tempfile.mkstemp(suffix='.docx')
    os.close(fd_o)

    t_doc = pymupdf.open()
    p1 = t_doc.new_page()
    p1.insert_text((72, 72), "★1. 核心数据库必须支持主备热切换功能。", fontname="china-s", fontsize=12)
    t_doc.save(t_pdf_str)
    t_doc.close()

    b_doc = pymupdf.open()
    b_doc.new_page().insert_text((72, 72), "第一页：投标函与开标一览表", fontname="china-s", fontsize=12)
    p2 = b_doc.new_page()
    p2.insert_text((72, 72), "★1. 核心数据库必须支持主备热切换功能。（我方完全满足）", fontname="china-s", fontsize=12)
    b_doc.save(b_pdf_str)
    b_doc.close()

    try:
        res = export_deviation_table(t_pdf_str, b_pdf_str, out_str, client=None)
        assert res['total_rows'] == 1
        loc_txt = res['rows'][0]['location']
        assert '招标书第 1 页' in loc_txt
        assert '投标书第 2 页' in loc_txt
    finally:
        Path(t_pdf_str).unlink(missing_ok=True)
        Path(b_pdf_str).unlink(missing_ok=True)
        Path(out_str).unlink(missing_ok=True)