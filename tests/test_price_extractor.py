# -*- coding: utf-8 -*-
"""F2-2 报价表提取辅助层单元测试（core/price_extractor.py）。

覆盖验收点：
1. 招标文件最高限价/采购预算提取与「万元 -> 元」自动折算；
2. 招标文件空白《开标一览表/分项报价表》模板识别并诚实返回 NEED_MANUAL（不把序号当金额）；
3. 投标文件 docx（开标一览表 + 分项报价明细表 + 合计行隔离 + 合并单元格去重）提取为结构化数据；
4. 表头标注「单位：万元」的分项报价表自动折算为元；
5. 纯文本 / PDF 文本行报价提取与噪声过滤（不误抓评分公式或保证金大写）；
6. run_price_check() 端到端串联 PriceSandbox 检出算术错误、跨表差额、大写错误与超限价。
7. E7 开标一览表提取鲁棒性（三列同行限价在前/报价格在前、标签格含限价语境、双金额歧义护栏、开标一览表主源优先与自洽表交叉核验）。
8. E8 多明细表小计聚合与分项汇总表权威源核验（单表全分项、多分表+小计聚合、多分表+小计+汇总表权威源、Σ小计与汇总表不一致报 warning 非 error、小计行混限价语境词排除、防双重计数）。
9. E9 明细行收集完整性（子序号 1.1/2.1、多字符单位如人月/个月/式、纵向合并分类格下探取名、无单价列小计分表）与租赁时长/服务期限定价算术核验（元/年×3年、36个月折年、期限不可解析时 warning 跳过算术硬判）。
10. E13 双胞胎明细表去重（滁州物业实锤：两张明细表 8 行金额完全一致但名称略有详简差异 -> 去重保留 8 条并报 DUPLICATE_DETAIL_TABLE warning；差 1 条金额不触发去重仍报 TOTAL_MISMATCH；三表同现及列更全者优先保留）。
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


def test_blank_format_table_lineno_sequence_not_misread():
    """空白投标格式的表头词+行号序列（1 2 3）严禁被解析成假报价行（亳州语料实测坑）。"""
    text = (
        "序 号 分项名称 服务费用报价依据 单价（元） 金额（元） 1 2 3 …\n"
        "数量 单价（元） 金额（元） 1 2 3\n"
        "备注 1 2 3\n"
        "证书有效期 1 2 3\n"
    )
    res = extract_price_from_text(text)
    assert res['items'] == []
    assert res['status'] == 'NEED_MANUAL'


# ── 8. E7 新增测试集：限价/报价同行防误判、多金额歧义护栏与开标一览表主源优先 ──────

def test_e7_three_cols_cap_before_bid_extracts_bid_total_without_error():
    """E7 用例 ①：三列同行、限价格在前（主复现用例：投标总报价 | 不得高于最高限价390,000.00元 | 人民币 382,000.00 元（叁拾捌万贰仟元整））。"""
    doc = Document()
    doc.add_paragraph('一、商务承诺自洽性核验表')
    t_self = doc.add_table(rows=2, cols=3)
    t_self.cell(0, 0).text = '承诺要点'
    t_self.cell(0, 1).text = '招标文件要求'
    t_self.cell(0, 2).text = '我方投标响应承诺'
    t_self.cell(1, 0).text = '投标总报价'
    t_self.cell(1, 1).text = '不得高于最高限价390,000.00元'
    t_self.cell(1, 2).text = '人民币 382,000.00 元（叁拾捌万贰仟元整）'

    doc.add_paragraph('二、分项报价明细表')
    t_items = doc.add_table(rows=3, cols=4)
    for i, h in enumerate(['品目名称', '数量', '单价（元）', '合价（元）']):
        t_items.cell(0, i).text = h
    t_items.cell(1, 0).text = '智慧托育实训平台软件'
    t_items.cell(1, 1).text = '1套'
    t_items.cell(1, 2).text = '382,000.00'
    t_items.cell(1, 3).text = '382,000.00'
    t_items.cell(2, 0).text = '合计'
    t_items.cell(2, 3).text = '382,000.00'

    p = _save_temp_docx(doc)
    try:
        chk = run_price_check(p)
        assert chk['status'] == 'OK'
        assert chk['bid_total'] == '382000.00'
        assert chk['bid_total_upper'] == '叁拾捌万贰仟元整'
        assert chk['table_total'] == '382000.00'
        assert chk['price_cap'] == '390000.00'
        assert chk['findings'] == []
    finally:
        p.unlink(missing_ok=True)


def test_e7_three_cols_bid_before_cap_extracts_bid_total_without_error():
    """E7 用例 ②：三列同行、报价格在前，且限价格内含限价大写（叁拾玖万元整），严禁混淆。"""
    doc = Document()
    t = doc.add_table(rows=2, cols=3)
    t.cell(0, 0).text = '承诺要点'
    t.cell(0, 1).text = '我方投标响应承诺'
    t.cell(0, 2).text = '招标文件要求'
    t.cell(1, 0).text = '投标总报价'
    t.cell(1, 1).text = '人民币 382,000.00 元（叁拾捌万贰仟元整）'
    t.cell(1, 2).text = '不得高于最高限价390,000.00元（叁拾玖万元整）'

    p = _save_temp_docx(doc)
    try:
        chk = run_price_check(p)
        assert chk['status'] == 'OK'
        assert chk['bid_total'] == '382000.00'
        assert chk['bid_total_upper'] == '叁拾捌万贰仟元整'
        assert chk['findings'] == []
    finally:
        p.unlink(missing_ok=True)


def test_e7_cap_keyword_inside_label_cell_itself():
    """E7 用例 ③：限价语境词出现在标签格自身（如标签"投标总报价（不得高于最高限价）"）。"""
    # 场景 3a：标签格带括号限价提醒，后续响应格为真实投标报价 -> 正常提取响应格报价
    doc_a = Document()
    t_a = doc_a.add_table(rows=2, cols=2)
    t_a.cell(0, 0).text = '投标总报价（不得高于最高限价390,000.00元）'
    t_a.cell(0, 1).text = '人民币 382,000.00 元（叁拾捌万贰仟元整）'
    t_a.cell(1, 0).text = '工期要求'
    t_a.cell(1, 1).text = '合同签订后 30 日历天'

    p_a = _save_temp_docx(doc_a)
    try:
        res_a = run_price_check(p_a)
        assert res_a['status'] == 'OK'
        assert res_a['bid_total'] == '382000.00'
        assert res_a['bid_total_upper'] == '叁拾捌万贰仟元整'
        assert res_a['findings'] == []
    finally:
        p_a.unlink(missing_ok=True)

    # 场景 3b：单格或值格本身仅为限价要求说明 -> 不得误提为 bid_total
    doc_b = Document()
    t_b = doc_b.add_table(rows=1, cols=2)
    t_b.cell(0, 0).text = '投标总报价（不得高于最高限价）'
    t_b.cell(0, 1).text = '不得高于最高限价 390,000.00 元'

    p_b = _save_temp_docx(doc_b)
    try:
        res_b = extract_price_data(p_b)
        assert res_b['bid_total'] is None
        assert res_b['status'] == 'NEED_MANUAL'
    finally:
        p_b.unlink(missing_ok=True)


def test_e7_standard_bid_summary_table_unscathed():
    """E7 用例 ④：规范《开标一览表》（横向表头式）照常提取，零误伤。"""
    doc = Document()
    doc.add_paragraph('一、开标一览表')
    t = doc.add_table(rows=2, cols=4)
    t.cell(0, 0).text = '项目名称'
    t.cell(0, 1).text = '投标总报价（元）'
    t.cell(0, 2).text = '投标总报价（大写）'
    t.cell(0, 3).text = '交付期'
    t.cell(1, 0).text = '智慧园区安防改造采购项目'
    t.cell(1, 1).text = '382,000.00'
    t.cell(1, 2).text = '叁拾捌万贰仟元整'
    t.cell(1, 3).text = '30日历天'

    p = _save_temp_docx(doc)
    try:
        chk = run_price_check(p)
        assert chk['status'] == 'OK'
        assert chk['bid_total'] == '382000.00'
        assert chk['bid_total_upper'] == '叁拾捌万贰仟元整'
        assert chk['findings'] == []
    finally:
        p.unlink(missing_ok=True)


def test_e7_dual_amounts_without_context_words_triggers_warning_not_error():
    """E7 用例 ⑤：双金额无语境词歧义行 -> bid_total=None + warning 级 finding（绝不产出 error）。"""
    doc = Document()
    t_amb = doc.add_table(rows=2, cols=3)
    t_amb.cell(0, 0).text = '要素名称'
    t_amb.cell(0, 1).text = '数值一'
    t_amb.cell(0, 2).text = '数值二'
    t_amb.cell(1, 0).text = '投标总报价'
    t_amb.cell(1, 1).text = '人民币 390,000.00 元'
    t_amb.cell(1, 2).text = '人民币 382,000.00 元'

    t_items = doc.add_table(rows=3, cols=4)
    for i, h in enumerate(['品目名称', '数量', '单价（元）', '合价（元）']):
        t_items.cell(0, i).text = h
    t_items.cell(1, 0).text = '核心交换机'
    t_items.cell(1, 1).text = '1台'
    t_items.cell(1, 2).text = '382,000.00'
    t_items.cell(1, 3).text = '382,000.00'
    t_items.cell(2, 0).text = '合计'
    t_items.cell(2, 3).text = '382,000.00'

    p = _save_temp_docx(doc)
    try:
        chk = run_price_check(p)
        assert chk['bid_total'] is None
        assert len(chk['findings']) >= 1
        assert all(f['severity'] == 'warning' for f in chk['findings'])
        assert any(f['code'] == 'AMBIGUOUS_BID_TOTAL' for f in chk['findings'])
    finally:
        p.unlink(missing_ok=True)


def test_e7_primary_bid_summary_table_wins_over_self_check_table():
    """E7 用例 ⑥：独立《开标一览表》与自洽表并存时，bid_total 只从开标一览表赋值，自洽表仅作交叉核验。"""
    doc = Document()
    # 先放一份自洽表（故意把自洽表里的响应金额写成 380,000.00 元，测试其既不会抢占赋值、不一致也只报 warning）
    doc.add_paragraph('一、商务响应自洽表')
    t_self = doc.add_table(rows=2, cols=3)
    t_self.cell(0, 0).text = '承诺要点'
    t_self.cell(0, 1).text = '招标文件要求'
    t_self.cell(0, 2).text = '我方投标响应承诺'
    t_self.cell(1, 0).text = '投标总报价'
    t_self.cell(1, 1).text = '不得高于最高限价390,000.00元'
    t_self.cell(1, 2).text = '人民币 380,000.00 元'

    # 再放独立《开标一览表》（真实报价 382,000.00 元）
    doc.add_paragraph('二、开标一览表')
    t_primary = doc.add_table(rows=3, cols=2)
    t_primary.cell(0, 0).text = '项目名称'
    t_primary.cell(0, 1).text = '某市政务云扩容采购项目'
    t_primary.cell(1, 0).text = '投标总报价（小写）'
    t_primary.cell(1, 1).text = '382,000.00元'
    t_primary.cell(2, 0).text = '投标总报价（大写）'
    t_primary.cell(2, 1).text = '人民币叁拾捌万贰仟元整'

    # 分项报价表与开标一览表一致（382,000.00 元）
    doc.add_paragraph('三、分项报价表')
    t_items = doc.add_table(rows=3, cols=4)
    for i, h in enumerate(['货物名称', '数量', '投标单价(元)', '投标合价(元)']):
        t_items.cell(0, i).text = h
    t_items.cell(1, 0).text = '云平台虚拟化套件'
    t_items.cell(1, 1).text = '1套'
    t_items.cell(1, 2).text = '382,000.00'
    t_items.cell(1, 3).text = '382,000.00'
    t_items.cell(2, 0).text = '合计'
    t_items.cell(2, 3).text = '382,000.00'

    p = _save_temp_docx(doc)
    try:
        chk = run_price_check(p)
        assert chk['status'] == 'OK'
        # bid_total 严格取自独立《开标一览表》的 382000.00，而非前面自洽表的 380000.00
        assert chk['bid_total'] == '382000.00'
        assert chk['bid_total_upper'] == '叁拾捌万贰仟元整'
        # 自洽表与开标一览表差异仅产出 warning 级 CROSS_TABLE_BID_WARNING，0 条 error
        errors = [f for f in chk['findings'] if f['severity'] == 'error']
        warnings = [f for f in chk['findings'] if f['severity'] == 'warning']
        assert errors == []
        assert len(warnings) == 1
        assert warnings[0]['code'] == 'CROSS_TABLE_BID_WARNING'
    finally:
        p.unlink(missing_ok=True)


# ── 9. E8 新增测试集：多明细表小计聚合、分项汇总表权威源与防双重计数 ──────────

def _add_detail_subtable(doc: Document, title: str, rows_spec: list[tuple[str, str, str, str]], subtotal_str: str, summary_label: str = '小计'):
    """辅助构造一张带小计/合计行的分表明细表。"""
    doc.add_paragraph(title)
    t = doc.add_table(rows=len(rows_spec) + 2, cols=5)
    for idx, h in enumerate(['序号', '子系统/模块名称', '数量', '单价（元）', '合价（元）']):
        t.cell(0, idx).text = h
    for r_i, (nm, qty, up, tp) in enumerate(rows_spec, start=1):
        t.cell(r_i, 0).text = str(r_i)
        t.cell(r_i, 1).text = nm
        t.cell(r_i, 2).text = qty
        t.cell(r_i, 3).text = up
        t.cell(r_i, 4).text = tp
    last_r = len(rows_spec) + 1
    t.cell(last_r, 0).text = summary_label
    t.cell(last_r, 1).text = f"{summary_label}金额"
    t.cell(last_r, 4).text = subtotal_str
    return t


def _add_summary_price_table(doc: Document, sub_rows: list[tuple[str, str]], grand_total_str: str):
    """辅助构造一张无单价/数量列但含'分项名称+金额+合计行'的《分项汇总表》。"""
    doc.add_paragraph('分项报价汇总表')
    t = doc.add_table(rows=len(sub_rows) + 2, cols=4)
    for idx, h in enumerate(['序号', '分项名称', '金额（元）', '备注']):
        t.cell(0, idx).text = h
    for r_i, (nm, amt) in enumerate(sub_rows, start=1):
        t.cell(r_i, 0).text = str(r_i)
        t.cell(r_i, 1).text = nm
        t.cell(r_i, 2).text = amt
        t.cell(r_i, 3).text = '含税'
    last_r = len(sub_rows) + 1
    t.cell(last_r, 0).text = '合计'
    t.cell(last_r, 1).text = '总计金额'
    t.cell(last_r, 2).text = grand_total_str
    return t


def test_e8_single_table_with_both_subtotal_and_grand_total_prefers_grand_total():
    """E8 用例 ①：单表全分项（同表内既有阶段小计又有总合计）-> 合计优先，不得误伤。"""
    doc = Document()
    doc.add_paragraph('一、开标一览表')
    t_bid = doc.add_table(rows=3, cols=2)
    t_bid.cell(0, 0).text = '项目名称'
    t_bid.cell(0, 1).text = '单表小计与合计并存测试项目'
    t_bid.cell(1, 0).text = '投标总报价（小写）'
    t_bid.cell(1, 1).text = '300,000.00元'
    t_bid.cell(2, 0).text = '投标总报价（大写）'
    t_bid.cell(2, 1).text = '人民币叁拾万元整'

    t = doc.add_table(rows=5, cols=5)
    for idx, h in enumerate(['序号', '品目名称', '数量', '单价（元）', '合价（元）']):
        t.cell(0, idx).text = h
    t.cell(1, 0).text = '1'
    t.cell(1, 1).text = '软件分项A'
    t.cell(1, 2).text = '1项'
    t.cell(1, 3).text = '180,000.00'
    t.cell(1, 4).text = '180,000.00'
    t.cell(2, 0).text = '小计'
    t.cell(2, 1).text = '一阶段小计'
    t.cell(2, 4).text = '180,000.00'
    t.cell(3, 0).text = '2'
    t.cell(3, 1).text = '软件分项B'
    t.cell(3, 2).text = '1项'
    t.cell(3, 3).text = '120,000.00'
    t.cell(3, 4).text = '120,000.00'
    t.cell(4, 0).text = '合计'
    t.cell(4, 1).text = '报价总计'
    t.cell(4, 4).text = '300,000.00'

    p = _save_temp_docx(doc)
    try:
        chk = run_price_check(p)
        assert chk['status'] == 'OK'
        assert len(chk['items']) == 2
        assert chk['table_total'] == '300000.00'
        assert chk['bid_total'] == '300000.00'
        assert chk['findings'] == []
    finally:
        p.unlink(missing_ok=True)


def test_e8_multiple_subtables_with_subtotals_no_summary_table_aggregates_sum():
    """E8 用例 ②：多分表+小计、无汇总表（118万 + 86万 + 70.6万 + 24万 = 298.6万） -> table_total = Σ小计，0 error。"""
    doc = Document()
    doc.add_paragraph('一、开标一览表')
    t_bid = doc.add_table(rows=3, cols=2)
    t_bid.cell(0, 0).text = '项目名称'
    t_bid.cell(0, 1).text = '某省生态环境智慧监管平台项目'
    t_bid.cell(1, 0).text = '投标总报价（小写）'
    t_bid.cell(1, 1).text = '2,986,000.00元'
    t_bid.cell(2, 0).text = '投标总报价（大写）'
    t_bid.cell(2, 1).text = '人民币贰佰玖拾捌万陆仟元整'

    _add_detail_subtable(doc, '明细表A', [('子系统A1', '1套', '680,000.00', '680,000.00'), ('子系统A2', '1套', '500,000.00', '500,000.00')], '1,180,000.00')
    _add_detail_subtable(doc, '明细表B', [('子系统B1', '2套', '430,000.00', '860,000.00')], '860,000.00')
    _add_detail_subtable(doc, '明细表C', [('模块C1', '1项', '706,000.00', '706,000.00')], '706,000.00')
    _add_detail_subtable(doc, '明细表D', [('运维项D1', '1年', '240,000.00', '240,000.00')], '240,000.00')

    p = _save_temp_docx(doc)
    try:
        chk = run_price_check(p)
        assert chk['status'] == 'OK'
        assert len(chk['items']) == 5
        assert chk['table_total'] == '2986000.00'
        assert chk['bid_total'] == '2986000.00'
        assert chk['findings'] == []
    finally:
        p.unlink(missing_ok=True)


def test_e8_multiple_subtables_plus_summary_table_uses_summary_as_authority():
    """E8 用例 ③：多分表+小计+汇总表（实锤原貌：汇总表五大分项 + 4张分表明细表 + 开标一览表） -> 取汇总表合计为权威源，0 error。"""
    doc = Document()
    doc.add_paragraph('一、开标一览表')
    t_bid = doc.add_table(rows=3, cols=2)
    t_bid.cell(0, 0).text = '项目名称'
    t_bid.cell(0, 1).text = '固废管理信息系统升级改造项目'
    t_bid.cell(1, 0).text = '投标总报价（小写）'
    t_bid.cell(1, 1).text = '2,986,000.00元'
    t_bid.cell(2, 0).text = '投标总报价（大写）'
    t_bid.cell(2, 1).text = '人民币贰佰玖拾捌万陆仟元整'

    # 汇总表（五大分项金额 + 合计 2,986,000.00，其中分项3+分项4之和 = 明细表C 706,000.00）
    _add_summary_price_table(
        doc,
        [
            ('平台一建设费', '1,180,000.00'),
            ('平台二建设费', '860,000.00'),
            ('平台三模块一', '406,000.00'),
            ('平台三模块二', '300,000.00'),
            ('安全测评与运维服务', '240,000.00'),
        ],
        '2,986,000.00',
    )

    _add_detail_subtable(doc, '明细表A', [('子系统A1', '1套', '1,180,000.00', '1,180,000.00')], '1,180,000.00')
    _add_detail_subtable(doc, '明细表B', [('子系统B1', '1套', '860,000.00', '860,000.00')], '860,000.00')
    _add_detail_subtable(doc, '明细表C', [('模块C1', '1项', '406,000.00', '406,000.00'), ('模块C2', '1项', '300,000.00', '300,000.00')], '706,000.00')
    _add_detail_subtable(doc, '明细表D', [('服务D1', '1项', '240,000.00', '240,000.00')], '240,000.00')

    p = _save_temp_docx(doc)
    try:
        chk = run_price_check(p)
        assert chk['status'] == 'OK'
        assert chk['table_total'] == '2986000.00'
        assert chk['bid_total'] == '2986000.00'
        assert len(chk['items']) == 5
        assert chk['findings'] == []
    finally:
        p.unlink(missing_ok=True)


def test_e8_subtotal_sum_mismatch_with_summary_table_emits_warning_not_error():
    """E8 用例 ④：Σ小计与汇总表合计不一致 -> 取汇总表合计为 table_total，且产出 warning 级 finding（非 error）。"""
    doc = Document()
    doc.add_paragraph('一、开标一览表')
    t_bid = doc.add_table(rows=3, cols=2)
    t_bid.cell(0, 0).text = '项目名称'
    t_bid.cell(0, 1).text = '汇总表与分表小计差异测试项目'
    t_bid.cell(1, 0).text = '投标总报价（小写）'
    t_bid.cell(1, 1).text = '2,986,000.00元'
    t_bid.cell(2, 0).text = '投标总报价（大写）'
    t_bid.cell(2, 1).text = '人民币贰佰玖拾捌万陆仟元整'

    _add_summary_price_table(
        doc,
        [
            ('平台一建设费', '1,180,000.00'),
            ('平台二建设费', '860,000.00'),
            ('平台三建设费', '706,000.00'),
            ('运维服务费', '240,000.00'),
        ],
        '2,986,000.00',
    )

    # 仅附前 3 张分表明细（Σ小计 = 2,746,000.00 != 汇总表合计 2,986,000.00）
    _add_detail_subtable(doc, '明细表A', [('子系统A1', '1套', '1,180,000.00', '1,180,000.00')], '1,180,000.00')
    _add_detail_subtable(doc, '明细表B', [('子系统B1', '1套', '860,000.00', '860,000.00')], '860,000.00')
    _add_detail_subtable(doc, '明细表C', [('模块C1', '1项', '706,000.00', '706,000.00')], '706,000.00')

    p = _save_temp_docx(doc)
    try:
        chk = run_price_check(p)
        assert chk['status'] == 'OK'
        assert chk['table_total'] == '2986000.00'
        assert chk['bid_total'] == '2986000.00'
        errors = [f for f in chk['findings'] if f['severity'] == 'error']
        warnings = [f for f in chk['findings'] if f['severity'] == 'warning']
        assert errors == []
        assert len(warnings) == 1
        assert warnings[0]['code'] == 'SUBTOTAL_SUMMARY_MISMATCH'
    finally:
        p.unlink(missing_ok=True)


def test_e8_subtotal_row_with_cap_context_words_not_misread():
    """E8 用例 ⑤：小计行混入限价语境词（继承 E7 排除，既不把限价当小计，也不误判为 bid_total）。"""
    # 招标文件（总限价 250 万；分项小计行的括号限价注记是分项级限额，不应被当作总限价）
    tender_doc = Document()
    tender_doc.add_paragraph('投标人须知：本项目最高投标限价为 2,500,000.00 元。')
    tender_p = _save_temp_docx(tender_doc)

    doc = Document()
    doc.add_paragraph('一、开标一览表')
    t_bid = doc.add_table(rows=2, cols=2)
    t_bid.cell(0, 0).text = '项目名称'
    t_bid.cell(0, 1).text = '分包限价备注测试'
    t_bid.cell(1, 0).text = '投标总报价（小写）'
    t_bid.cell(1, 1).text = '2,040,000.00元'

    # 明细表A：小计行在合价格附带括号限价说明 "1,180,000.00（本分项不得高于最高限价1,300,000.00元）"
    _add_detail_subtable(
        doc,
        '明细表A',
        [('子系统A1', '1套', '1,180,000.00', '1,180,000.00')],
        '1,180,000.00（不得高于最高限价1,300,000.00元）',
        summary_label='投标总报价小计',
    )
    _add_detail_subtable(
        doc,
        '明细表B',
        [('子系统B1', '1套', '860,000.00', '860,000.00')],
        '860,000.00（不得超过最高限价900,000.00元）',
        summary_label='小计',
    )

    p = _save_temp_docx(doc)
    try:
        chk = run_price_check(p, tender_path=tender_p)
        assert chk['status'] == 'OK'
        assert chk['table_total'] == '2040000.00'
        assert chk['bid_total'] == '2040000.00'
        assert chk['findings'] == []
    finally:
        p.unlink(missing_ok=True)
        tender_p.unlink(missing_ok=True)


def test_e8_no_double_counting_when_summary_and_subtables_coexist():
    """E8 用例 ⑥：防双重计数——汇总表与分表明细表并存（无论汇总表在前或在后），table_total 与 items 均只计一次。"""
    doc = Document()
    _add_detail_subtable(doc, '明细表A', [('子系统A1', '1套', '1,180,000.00', '1,180,000.00')], '1,180,000.00')
    _add_detail_subtable(doc, '明细表B', [('子系统B1', '1套', '860,000.00', '860,000.00')], '860,000.00')
    # 汇总表放在各分表明细表之后
    _add_summary_price_table(
        doc,
        [('平台一', '1,180,000.00'), ('平台二', '860,000.00')],
        '2,040,000.00',
    )

    p = _save_temp_docx(doc)
    try:
        res = extract_price_data(p)
        assert res['status'] == 'OK'
        # 严禁把汇总表的 204 万与分表小计的 204 万翻倍累加成 408 万
        assert res['table_total'] == '2040000.00'
        # 汇总表的行（无单价/数量）严禁混入 items 明细列表
        assert len(res['items']) == 2
        items_sum = sum(Decimal(it['total_price']) for it in res['items'])
        assert items_sum == Decimal('2040000.00')
    finally:
        p.unlink(missing_ok=True)


# ── 10. E9 新增测试集：明细行收集完整性（子序号/多字量词/合并分类列）与租赁期限定价算术 ──

def test_e9_sub_sequence_numbers_and_multichar_qty_units_collected_completely():
    """E9 事故一：验证子序号（1.1/1.2/2.1）、多字符数量单位（12人月/36个月/1节点/1式）及纵向合并空分类名下探取名不漏行。"""
    doc = Document()
    doc.add_paragraph('一、开标一览表')
    t_bid = doc.add_table(rows=2, cols=2)
    t_bid.cell(0, 0).text = '项目名称'
    t_bid.cell(0, 1).text = '徐州智慧园区综合信息化建设项目'
    t_bid.cell(1, 0).text = '投标总报价（小写）'
    t_bid.cell(1, 1).text = '3,200,000.00元'

    # 构造含一级分类（vMerge 延续行留空）、二级子序号（1.1/1.2/2.1）与多字符单位（人月/个月/式/节点）的分表明细表
    doc.add_paragraph('二、定制开发与驻场运维分项明细表')
    t = doc.add_table(rows=6, cols=6)
    headers = ['序号', '项目名称', '功能模块', '工作量', '单价（元）', '合价（元）']
    for i, h in enumerate(headers):
        t.cell(0, i).text = h

    rows_data = [
        ('1.1', '数据中台系统', '数据接入模块', '1式', '800,000.00', '800,000.00'),
        ('1.2', '', '数据治理模块', '1节点', '1,200,000.00', '1,200,000.00'),
        ('2.1', '驻场技术服务', '高级架构师驻场', '12人月', '50,000.00', '600,000.00'),
        ('2.2', '', '安全运维服务', '12个月', '50,000.00', '600,000.00'),
    ]
    for r_i, vals in enumerate(rows_data, start=1):
        for c_i, v in enumerate(vals):
            t.cell(r_i, c_i).text = v
    t.cell(5, 0).text = '小计'
    t.cell(5, 1).text = '本表小计'
    t.cell(5, 5).text = '3,200,000.00'

    p = _save_temp_docx(doc)
    try:
        chk = run_price_check(p)
        assert chk['status'] == 'OK'
        assert len(chk['items']) == 4
        assert chk['table_total'] == '3200000.00'
        assert chk['bid_total'] == '3200000.00'
        assert chk['findings'] == []
    finally:
        p.unlink(missing_ok=True)


def test_e9_leasing_duration_pricing_year_and_month_conversion_zero_error():
    """E9 事故二（连云港实锤）：单价 (元/年) + 租赁期限 (3年 / 36个月) + 数量×期限复合定价算术校验全通过。"""
    doc = Document()
    doc.add_paragraph('一、开标一览表')
    t_bid = doc.add_table(rows=2, cols=2)
    t_bid.cell(0, 0).text = '项目名称'
    t_bid.cell(0, 1).text = '连云港政务云资源租赁采购项目'
    t_bid.cell(1, 0).text = '投标总报价（小写）'
    t_bid.cell(1, 1).text = '2,400,000.00元'

    # 表 A：无独立数量列，含"租赁期限"列 + "单价 (元/年)"列（含 "3年" 与 "36个月" 折年）
    doc.add_paragraph('二、云资源租赁报价明细表')
    t1 = doc.add_table(rows=4, cols=5)
    for i, h in enumerate(['序号', '租赁服务名称', '租赁期限', '单价 (元/年)', '合价 (元)']):
        t1.cell(0, i).text = h
    t1.cell(1, 0).text = '1'
    t1.cell(1, 1).text = '云管理节点租赁'
    t1.cell(1, 2).text = '3年'
    t1.cell(1, 3).text = '320,000.00'
    t1.cell(1, 4).text = '960,000.00'

    t1.cell(2, 0).text = '2'
    t1.cell(2, 1).text = '云安全防护服务租赁'
    t1.cell(2, 2).text = '36个月'
    t1.cell(2, 3).text = '160,000.00'
    t1.cell(2, 4).text = '480,000.00'

    t1.cell(3, 0).text = '小计'
    t1.cell(3, 4).text = '1,440,000.00'

    # 表 B：同时含"数量"列（2台）与"服务期"列（3年），有效数量 = 2 × 3 = 6
    doc.add_paragraph('三、专线与主机租赁明细表')
    t2 = doc.add_table(rows=3, cols=6)
    for i, h in enumerate(['序号', '设备名称', '数量', '服务期', '单价 (元/年)', '合价 (元)']):
        t2.cell(0, i).text = h
    t2.cell(1, 0).text = '1'
    t2.cell(1, 1).text = '高性能计算节点租赁'
    t2.cell(1, 2).text = '2台'
    t2.cell(1, 3).text = '3年'
    t2.cell(1, 4).text = '160,000.00'
    t2.cell(1, 5).text = '960,000.00'

    t2.cell(2, 0).text = '小计'
    t2.cell(2, 5).text = '960,000.00'

    p = _save_temp_docx(doc)
    try:
        chk = run_price_check(p)
        assert chk['status'] == 'OK'
        assert len(chk['items']) == 3
        assert chk['items'][0]['qty'] == '3'
        assert chk['items'][1]['qty'] == '3'
        assert chk['items'][2]['qty'] == '6'
        assert chk['table_total'] == '2400000.00'
        assert chk['bid_total'] == '2400000.00'
        assert chk['findings'] == []
    finally:
        p.unlink(missing_ok=True)


def test_e9_unparseable_duration_skips_arithmetic_with_warning_not_error():
    """E9 事故二规则 3：期限定价行中期限无法解析（如 '-' 或空）时，跳过该行算术校验并产出 warning，严禁硬判 error。"""
    doc = Document()
    doc.add_paragraph('一、开标一览表')
    t_bid = doc.add_table(rows=2, cols=2)
    t_bid.cell(0, 0).text = '项目名称'
    t_bid.cell(0, 1).text = '期限缺省测试项目'
    t_bid.cell(1, 0).text = '投标总报价（小写）'
    t_bid.cell(1, 1).text = '960,000.00元'

    t = doc.add_table(rows=3, cols=5)
    for i, h in enumerate(['序号', '租赁服务名称', '租赁期限', '单价 (元/年)', '合价 (元)']):
        t.cell(0, i).text = h
    t.cell(1, 0).text = '1'
    t.cell(1, 1).text = '云灾备存储租赁'
    t.cell(1, 2).text = '-'
    t.cell(1, 3).text = '320,000.00'
    t.cell(1, 4).text = '960,000.00'
    t.cell(2, 0).text = '合计'
    t.cell(2, 4).text = '960,000.00'

    p = _save_temp_docx(doc)
    try:
        chk = run_price_check(p)
        assert chk['status'] == 'OK'
        assert len(chk['items']) == 1
        assert chk['table_total'] == '960000.00'
        errors = [f for f in chk['findings'] if f['severity'] == 'error']
        warnings = [f for f in chk['findings'] if f['severity'] == 'warning']
        assert errors == []
        assert len(warnings) == 1
        assert warnings[0]['code'] == 'DURATION_UNPARSEABLE_SKIP'
    finally:
        p.unlink(missing_ok=True)


# ── 11. E13 新增测试集：双胞胎明细表去重（滁州物业实锤）────────────────────

_CHUZHOU_8_ITEMS_B = [
    ('1', '7区县历史数据清洗迁移治理', '存量异构数据清洗与入库', '1项', '320,000.00', '320,000.00'),
    ('2', '维修资金核心业务管理子系统', '业主开户与交存支取管理', '1套', '450,000.00', '450,000.00'),
    ('3', '银企直联与资金监管结算接口', '多家商业银行专户直联', '1套', '280,000.00', '280,000.00'),
    ('4', '电子票据与财政非税对接模块', '财政电子票据自动开具', '1项', '160,000.00', '160,000.00'),
    ('5', '业主端移动表决与微信公众号', '线上实名认证与维修投票', '1套', '190,000.00', '190,000.00'),
    ('6', 'GIS地理信息楼盘表可视化模块', '自然幢与户室空间挂接', '1套', '180,000.00', '180,000.00'),
    ('7', '国产化信创环境适配与安全加固', '麒麟OS与达梦数据库适配', '1项', '140,000.00', '140,000.00'),
    ('8', '驻场运维与操作培训服务', '免费维保期内驻场保障', '1年', '100,000.00', '100,000.00'),
]

_CHUZHOU_8_ITEMS_C = [
    ('1', '全市7区县异构历史数据清洗迁移治理', '数据治理类', '1项', '320,000.00', '320,000.00'),
    ('2', '市级物业专项维修资金核心业务管理子系统', '应用软件类', '1套', '450,000.00', '450,000.00'),
    ('3', '商业银行专户银企直联与资金监管结算接口', '接口开发类', '1套', '280,000.00', '280,000.00'),
    ('4', '财政电子票据与非税系统对接模块', '接口开发类', '1项', '160,000.00', '160,000.00'),
    ('5', '业主端移动端投票表决与微信公众号服务', '移动应用类', '1套', '190,000.00', '190,000.00'),
    ('6', 'GIS空间地理信息楼盘表可视化管理模块', '应用软件类', '1套', '180,000.00', '180,000.00'),
    ('7', '国产化信创全栈适配改造与三级等保安全加固', '信创适配类', '1项', '140,000.00', '140,000.00'),
    ('8', '本地化驻场运维保障与全区县巡回操作培训', '技术服务类', '1年', '100,000.00', '100,000.00'),
]


def _fill_six_col_detail_table(doc: Document, title: str, headers: list[str], rows_data: list[tuple], total_str: str):
    doc.add_paragraph(title)
    t = doc.add_table(rows=len(rows_data) + 2, cols=6)
    for i, h in enumerate(headers):
        t.cell(0, i).text = h
    for r_i, vals in enumerate(rows_data, start=1):
        for c_i, v in enumerate(vals):
            t.cell(r_i, c_i).text = v
    last_r = len(rows_data) + 1
    t.cell(last_r, 0).text = '合计'
    t.cell(last_r, 1).text = '报价合计'
    t.cell(last_r, 5).text = total_str
    return t


def test_e13_twin_detail_tables_dedup_keeps_eight_items_and_warns():
    """E13 验收标准 1：双胞胎明细表（表 B、表 C 列名与分项详简不同但 8 行金额完全一致）-> items 恰 8 条、零 error、报 DUPLICATE_DETAIL_TABLE。"""
    doc = Document()
    doc.add_paragraph('采购预算与最高投标限价：1,923,700.00元')
    doc.add_paragraph('一、开标一览表')
    t_bid = doc.add_table(rows=3, cols=2)
    t_bid.cell(0, 0).text = '项目名称'
    t_bid.cell(0, 1).text = '滁州市物业专项维修资金系统整合迁移项目'
    t_bid.cell(1, 0).text = '投标总报价（小写）'
    t_bid.cell(1, 1).text = '1,820,000.00元'
    t_bid.cell(2, 0).text = '投标总报价（大写）'
    t_bid.cell(2, 1).text = '人民币壹佰捌拾贰万元整'

    _fill_six_col_detail_table(
        doc,
        '二、分项报价明细表（表B）',
        ['序号', '分项名称', '服务内容', '数量', '单价(元)', '合价(元)'],
        _CHUZHOU_8_ITEMS_B,
        '1,820,000.00',
    )
    _fill_six_col_detail_table(
        doc,
        '三、投标报价明细清册（表C）',
        ['分项序号', '分项工程名称', '服务类别/品牌规格', '数量及单位', '投标单价(元)', '投标合价(元)'],
        _CHUZHOU_8_ITEMS_C,
        '1,820,000.00',
    )

    p = _save_temp_docx(doc)
    try:
        chk = run_price_check(p)
        assert chk['status'] == 'OK'
        assert len(chk['items']) == 8
        assert chk['table_total'] == '1820000.00'
        assert chk['bid_total'] == '1820000.00'
        assert chk['price_cap'] == '1923700.00'

        errors = [f for f in chk['findings'] if f['severity'] == 'error']
        warnings = [f for f in chk['findings'] if f['severity'] == 'warning']
        assert errors == []
        assert any(f['code'] == 'DUPLICATE_DETAIL_TABLE' for f in warnings)
    finally:
        p.unlink(missing_ok=True)


def test_e13_non_identical_detail_tables_not_deduped_triggers_total_mismatch():
    """E13 验收标准 2（反向用例）：两张明细表金额序列不完全一致（哪怕只差 1 条）-> 不触发去重，保持 TOTAL_MISMATCH 报错。"""
    doc = Document()
    doc.add_paragraph('一、开标一览表')
    t_bid = doc.add_table(rows=2, cols=2)
    t_bid.cell(0, 0).text = '项目名称'
    t_bid.cell(0, 1).text = '非双胞胎错标测试项目'
    t_bid.cell(1, 0).text = '投标总报价（小写）'
    t_bid.cell(1, 1).text = '1,820,000.00元'

    _fill_six_col_detail_table(
        doc,
        '二、分项报价明细表（表B）',
        ['序号', '分项名称', '服务内容', '数量', '单价(元)', '合价(元)'],
        _CHUZHOU_8_ITEMS_B,
        '1,820,000.00',
    )

    # 表 C 第 8 项金额改为 110,000.00（与表 B 的 100,000.00 差 1 条）
    modified_c = list(_CHUZHOU_8_ITEMS_C)
    modified_c[-1] = ('8', '本地化驻场运维保障', '技术服务类', '1年', '110,000.00', '110,000.00')
    _fill_six_col_detail_table(
        doc,
        '三、投标报价明细清册（表C）',
        ['分项序号', '分项工程名称', '服务类别/品牌规格', '数量及单位', '投标单价(元)', '投标合价(元)'],
        modified_c,
        '1,820,000.00',
    )

    p = _save_temp_docx(doc)
    try:
        chk = run_price_check(p)
        assert len(chk['items']) == 16
        codes = {f['code'] for f in chk['findings']}
        assert 'DUPLICATE_DETAIL_TABLE' not in codes
        assert 'TOTAL_MISMATCH' in codes
    finally:
        p.unlink(missing_ok=True)


def test_e13_three_tables_coexist_and_column_completeness_priority():
    """E13 验收标准 3：三表同现（表 A 汇总 + 表 B/C 双胞胎，且先出现的表 B 缺单价列、后出现的表 C 列更全）-> 优先保留列更全的表 C，items 8 条，table_total 正确。"""
    doc = Document()
    doc.add_paragraph('一、开标一览表')
    t_bid = doc.add_table(rows=2, cols=2)
    t_bid.cell(0, 0).text = '项目名称'
    t_bid.cell(0, 1).text = '滁州物业三表同现测试'
    t_bid.cell(1, 0).text = '投标总报价（小写）'
    t_bid.cell(1, 1).text = '1,820,000.00元'

    # 表 A：分项合价汇总（"分项合价(元)" + "占总报价比例"，无单价/数量列）
    doc.add_paragraph('二、分项合价汇总表（表A）')
    t_a = doc.add_table(rows=10, cols=4)
    for i, h in enumerate(['序号', '分项名称', '分项合价(元)', '占总报价比例']):
        t_a.cell(0, i).text = h
    for r_i, row_b in enumerate(_CHUZHOU_8_ITEMS_B, start=1):
        t_a.cell(r_i, 0).text = row_b[0]
        t_a.cell(r_i, 1).text = row_b[1]
        t_a.cell(r_i, 2).text = row_b[5]
        t_a.cell(r_i, 3).text = '12.5%'
    t_a.cell(9, 0).text = '合计'
    t_a.cell(9, 1).text = '合计'
    t_a.cell(9, 2).text = '1,820,000.00'
    t_a.cell(9, 3).text = '100%'

    # 表 B（先出现，但只有 序号 | 分项名称 | 数量 | 合价(元)，缺少单价列）
    doc.add_paragraph('三、简化分项明细表（表B）')
    t_b = doc.add_table(rows=9, cols=4)
    for i, h in enumerate(['序号', '分项名称', '数量', '合价(元)']):
        t_b.cell(0, i).text = h
    for r_i, row_b in enumerate(_CHUZHOU_8_ITEMS_B, start=1):
        t_b.cell(r_i, 0).text = row_b[0]
        t_b.cell(r_i, 1).text = row_b[1]
        t_b.cell(r_i, 2).text = row_b[3]
        t_b.cell(r_i, 3).text = row_b[5]

    # 表 C（后出现，含数量列与单价列，列更全）
    _fill_six_col_detail_table(
        doc,
        '四、完整投标报价明细清册（表C）',
        ['分项序号', '分项工程名称', '服务类别/品牌规格', '数量及单位', '投标单价(元)', '投标合价(元)'],
        _CHUZHOU_8_ITEMS_C,
        '1,820,000.00',
    )

    p = _save_temp_docx(doc)
    try:
        chk = run_price_check(p)
        assert chk['status'] == 'OK'
        assert len(chk['items']) == 8
        assert chk['table_total'] == '1820000.00'
        # 验证优先保留了列更全的表 C（其首项名称为 '全市7区县异构历史数据清洗迁移治理'）
        assert chk['items'][0]['name'] == '全市7区县异构历史数据清洗迁移治理'
        errors = [f for f in chk['findings'] if f['severity'] == 'error']
        assert errors == []
        assert any(f['code'] == 'DUPLICATE_DETAIL_TABLE' for f in chk['findings'])
    finally:
        p.unlink(missing_ok=True)