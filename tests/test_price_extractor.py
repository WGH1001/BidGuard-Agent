# -*- coding: utf-8 -*-
"""F2-2 报价表提取辅助层单元测试（core/price_extractor.py）。

覆盖验收点：
1. 招标文件最高限价/采购预算提取与「万元 -> 元」自动折算；
2. 招标文件空白《开标一览表/分项报价表》模板识别并诚实返回 NEED_MANUAL（不把序号当金额）；
3. 投标文件 docx（开标一览表 + 分项报价明细表 + 合计行隔离 + 合并单元格去重）提取为结构化数据；
4. 表头标注「单位：万元」的分项报价表自动折算为元；
5. 纯文本 / PDF 文本行报价提取与噪声过滤（不误抓评分公式或保证金大写）；
6. run_price_check() 端到端串联 PriceSandbox 检出算术错误、跨表差额、大写错误与超限价。
"""
import os
import sys
import tempfile
from pathlib import Path
from docx import Document
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.price_extractor import (
    extract_price_cap,
    extract_price_from_text,
    extract_price_data,
    run_price_check,
)
from core.price_sandbox import PriceSandbox


def _save_temp_docx(doc: Document) -> Path:
    fd, path = tempfile.mkstemp(suffix='.docx')
    os.close(fd)
    doc.save(path)
    return Path(path)


# ── 1. 最高限价与万元换算 ─────────────────────────────────────────────

def test_extract_price_cap_variants_and_wan_conversion():
    assert extract_price_cap('本项目最高投标限价为人民币 145.00 万元，超过限价作废标处理。') == '1450000.00'
    assert extract_price_cap('采购预算：2,680,000.00元') == '2680000.00'
    assert extract_price_cap('招标控制价（万元）：98.5') == '985000.00'
    # 同时存在采购预算与最高限价时，优先取最高限价
    txt = '本项目采购预算为 150 万元，最高投标限价为 142.8 万元。'
    assert extract_price_cap(txt) == '1428000.00'


# ── 2. 空白招标文件报价表模板 -> NEED_MANUAL ───────────────────────────

def test_blank_tender_template_returns_need_manual():
    doc = Document()
    doc.add_paragraph('第一章 招标公告：本项目采购预算为 120.00 万元。')
    doc.add_paragraph('第六章 投标文件格式 —— 一、开标一览表')
    t1 = doc.add_table(rows=2, cols=3)
    t1.cell(0, 0).text = '项目名称'
    t1.cell(0, 1).text = '投标总价（元）'
    t1.cell(0, 2).text = '投标总价（大写）'
    t1.cell(1, 0).text = '某市政务云平台项目'
    t1.cell(1, 1).text = ''
    t1.cell(1, 2).text = ''

    doc.add_paragraph('二、分项报价明细表')
    t2 = doc.add_table(rows=4, cols=5)
    headers = ['序号', '品目名称', '数量', '单价（元）', '合价（元）']
    for i, h in enumerate(headers):
        t2.cell(0, i).text = h
    # 填入序号与品目名，但金额为空白或斜杠，检验绝不会把序号 1/2 当金额
    t2.cell(1, 0).text = '1'
    t2.cell(1, 1).text = '核心交换机'
    t2.cell(1, 2).text = '2台'
    t2.cell(1, 3).text = '/'
    t2.cell(1, 4).text = ''
    t2.cell(2, 0).text = '2'
    t2.cell(2, 1).text = '防火墙设备'
    t2.cell(2, 2).text = '1台'
    t2.cell(2, 3).text = ''
    t2.cell(2, 4).text = ''
    t2.cell(3, 0).text = '合计'

    p = _save_temp_docx(doc)
    try:
        res = extract_price_data(p)
        assert res['status'] == 'NEED_MANUAL'
        assert res['has_price_table_skeleton'] is True
        assert res['items'] == []
        assert res['bid_total'] is None
        assert res['price_cap'] == '1200000.00'
    finally:
        p.unlink(missing_ok=True)


# ── 3. 完整投标书 docx 提取（含合计行隔离与合并单元格去重） ────────────

def test_docx_bid_extraction_and_vmerge_dedup():
    doc = Document()
    doc.add_paragraph('一、开标一览表')
    t_bid = doc.add_table(rows=3, cols=2)
    t_bid.cell(0, 0).text = '项目名称'
    t_bid.cell(0, 1).text = '某省政务云安全态势感知项目'
    t_bid.cell(1, 0).text = '投标总报价（小写）'
    t_bid.cell(1, 1).text = '￥485,500.00元'
    t_bid.cell(2, 0).text = '投标总报价（大写）'
    t_bid.cell(2, 1).text = '人民币肆拾捌万伍仟伍佰元整'

    doc.add_paragraph('二、分项报价表')
    t_items = doc.add_table(rows=5, cols=5)
    for idx, h in enumerate(['序号', '货物名称', '数量', '投标单价(元)', '投标合价(元)']):
        t_items.cell(0, idx).text = h

    rows_data = [
        ('1', '日志审计系统', '2台', '42,500.00', '85,000.00'),
        ('2', '数据库审计系统', '1套', '168,000.00', '168,000.00'),
        ('3', '运维安全堡垒机', '3台', '77,500.00', '232,500.00'),
        ('合计', '报价合计', '', '', '485,500.00'),
    ]
    for r_i, r_vals in enumerate(rows_data, start=1):
        for c_i, val in enumerate(r_vals):
            t_items.cell(r_i, c_i).text = val

    p = _save_temp_docx(doc)
    try:
        chk = run_price_check(p)
        assert chk['status'] == 'OK'
        assert len(chk['items']) == 3                 # 合计行被严格隔离，未混入 items
        assert chk['table_total'] == '485500.00'
        assert chk['bid_total'] == '485500.00'
        assert chk['bid_total_upper'] == '肆拾捌万伍仟伍佰元整'
        assert chk['findings'] == []                  # 全部算术一致，0 报错
    finally:
        p.unlink(missing_ok=True)


# ── 4. 表头为「万元」单位的分项报价表自动折算为元 ────────────────────────

def test_docx_table_with_wan_unit_header():
    doc = Document()
    t = doc.add_table(rows=3, cols=4)
    for i, h in enumerate(['分项名称', '数量', '单价（万元）', '合价（万元）']):
        t.cell(0, i).text = h
    t.cell(1, 0).text = '核心业务应用开发'
    t.cell(1, 1).text = '1项'
    t.cell(1, 2).text = '35.5'
    t.cell(1, 3).text = '35.5'
    t.cell(2, 0).text = '合计'
    t.cell(2, 3).text = '35.5'

    p = _save_temp_docx(doc)
    try:
        res = extract_price_data(p)
        assert res['status'] == 'OK'
        assert len(res['items']) == 1
        assert res['items'][0]['unit_price'] == '355000.00'
        assert res['items'][0]['total_price'] == '355000.00'
        assert res['table_total'] == '355000.00'
    finally:
        p.unlink(missing_ok=True)


# ── 5. 端到端检出算术错误、跨表差额、大写错字与超限价 ────────────────────

def test_end_to_end_run_price_check_catches_errors():
    # 招标文件（提供 120 万最高限价）
    tender_doc = Document()
    tender_doc.add_paragraph('投标人须知：本项目最高投标限价为 120.00 万元。')
    tender_p = _save_temp_docx(tender_doc)

    # 投标文件（故意埋入：第2项单价×数量差500元、表合计与一览表差500元、大写错字、超120万限价）
    bid_doc = Document()
    bid_doc.add_paragraph('投标总报价：1,286,000.00元')
    bid_doc.add_paragraph('投标总报价（大写）：人民币壹佰贰拾捌万柒仟元整')
    t = bid_doc.add_table(rows=4, cols=4)
    for i, h in enumerate(['项目名称', '数量', '单价（元）', '小计（元）']):
        t.cell(0, i).text = h
    t.cell(1, 0).text = '态势感知平台软件'
    t.cell(1, 1).text = '1套'
    t.cell(1, 2).text = '1,201,500.00'
    t.cell(1, 3).text = '1,201,500.00'
    t.cell(2, 0).text = '日志采集器'
    t.cell(2, 1).text = '2台'
    t.cell(2, 2).text = '42,500.00'
    t.cell(2, 3).text = '84,500.00'                   # 应为 85,000.00（算错500元）
    t.cell(3, 0).text = '合计'
    t.cell(3, 3).text = '1,286,500.00'
    bid_p = _save_temp_docx(bid_doc)

    try:
        res = run_price_check(bid_p, tender_path=tender_p)
        assert res['status'] == 'OK'
        assert res['price_cap'] == '1200000.00'
        codes = {f['code'] for f in res['findings']}
        assert 'ITEM_ARITHMETIC' in codes
        assert 'TOTAL_MISMATCH' in codes
        assert 'BID_TOTAL_MISMATCH' in codes
        assert 'UPPER_MISMATCH' in codes
        assert 'CAP_EXCEEDED' in codes
    finally:
        tender_p.unlink(missing_ok=True)
        bid_p.unlink(missing_ok=True)


# ── 6. 纯文本提取与招标说明噪声过滤 ───────────────────────────────────

def test_text_extraction_and_noise_filtering():
    tender_txt = """
    投标报价不得超过最高限价150万元（大写：壹佰伍拾万元整），否则按废标处理。
    投标保证金：人民币贰万元整（20000元）。
    """
    r_tender = extract_price_from_text(tender_txt)
    assert r_tender['status'] == 'NEED_MANUAL'
    assert r_tender['price_cap'] == '1500000.00'
    assert r_tender['bid_total'] is None
    assert r_tender['bid_total_upper'] is None

    bid_txt = """
    开标一览表
    投标总报价：小写 680,000.00 元
    投标总报价（大写）：人民币陆拾捌万元整
    1 应用软件开发 1项 500,000.00 500,000.00
    2 驻场运维服务 2年 90,000.00 180,000.00
    分项合计：680,000.00元
    """
    r_bid = extract_price_from_text(bid_txt)
    assert r_bid['status'] == 'OK'
    assert r_bid['bid_total'] == '680000.00'
    assert r_bid['bid_total_upper'] == '陆拾捌万元整'
    assert len(r_bid['items']) == 2
    assert r_bid['table_total'] == '680000.00'

# ── 7. 幻影金额防护与"圆"写法（2026-11-04 试跑实测回归）────────────────

def test_prose_context_requires_currency_unit():
    """自由文本语境数字必须紧邻货币单位：'第9条要求'不得被解析成 9.00 元
    （滁州托育实测：资格审查表邻格说明文字产生幻影 bid_total=9.00）。"""
    from core.price_extractor import _parse_money_str
    assert _parse_money_str('符合招标文件投标人须知正文第9条要求', require_unit=True) is None
    assert _parse_money_str('详见第六章投标文件格式', require_unit=True) is None
    # 有货币单位的正常文本可以提取
    assert _parse_money_str('投标总报价：680,000.00元', require_unit=True) == Decimal('680000.00')
    assert _parse_money_str('最高限价145万元', require_unit=True) == Decimal('1450000.00')
    # 专用数值单元格不受影响（无单位纯数字照常解析）
    assert _parse_money_str('1286500.00') == Decimal('1286500.00')


def test_price_sandbox_accepts_yuan_variant():
    """price_sandbox 归一化兼容'圆'代'元'（Gemini 建议采纳）。"""
    r = PriceSandbox(bid_total='1286500.00',
                     bid_total_upper='壹佰贰拾捌万陆仟伍佰圆整').run()
    assert r == []
