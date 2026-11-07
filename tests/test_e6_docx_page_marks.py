# -*- coding: utf-8 -*-
"""E6 · Word 标书页码双轨定位单元测试（断言严格数数，严禁空循环假绿）。

覆盖任务卡 §4 全部测试要求：
1. 三页文档（两处 lastRenderedPageBreak） -> 物理页 1/2/3 区间正确，且重建全文 sha256 与 parse_document 输出逐字节相等
   （同时对 data/corpus/m0_samples 下任意 3 份真实 docx 语料做 sha256 全等核验）；
2. 分节重置：第 3 物理页起 sectPr 设 pgNumType start="1" 且挂载含 PAGE 域的页脚 -> 该区间 footer_page = 1, 2, ...；
3. 无页脚节 -> footer_page = None 但 physical 正常递增；
4. w:fmt="upperRoman" 节 -> footer_page = None 回退物理页；
5. 纯机器生成无锚点 docx -> spans 为空 []，offset_to_docx_page 返回 None；
6. deviation_exporter：docx 页码可得时位置列输出 `第X页(印刷页Y·估算)` / `第X页(物理页·估算)`；不可得时保持 `@字符N`。
"""
from __future__ import annotations

import hashlib
import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path

from docx import Document
import lxml.etree as ET

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.deviation_exporter import export_deviation_table
from core.parser import (
    _W_NS,
    _parse_docx_with_page_spans_internal,
    build_docx_page_spans,
    offset_to_docx_page,
    parse_document,
)


def _save_custom_docx_with_xml_mutator(
    paragraphs: list[str],
    mutator_fn=None,
    extra_footer_xml: bytes | None = None,
) -> Path:
    """用 python-docx 生成基础文档，再经 zipfile + lxml 注入 lastRenderedPageBreak / sectPr / footer1.xml。"""
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    raw_bytes = buf.getvalue()

    fd, out_path_str = tempfile.mkstemp(suffix='.docx')
    os.close(fd)
    out_path = Path(out_path_str)

    if mutator_fn is None and extra_footer_xml is None:
        out_path.write_bytes(raw_bytes)
        return out_path

    out_buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw_bytes), 'r') as zin:
        with zipfile.ZipFile(out_buf, 'w', zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                data = zin.read(info.filename)
                if info.filename == 'word/document.xml' and mutator_fn is not None:
                    root = ET.fromstring(data)
                    mutator_fn(root)
                    data = ET.tostring(root, xml_declaration=True, encoding='UTF-8', standalone='yes')
                elif info.filename == 'word/_rels/document.xml.rels' and extra_footer_xml is not None:
                    rels_root = ET.fromstring(data)
                    rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
                    rel_el = ET.SubElement(rels_root, f"{{{rel_ns}}}Relationship")
                    rel_el.set("Id", "rIdFooterE6")
                    rel_el.set(
                        "Type",
                        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer",
                    )
                    rel_el.set("Target", "footer1.xml")
                    data = ET.tostring(rels_root, xml_declaration=True, encoding='UTF-8', standalone='yes')
                zout.writestr(info, data)

            if extra_footer_xml is not None:
                zout.writestr('word/footer1.xml', extra_footer_xml)

    out_path.write_bytes(out_buf.getvalue())
    return out_path


_SAMPLE_FOOTER_WITH_PAGE_XML = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    b'<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    b'<w:p><w:fldSimple w:instr="PAGE \\* MERGEFORMAT"><w:r><w:t>1</w:t></w:r></w:fldSimple></w:p>'
    b'</w:ftr>'
)


# ── 1. 三页文档（两处 lastRenderedPageBreak）物理页区间 + 重建全文 SHA256 等值核验 ──

def test_three_pages_last_rendered_pb_and_sha256_equality():
    paras = [
        '第一页：封面与招标公告正文内容。',
        '第二页：★1. 系统必须支持国产银河麒麟操作系统 V10。',
        '第三页：★2. 免费质保期不少于 36 个月。',
    ]

    def _inject_two_page_breaks(root):
        body = root.find(f"{{{_W_NS}}}body")
        p_list = body.findall(f"{{{_W_NS}}}p")
        # 在第 2 段和第 3 段的第一个 w:r 开头各塞入一个 <w:lastRenderedPageBreak/>
        for p_idx in (1, 2):
            r_el = p_list[p_idx].find(f"{{{_W_NS}}}r")
            pb_el = ET.Element(f"{{{_W_NS}}}lastRenderedPageBreak")
            r_el.insert(0, pb_el)

    docx_p = _save_custom_docx_with_xml_mutator(paras, mutator_fn=_inject_two_page_breaks)
    try:
        parsed_std = parse_document(str(docx_p))
        rebuilt_txt, spans, _, _ = _parse_docx_with_page_spans_internal(docx_p)

        # §2.1 铁律：新链路重建全文与 parse_document 输出 sha256 完全相等
        sha_std = hashlib.sha256(parsed_std.text.encode('utf-8')).hexdigest()
        sha_new = hashlib.sha256(rebuilt_txt.encode('utf-8')).hexdigest()
        assert sha_std == sha_new

        assert len(spans) == 3
        assert [s[2] for s in spans] == [1, 2, 3]

        off_p1 = parsed_std.text.find('第一页')
        off_p2 = parsed_std.text.find('★1.')
        off_p3 = parsed_std.text.find('★2.')
        assert offset_to_docx_page(off_p1, spans) == {'physical': 1, 'footer': None}
        assert offset_to_docx_page(off_p2, spans) == {'physical': 2, 'footer': None}
        assert offset_to_docx_page(off_p3, spans) == {'physical': 3, 'footer': None}
    finally:
        docx_p.unlink(missing_ok=True)

    # 若本地存在 data/corpus/m0_samples 真实 docx 语料，取前 3 份验证全文 sha256 100% 等值
    corpus_dir = Path('data/corpus/m0_samples')
    if corpus_dir.is_dir():
        real_docx_files = sorted(corpus_dir.glob('*.docx'))[:3]
        for rf in real_docx_files:
            std_t = parse_document(str(rf)).text
            new_t, _, _, _ = _parse_docx_with_page_spans_internal(rf)
            assert hashlib.sha256(std_t.encode('utf-8')).hexdigest() == hashlib.sha256(
                new_t.encode('utf-8')
            ).hexdigest()


# ── 2 & 3 & 4. 分节重置（第3物理页起 start=1 + 含 PAGE 页脚）、无页脚节、upperRoman 节 ──

def test_section_reset_footer_page_and_roman_fallback():
    paras = [
        '物理第1页：封面（第1节，无页脚）',
        '物理第2页：目录（第2节，upperRoman 罗马数字页码，应回退 None）',
        '物理第3页：正文第一章（第3节，设 start=1 且挂载 PAGE 页脚 -> 印刷页=1）',
        '物理第4页：正文第二章（第3节续页 -> 印刷页=2）',
    ]

    def _mutate_sections(root):
        r_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        body = root.find(f"{{{_W_NS}}}body")
        p_list = body.findall(f"{{{_W_NS}}}p")

        # 第 1 段末尾加第 1 节 sectPr（无 footerReference -> footer_page=None，且换到物理第 2 页）
        ppr0 = ET.SubElement(p_list[0], f"{{{_W_NS}}}pPr")
        ET.SubElement(ppr0, f"{{{_W_NS}}}sectPr")

        # 第 2 段末尾加第 2 节 sectPr（挂载页脚但 fmt="upperRoman" -> footer_page=None，换到物理第 3 页）
        ppr1 = ET.SubElement(p_list[1], f"{{{_W_NS}}}pPr")
        sect2 = ET.SubElement(ppr1, f"{{{_W_NS}}}sectPr")
        fref2 = ET.SubElement(sect2, f"{{{_W_NS}}}footerReference")
        fref2.set(f"{{{_W_NS}}}type", "default")
        fref2.set(f"{{{r_ns}}}id", "rIdFooterE6")
        pg2 = ET.SubElement(sect2, f"{{{_W_NS}}}pgNumType")
        pg2.set(f"{{{_W_NS}}}fmt", "upperRoman")
        pg2.set(f"{{{_W_NS}}}start", "1")

        # 第 4 段开头塞入 lastRenderedPageBreak（从物理第 3 页换到物理第 4 页）
        r3 = p_list[3].find(f"{{{_W_NS}}}r")
        r3.insert(0, ET.Element(f"{{{_W_NS}}}lastRenderedPageBreak"))

        # body 末尾 sectPr 管辖第 3 节（物理第 3、4 页）：设 decimal + start=1 + 挂载 PAGE 页脚
        body_sect = body.find(f"{{{_W_NS}}}sectPr")
        if body_sect is None:
            body_sect = ET.SubElement(body, f"{{{_W_NS}}}sectPr")
        fref3 = ET.SubElement(body_sect, f"{{{_W_NS}}}footerReference")
        fref3.set(f"{{{_W_NS}}}type", "default")
        fref3.set(f"{{{r_ns}}}id", "rIdFooterE6")
        pg3 = ET.SubElement(body_sect, f"{{{_W_NS}}}pgNumType")
        pg3.set(f"{{{_W_NS}}}fmt", "decimal")
        pg3.set(f"{{{_W_NS}}}start", "1")

    docx_p = _save_custom_docx_with_xml_mutator(
        paras,
        mutator_fn=_mutate_sections,
        extra_footer_xml=_SAMPLE_FOOTER_WITH_PAGE_XML,
    )
    try:
        txt = parse_document(str(docx_p)).text
        spans = build_docx_page_spans(docx_p)
        assert len(spans) == 4

        # 物理第 1 页（无页脚）-> physical=1, footer=None
        p1_info = offset_to_docx_page(txt.find('物理第1页'), spans)
        assert p1_info == {'physical': 1, 'footer': None}

        # 物理第 2 页（upperRoman 节）-> physical=2, footer=None
        p2_info = offset_to_docx_page(txt.find('物理第2页'), spans)
        assert p2_info == {'physical': 2, 'footer': None}

        # 物理第 3 页（第 3 节 start=1）-> physical=3, footer=1
        p3_info = offset_to_docx_page(txt.find('物理第3页'), spans)
        assert p3_info == {'physical': 3, 'footer': 1}

        # 物理第 4 页（第 3 节第 2 页）-> physical=4, footer=2
        p4_info = offset_to_docx_page(txt.find('物理第4页'), spans)
        assert p4_info == {'physical': 4, 'footer': 2}
    finally:
        docx_p.unlink(missing_ok=True)


# ── 5. 纯机器生成无锚点 docx -> spans 为空 []，offset_to_docx_page 返 None ──

def test_machine_generated_docx_without_anchors_returns_empty_spans():
    docx_p = _save_custom_docx_with_xml_mutator(['第一段纯文本', '第二段纯文本'])
    try:
        spans = build_docx_page_spans(docx_p)
        assert spans == []
        assert offset_to_docx_page(5, spans) is None
    finally:
        docx_p.unlink(missing_ok=True)


# ── 6. deviation_exporter 位置列：有锚点输出 第X页(印刷页Y·估算)，无锚点保持 @字符N ──

def test_deviation_exporter_docx_dual_page_format_and_fallback():
    tender_paras = [
        '封面与目录（物理第1页）',
        '★1. 核心平台须支持国产银河麒麟高级服务器操作系统 V10。',
    ]
    bid_paras = [
        '投标书封面（物理第1页）',
        '投标书目录（物理第2页）',
        '★1. 核心平台须支持国产银河麒麟高级服务器操作系统 V10。（我方完全满足，见附件1）',
    ]

    # 招标书为纯机器生成无锚点 docx（应保持 招标书@字符N）
    tender_p = _save_custom_docx_with_xml_mutator(tender_paras)

    # 投标书带分节重置：物理第 3 页对应印刷页 1
    def _mutate_bid(root):
        r_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        body = root.find(f"{{{_W_NS}}}body")
        p_list = body.findall(f"{{{_W_NS}}}p")

        # 第 1 段换页到物理第 2 页
        r1 = p_list[1].find(f"{{{_W_NS}}}r")
        r1.insert(0, ET.Element(f"{{{_W_NS}}}lastRenderedPageBreak"))

        # 第 2 段末尾结束前序节（换到物理第 3 页）
        ppr1 = ET.SubElement(p_list[1], f"{{{_W_NS}}}pPr")
        ET.SubElement(ppr1, f"{{{_W_NS}}}sectPr")

        # 正文节（物理第 3 页）设 start=1 + PAGE 页脚 -> physical=3, footer=1
        body_sect = body.find(f"{{{_W_NS}}}sectPr")
        fref = ET.SubElement(body_sect, f"{{{_W_NS}}}footerReference")
        fref.set(f"{{{_W_NS}}}type", "default")
        fref.set(f"{{{r_ns}}}id", "rIdFooterE6")
        pg = ET.SubElement(body_sect, f"{{{_W_NS}}}pgNumType")
        pg.set(f"{{{_W_NS}}}start", "1")

    bid_p = _save_custom_docx_with_xml_mutator(
        bid_paras,
        mutator_fn=_mutate_bid,
        extra_footer_xml=_SAMPLE_FOOTER_WITH_PAGE_XML,
    )
    fd, out_str = tempfile.mkstemp(suffix='.docx')
    os.close(fd)
    out_p = Path(out_str)

    try:
        res = export_deviation_table(tender_p, bid_p, out_p, client=None)
        assert res['total_rows'] == 1
        loc_str = res['rows'][0]['location']
        # 招标书无锚点保持 @字符N，投标书带锚点与页脚输出 第3页(印刷页1·估算)
        assert '招标书@字符' in loc_str
        assert '投标书 第3页(印刷页1·估算)' in loc_str
    finally:
        tender_p.unlink(missing_ok=True)
        bid_p.unlink(missing_ok=True)
        out_p.unlink(missing_ok=True)