# -*- coding: utf-8 -*-
"""F2 报价算术沙盒单元测试（0 Token，纯离线）。

覆盖验收线：差 1 分钱报警、单价×数量误差、大写错一字、最高限价边缘，
以及人民币大写转换的零/整/角分/跨节零边缘。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from decimal import Decimal
from core.price_sandbox import PriceSandbox, amount_to_upper, _normalize_upper

ITEMS_OK = [
    {'name': '日志审计', 'qty': 2, 'unit_price': 42500, 'total_price': 85000},
    {'name': '数据库审计', 'qty': 1, 'unit_price': 168000, 'total_price': 168000},
    {'name': '堡垒机', 'qty': 3, 'unit_price': 77500, 'total_price': 232500},
]                                                    # 合计 485500.00


def codes(findings):
    return [f['code'] for f in findings]


# ── 人民币大写转换边缘 ────────────────────────────────────────────────

def test_amount_to_upper_basic_and_edges():
    assert amount_to_upper('1286500.00') == '壹佰贰拾捌万陆仟伍佰元整'
    assert amount_to_upper(10) == '壹拾元整'
    assert amount_to_upper('1030.00') == '壹仟零叁拾元整'          # 节内零合并
    assert amount_to_upper('10005000.00') == '壹仟万零伍仟元整'    # 节首补零
    assert amount_to_upper('100000001.00') == '壹亿零壹元整'       # 跨节零
    assert amount_to_upper('0.05') == '伍分'
    assert amount_to_upper('0.50') == '伍角'
    assert amount_to_upper('1.53') == '壹元伍角叁分'
    assert amount_to_upper('1.50') == '壹元伍角'                   # 有角无分不加整
    assert amount_to_upper('1.05') == '壹元零伍分'
    assert amount_to_upper('0.00') == '零元整'
    assert amount_to_upper('20.00') == '贰拾元整'


# ── 核验点 1：分项算术 ────────────────────────────────────────────────

def test_item_arithmetic_off_by_cent():
    """验收线：差 1 分钱必须报警。"""
    items = [{'name': 'A', 'qty': 3, 'unit_price': '100.00', 'total_price': '300.01'}]
    findings = PriceSandbox(items=items).run()
    assert 'ITEM_ARITHMETIC' in codes(findings)
    f = next(f for f in findings if f['code'] == 'ITEM_ARITHMETIC')
    assert f['diff'] == '0.01'

    items_ok = [{'name': 'A', 'qty': 3, 'unit_price': '100.00', 'total_price': '300.00'}]
    assert PriceSandbox(items=items_ok).run() == []


def test_item_arithmetic_qty_times_price():
    """数量×单价误差（指令指定场景）。"""
    items = [{'name': '日志审计', 'qty': 2, 'unit_price': 42500, 'total_price': 85001}]
    findings = PriceSandbox(items=items).run()
    assert 'ITEM_ARITHMETIC' in codes(findings)


# ── 核验点 2：汇总一致性 ──────────────────────────────────────────────

def test_total_mismatch_one_cent():
    """∑分项 与 表合计 差 1 分 → 报警。"""
    findings = PriceSandbox(items=ITEMS_OK, table_total='485499.99',
                            bid_total='485499.99').run()
    assert 'TOTAL_MISMATCH' in codes(findings)


def test_bid_total_vs_table_mismatch():
    """分项表合计 与 开标一览表总价 不一致（跨表差 1 分）。"""
    findings = PriceSandbox(items=ITEMS_OK, table_total='485500.00',
                            bid_total='485500.01').run()
    assert 'BID_TOTAL_MISMATCH' in codes(findings)


def test_all_consistent_passes():
    findings = PriceSandbox(items=ITEMS_OK, table_total='485500.00',
                            bid_total='485500.00').run()
    assert findings == []


# ── 核验点 3：大小写严格一致 ──────────────────────────────────────────

def test_upper_missing_wan_char():
    """大写少一个'万'字（指令指定场景）→ 报警。"""
    findings = PriceSandbox(items=ITEMS_OK, bid_total='1286500.00',
                            bid_total_upper='壹佰贰拾捌陆仟伍佰元整').run()
    assert 'UPPER_MISMATCH' in codes(findings)


def test_upper_wrong_digit_one_char():
    """大写错 1 字（陆仟写成柒仟）→ 报警。"""
    findings = PriceSandbox(bid_total='1286500.00',
                            bid_total_upper='壹佰贰拾捌万柒仟伍佰元整').run()
    assert 'UPPER_MISMATCH' in codes(findings)


def test_upper_match_and_zheng_variant():
    """一致通过；'正/整'书写变体归一化后不算错。"""
    assert PriceSandbox(bid_total='1286500.00',
                        bid_total_upper='壹佰贰拾捌万陆仟伍佰元整').run() == []
    assert PriceSandbox(bid_total='1286500.00',
                        bid_total_upper='人民币壹佰贰拾捌万陆仟伍佰元正').run() == []
    assert _normalize_upper('（大写）壹拾元正 ') == '壹拾元整'


def test_upper_with_jiao_fen():
    """角分大写：1.53 → 壹元伍角叁分；错写'壹元伍角叁'少'分'字 → 报警。"""
    assert PriceSandbox(bid_total='1.53', bid_total_upper='壹元伍角叁分').run() == []
    findings = PriceSandbox(bid_total='1.53', bid_total_upper='壹元伍角叁').run()
    assert 'UPPER_MISMATCH' in codes(findings)


# ── 核验点 4：最高限价边缘 ────────────────────────────────────────────

def test_cap_edge_exact_and_over_by_cent():
    """恰等限价 → 通过；超 1 分 → 报警（验收边缘场景）。"""
    exact = PriceSandbox(bid_total='485500.00', price_cap='485500.00')
    assert exact.run() == []
    over = PriceSandbox(bid_total='485500.01', price_cap='485500.00')
    f = over.run()
    assert 'CAP_EXCEEDED' in codes(f)
    assert f[0]['diff'] == '0.01'


def test_cap_with_items_sum_when_no_bid_total():
    """无 bid_total 时用 ∑分项 与限价比。"""
    assert PriceSandbox(items=ITEMS_OK, price_cap='600000').run() == []
    assert 'CAP_EXCEEDED' in codes(PriceSandbox(items=ITEMS_OK, price_cap='400000').run())


# ── 综合场景：一份"千疮百孔"的报价表一次暴露全部硬伤 ─────────────────

def test_multi_findings_in_one_run():
    items = [
        {'name': 'A', 'qty': 2, 'unit_price': 100, 'total_price': 201},    # 算术错
        {'name': 'B', 'qty': 1, 'unit_price': 100, 'total_price': 100},
    ]
    findings = PriceSandbox(items=items, table_total='301.00',       # 累加301 一致
                            bid_total='300.00',                       # 与表合计差1
                            bid_total_upper='叁佰元整',                # 与300一致 → 不报大写
                            price_cap='299.99').run()                 # 超限价1分
    assert set(codes(findings)) == {'ITEM_ARITHMETIC', 'BID_TOTAL_MISMATCH', 'CAP_EXCEEDED'}
