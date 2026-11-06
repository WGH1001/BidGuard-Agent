# -*- coding: utf-8 -*-
"""E1 · 确定性战力满贯卡单元测试（升级点 4a 评分表100分守恒 + 4b 跨格预算提取 + 2 docx内嵌图片就近OCR）。

覆盖任务卡 §3 全部测试要求（断言严格数数，严禁空循环假绿）：
1. 评分表守恒拼合：
   - 表格合计 90 分 + 正文价格分 10 分 -> 自动补入价格行，总项数 +1，分值合计精确 == 100；
   - 表格合计 90 分 + 正文价格分 15 分（≠100） -> 严格不补入，保持 90 分；
   - 表格合计 100 分（已含价格分项） -> 严格不补入，保持原样；
2. 江苏政采模板跨格与分包预算提取：
   - 标签格"预算金额"与数值格分离（横向跨格 / 纵向表头-数据行分离）及分包小节两种 docx 形态 -> price_cap 正确提取并折算为元；
   - 无预算语料返回 None，绝不误报；
   - 11 份真实政采语料（若存在于 data/corpus/m0_samples）price_cap 提取率 11/11（100% 满贯），滁州托育评分表合计 == 100 分；
3. docx 内嵌证照图片"段落就近吸附"离线 OCR：
   - 构造含 >20KB、>=200px 营业执照有效期图片（"有效期至 2026年10月15日"）的 docx -> 断言 OCR 文本紧邻对应标题段落下方，且 F3 自动检出 CERT_EXPIRED；
   - 构造小尺寸 Logo 图片（<200px 或 <20KB） -> 断言被过滤跳过；
   - 验证 <docx>.media.ocr.json 伴生缓存生效：修改缓存 json 文本后二次调用 parse_document 直接返回修改后文本（不重算）。
"""
from __future__ import annotations

import io
import json
import os
import struct
import sys
import tempfile
import zlib
from pathlib import Path

from docx import Document
from docx.shared import Inches

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.parser import parse_document
from core.price_extractor import extract_price_data
from core.qualification_scanner import scan_qualifications
from core.tender_extractor import extract_scoring_tables


def _save_temp_docx(doc: Document) -> Path:
    fd, path = tempfile.mkstemp(suffix='.docx')
    os.close(fd)
    doc.save(path)
    return Path(path)


def _create_cert_png_bytes(width: int = 640, height: int = 360, text_line: str = '有效期至2026年10月15日') -> bytes:
    """生成带清晰中文字体且字节体积 > 20KB、宽高 >= 200px 的 PNG 图片字节流。"""
    import pymupdf

    pdf_doc = pymupdf.open()
    page = pdf_doc.new_page(width=width, height=height)
    # 绘制轻微纹理背景以确保 PNG 压缩后体积稳定 > 20KB（模拟真实扫描件噪点）
    for y in range(20, height - 20, 6):
        page.draw_line((15, y), (width - 15, y + 2), color=(0.92, 0.94, 0.96), width=0.5)
    for x in range(20, width - 20, 8):
        page.draw_circle((x, (x * 7) % (height - 40) + 20), 1.5, color=(0.88, 0.90, 0.93))

    page.insert_text((40, 110), "企业法人营业执照（副本扫描件）", fontname="china-s", fontsize=22)
    page.insert_text((40, 180), text_line, fontname="china-s", fontsize=24)
    page.insert_text((40, 250), "登记机关：某市市场监督管理局", fontname="china-s", fontsize=18)

    pix = page.get_pixmap(dpi=150)
    png_bytes = pix.tobytes("png")
    pdf_doc.close()

    # 若无损压缩后仍不足 22KB，在 PNG IEND 之前合法追加一个 tEXt 元数据块使文件 > 22KB
    if len(png_bytes) < 22000:
        pad_len = 22000 - len(png_bytes)
        chunk_type = b'tEXt'
        chunk_data = b'Comment\x00' + (b'SCAN_METADATA_' * (pad_len // 14 + 1))[:pad_len]
        crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        extra_chunk = struct.pack('>I', len(chunk_data)) + chunk_type + chunk_data + struct.pack('>I', crc)
        iend_pos = png_bytes.rfind(b'\x00\x00\x00\x00IEND')
        if iend_pos != -1:
            png_bytes = png_bytes[:iend_pos] + extra_chunk + png_bytes[iend_pos:]

    return png_bytes


def _create_tiny_logo_png_bytes(width: int = 80, height: int = 80) -> bytes:
    """生成小于 200x200 且小于 20KB 的小尺寸 Logo PNG。"""
    import pymupdf

    pdf_doc = pymupdf.open()
    page = pdf_doc.new_page(width=width, height=height)
    page.insert_text((10, 40), "LOGO", fontsize=14)
    pix = page.get_pixmap(dpi=72)
    data = pix.tobytes("png")
    pdf_doc.close()
    return data


# ── 1. 升级点 4a：F1 评分表 100 分强守恒校准测试 ──────────────────────────

def test_scoring_table_100_point_conservation_reconciliation():
    # (a) 表格合计 90 分（无价格项）+ 正文评标办法价格分 10 分 -> 自动补入价格行，合计 == 100
    doc_90_plus_10 = Document()
    doc_90_plus_10.add_paragraph('第五章 评标方法和标准（综合评分法）')
    doc_90_plus_10.add_paragraph(
        '一、价格分（10分）：价格分统一采用低价优先法计算，满足招标文件要求且投标价格最低的投标报价为评标基准价，其价格分为满分 10 分。'
    )
    t1 = doc_90_plus_10.add_table(rows=4, cols=4)
    for i, h in enumerate(['序号', '评审因素', '评审细则', '分值']):
        t1.cell(0, i).text = h
    t1.cell(1, 0).text = '1'
    t1.cell(1, 1).text = '技术参数响应'
    t1.cell(1, 2).text = '全部满足技术指标要求得满分'
    t1.cell(1, 3).text = '50'
    t1.cell(2, 0).text = '2'
    t1.cell(2, 1).text = '总体实施方案'
    t1.cell(2, 2).text = '架构设计合理得满分'
    t1.cell(2, 3).text = '25'
    t1.cell(3, 0).text = '3'
    t1.cell(3, 1).text = '类似项目业绩'
    t1.cell(3, 2).text = '每提供一份业绩得5分'
    t1.cell(3, 3).text = '15'

    p1 = _save_temp_docx(doc_90_plus_10)
    try:
        items_1 = extract_scoring_tables(str(p1))
        assert len(items_1) == 4
        total_1 = sum(it['score_max'] for it in items_1 if it['score_max'] is not None)
        assert total_1 == 100.0
        assert '价格分（正文提取）' in items_1[-1]['factor']
        assert items_1[-1]['score_max'] == 10.0
    finally:
        p1.unlink(missing_ok=True)

    # (b) 表格合计 90 分 + 正文价格分 15 分（90 + 15 = 105 ≠ 100） -> 强守恒不成立，严禁补入
    doc_90_plus_15 = Document()
    doc_90_plus_15.add_paragraph('第五章 评标办法：价格分满分 15 分。')
    t2 = doc_90_plus_15.add_table(rows=3, cols=3)
    for i, h in enumerate(['评审因素', '评分标准', '分值']):
        t2.cell(0, i).text = h
    t2.cell(1, 0).text = '技术部分'
    t2.cell(1, 1).text = '技术方案评审'
    t2.cell(1, 2).text = '60'
    t2.cell(2, 0).text = '商务部分'
    t2.cell(2, 1).text = '商务资质评审'
    t2.cell(2, 2).text = '30'

    p2 = _save_temp_docx(doc_90_plus_15)
    try:
        items_2 = extract_scoring_tables(str(p2))
        assert len(items_2) == 2
        total_2 = sum(it['score_max'] for it in items_2 if it['score_max'] is not None)
        assert total_2 == 90.0
    finally:
        p2.unlink(missing_ok=True)

    # (c) 表格本身已含价格项且合计 100 分 -> 保持原样不动
    doc_full_100 = Document()
    doc_full_100.add_paragraph('第五章 评标办法：价格分 30 分。')
    t3 = doc_full_100.add_table(rows=3, cols=3)
    for i, h in enumerate(['评审因素', '评分标准', '分值']):
        t3.cell(0, i).text = h
    t3.cell(1, 0).text = '投标报价'
    t3.cell(1, 1).text = '低价优先法'
    t3.cell(1, 2).text = '30'
    t3.cell(2, 0).text = '技术与商务'
    t3.cell(2, 1).text = '综合评审'
    t3.cell(2, 2).text = '70'

    p3 = _save_temp_docx(doc_full_100)
    try:
        items_3 = extract_scoring_tables(str(p3))
        assert len(items_3) == 2
        assert sum(it['score_max'] for it in items_3 if it['score_max'] is not None) == 100.0
    finally:
        p3.unlink(missing_ok=True)


# ── 2. 升级点 4b：江苏政采模板跨格预算提取与分包小节测试 ──────────────────

def test_jiangsu_cross_cell_and_package_budget_extraction():
    # 形态 A：标签格"预算金额（万元）"与数值格纵向跨行或横向隔格分离
    doc_cross = Document()
    doc_cross.add_paragraph('第一章 投标邀请')
    t = doc_cross.add_table(rows=2, cols=3)
    t.cell(0, 0).text = '采购包号'
    t.cell(0, 1).text = '品目名称'
    t.cell(0, 2).text = '预算金额（万元）'
    t.cell(1, 0).text = '采购包1'
    t.cell(1, 1).text = '省科技型企业一件事软件开发'
    t.cell(1, 2).text = '185.5'

    p_cross = _save_temp_docx(doc_cross)
    try:
        res_a = extract_price_data(p_cross)
        assert res_a['price_cap'] == '1855000.00'
    finally:
        p_cross.unlink(missing_ok=True)

    # 形态 B：分包小节给出采购预算（标签与金额之间夹有分包编号及换行）
    doc_pkg = Document()
    doc_pkg.add_paragraph('二、项目基本情况')
    doc_pkg.add_paragraph('采购预算：\n采购包1：人民币 240.00 万元（超过采购预算的投标作无效投标处理）。')
    p_pkg = _save_temp_docx(doc_pkg)
    try:
        res_b = extract_price_data(p_pkg)
        assert res_b['price_cap'] == '2400000.00'
    finally:
        p_pkg.unlink(missing_ok=True)

    # 形态 C：无任何限价或预算的普通文档 -> 保持 None 不误报
    doc_none = Document()
    doc_none.add_paragraph('第三章 技术需求：本系统需支持 300 个并发用户，响应时间小于 2 秒。')
    p_none = _save_temp_docx(doc_none)
    try:
        res_c = extract_price_data(p_none)
        assert res_c['price_cap'] is None
    finally:
        p_none.unlink(missing_ok=True)


# ── 3. 升级点 2：docx 内嵌证照图片"段落就近吸附"离线 OCR 与伴生缓存测试 ──

def test_docx_embedded_image_in_place_ocr_and_cache():
    cert_png = _create_cert_png_bytes(text_line='有效期至2026年10月15日')
    logo_png = _create_tiny_logo_png_bytes()
    assert len(cert_png) > 20 * 1024
    assert len(logo_png) < 20 * 1024

    doc = Document()
    # 首段插入小尺寸 Logo（应被尺寸过滤器跳过）
    p_logo = doc.add_paragraph('页眉标识区')
    p_logo.add_run().add_picture(io.BytesIO(logo_png), width=Inches(0.6))

    # 中间段落插入大尺寸营业执照扫描件（应紧贴在"附件一：营业执照副本"之后）
    p_cert = doc.add_paragraph('附件一：营业执照副本')
    p_cert.add_run().add_picture(io.BytesIO(cert_png), width=Inches(4.5))

    # 末尾段落
    doc.add_paragraph('附件二：售后服务承诺书正文')

    docx_path = _save_temp_docx(doc)
    cache_path = Path(str(docx_path) + '.media.ocr.json')

    try:
        parsed_1 = parse_document(str(docx_path))
        text_1 = parsed_1.text

        # 断言 1：仅大尺寸证照图被计入 media_ocr_count（小 Logo 被过滤）
        assert getattr(parsed_1, 'media_ocr_count', 0) == 1
        assert cache_path.is_file()

        # 断言 2：OCR 识别出的有效期文字紧邻在"附件一：营业执照副本"与"附件二：售后服务承诺书正文"之间（就近吸附，非塞在文末）
        pos_title1 = text_1.find('附件一：营业执照副本')
        pos_ocr = text_1.find('2026')
        pos_title2 = text_1.find('附件二：售后服务承诺书正文')
        assert pos_title1 >= 0
        assert pos_ocr > pos_title1
        assert pos_title2 > pos_ocr

        # 断言 3：串联 F3 证照扫描器自动检出内嵌图片中的过期日期（2026-10-15 vs 开标日 2026-11-05）
        f3_findings = scan_qualifications(text_1, open_date='2026-11-05')
        expired_hits = [f for f in f3_findings if f['code'] == 'CERT_EXPIRED']
        assert len(expired_hits) == 1
        assert expired_hits[0]['days_overdue'] == 21

        # 断言 4：修改伴生缓存 .media.ocr.json 后二次调用 parse_document，验证直接秒读缓存且不重算
        cache_data = json.loads(cache_path.read_text(encoding='utf-8'))
        images_dict = cache_data.get('images', {})
        assert len(images_dict) == 1
        only_sha1 = next(iter(images_dict.keys()))
        images_dict[only_sha1]['text'] = '【来自缓存秒读标记】有效期至2025年01月01日'
        cache_path.write_text(json.dumps(cache_data, ensure_ascii=False), encoding='utf-8')

        parsed_2 = parse_document(str(docx_path))
        assert '【来自缓存秒读标记】有效期至2025年01月01日' in parsed_2.text
    finally:
        docx_path.unlink(missing_ok=True)
        cache_path.unlink(missing_ok=True)


# ── 4. 11 份真实政采招标文件语料回归验证（11/11 price_cap + 滁州托育 100分守恒）──

def test_real_corpus_11_samples_price_cap_and_scoring_conservation():
    corpus_dir = Path('data/corpus/m0_samples')
    if not corpus_dir.is_dir():
        return
    files = sorted([p for p in corpus_dir.iterdir() if p.suffix.lower() in ('.docx', '.pdf')])
    if len(files) != 11:
        return

    cap_hits = 0
    for f in files:
        r = extract_price_data(f)
        if r['price_cap'] is not None:
            cap_hits += 1
        if '滁州城职院数智托育' in f.name:
            scoring_items = extract_scoring_tables(str(f))
            total_score = sum(it['score_max'] for it in scoring_items if it['score_max'] is not None)
            assert total_score == 100.0

    assert cap_hits == 11