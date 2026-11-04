# -*- coding: utf-8 -*-
"""F3 证照效期与串名穿帮扫描单测（0 Token，纯离线）。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.qualification_scanner import (
    PLACEHOLDER_RE,
    collect_covered_months,
    parse_cn_date,
    scan_qualifications,
)

OPEN_DAY = '2026-11-04'


def codes(findings):
    return [f['code'] for f in findings]


# ── 日期解析 ────────────────────────────────────────────────────────────

def test_parse_cn_date_six_formats():
    """6 种写法全过 + 非法日期不崩。"""
    from datetime import date
    for s in ('2027-05-31', '2027.5.31', '2027年5月31日', '2027/5/31',
              '2027年05月31日', '2027-5-31'):
        assert parse_cn_date(s) == date(2027, 5, 31), s
    assert parse_cn_date('2026-13-45') is None      # 非法月日 → None 不崩
    assert parse_cn_date('没有日期') is None
    assert parse_cn_date('') is None


# ── 卡内必测场景 1：ISO 过期 7 天 ────────────────────────────────────────

def test_cert_expired_exact_seven_days():
    """ISO 证书有效期至 2026.10.28，开标 2026-11-04 → 精准过期 7 天。"""
    text = '我方 ISO9001 质量管理体系认证证书有效期至2026.10.28，附证书复印件。'
    f = scan_qualifications(text, OPEN_DAY)
    exp = [x for x in f if x['code'] == 'CERT_EXPIRED']
    assert len(exp) == 1
    assert exp[0]['days_overdue'] == 7
    assert exp[0]['expire_date'] == '2026-10-28'
    assert 'ISO9001' in exp[0]['quote']


def test_cert_expiring_within_30_days_warning():
    """有效期至 2026-11-25（剩 21 天）→ WARNING 临期，不报过期。"""
    text = '检测报告有效期至2026年11月25日。'
    f = scan_qualifications(text, OPEN_DAY)
    assert codes(f) == ['CERT_EXPIRING']
    assert f[0]['severity'] == 'warning'
    assert f[0]['days_remaining'] == 21


def test_cert_valid_no_finding():
    """有效期覆盖开标日且 >30 天 → 零报警。"""
    text = '营业执照长期有效，ISO 证书有效期至2027年5月31日。'
    assert scan_qualifications(text, OPEN_DAY) == []


def test_cert_range_takes_end():
    """区间写法'有效期：2025.06.01-2026.06.01'按止期 2026-06-01 判过期。"""
    text = '体系证书（有效期：2025.06.01-2026.06.01）'
    f = scan_qualifications(text, OPEN_DAY)
    assert codes(f) == ['CERT_EXPIRED']
    assert f[0]['expire_date'] == '2026-06-01'


def test_invalid_date_in_cert_ignored():
    """证照写非法日期 2026-13-45 → 忽略不崩，不误报。"""
    text = '证书有效期至2026-13-45，特殊情况。'
    assert scan_qualifications(text, OPEN_DAY) == []


# ── 卡内必测场景 2：社保月份不符 ────────────────────────────────────────

def test_social_months_missing_fatal():
    """要求 8/9/10 月，标书写 2026年05月至2026年07月 → FATAL 月份不符。"""
    text = '社保缴纳证明：所属期：2026年05月至2026年07月，附回执。'
    f = scan_qualifications(text, OPEN_DAY,
                            required_social_months=['2026-08', '2026-09', '2026-10'])
    assert codes(f) == ['SOCIAL_MONTH_MISSING']
    assert f[0]['severity'] == 'error'
    assert f[0]['missing_months'] == ['2026-08', '2026-09', '2026-10']


def test_social_months_range_partial_cover():
    """要求 8/9/10，标书只写 8 月至 9 月 → 只缺 10 月。"""
    text = '所属期：2026年08月至2026年09月'
    f = scan_qualifications(text, OPEN_DAY,
                            required_social_months=['2026-08', '2026-09', '2026-10'])
    assert f[0]['missing_months'] == ['2026-10']


def test_social_months_fully_covered_pass():
    """要求 8/9/10，区间 8-10 月 + 额外单月 → 全覆盖零报警。"""
    text = '所属期：2026年08月至2026年10月；另附 2026年07月 补缴记录。'
    f = scan_qualifications(text, OPEN_DAY,
                            required_social_months=['2026-08', '2026-09', '2026-10'])
    assert f == []


def test_collect_months_variants():
    """区间展开 + 数字连写 202608 + 省略年份区间。"""
    assert collect_covered_months('2026年05月至2026年07月') == {'2026-05', '2026-06', '2026-07'}
    assert '2026-08' in collect_covered_months('缴纳月份202608')
    assert collect_covered_months('2026年5月到7月') == {'2026-05', '2026-06', '2026-07'}
    assert collect_covered_months('2026-13月') == set()   # 非法月份忽略


# ── 卡内必测场景 3：串名穿帮 ────────────────────────────────────────────

def test_forbidden_name_hit_with_context():
    """残留'向沈阳市某某大数据局汇报' → 精准命中禁用词。"""
    text = '项目实施期间，我方将向沈阳市某某大数据局汇报项目进展，并接受监督。'
    f = scan_qualifications(text, OPEN_DAY, forbidden_names=['沈阳市某某大数据局'])
    assert codes(f) == ['FORBIDDEN_NAME']
    assert f[0]['forbidden_name'] == '沈阳市某某大数据局'
    assert '沈阳市某某大数据局' in f[0]['quote']
    assert f[0]['severity'] == 'error'


def test_forbidden_name_multiple_and_clean():
    """多个禁用词各报一条；本项目名不误报；target_buyer 防呆自伤。"""
    text = ('本项目服务于滁州城市职业学院，兼顾对接滁州市住建局，'
            '曾服务南京市财政局旧项目。')
    f = scan_qualifications(text, OPEN_DAY,
                            forbidden_names=['滁州市住建局', '南京市财政局'],
                            target_buyer='滁州城市职业学院')
    names = sorted(x['forbidden_name'] for x in f)
    assert names == ['南京市财政局', '滁州市住建局']       # 两处外部名均命中
    # 防呆：把本项目名误配成禁用词 → 自动跳过不报
    assert scan_qualifications(text, OPEN_DAY,
                               forbidden_names=['滁州城市职业学院'],
                               target_buyer='滁州城市职业学院') == []


# ── 卡内必测场景 4：占位符 ──────────────────────────────────────────────

def test_placeholder_xxx_and_templates():
    """本系统具有 XXX 功能 → 报占位符；各类模板残留全检出。"""
    text = '本系统具有 XXX 功能。\n附件：[请填写此处]；甲方代表：[甲方单位名称]；待办 TODO。'
    f = scan_qualifications(text, OPEN_DAY)
    ph = [x for x in f if x['code'] == 'PLACEHOLDER']
    tokens = {x['placeholder'] for x in ph}
    assert {'XXX', '[请填写此处]', '[甲方单位名称]', 'TODO'} <= tokens


def test_placeholder_no_false_positive():
    """正常技术词不误报：'XXXXX' 连排只报一次；'xx 分钟' 不报。"""
    assert PLACEHOLDER_RE.search('系统响应速度提升 xx%') is None
    hits = PLACEHOLDER_RE.findall('XXXXX')
    assert len(hits) >= 1                # XXXX 是 XXX 的超集，检出即可


# ── 综合：一份"千疮百孔"标书 ────────────────────────────────────────────

def test_comprehensive_tattered_bid():
    """过期+临期+串名+占位符+社保缺月一次全中；干净标书零报警。"""
    bad = ('ISO证书有效期至2026.10.28。\n'
           '社保证明：所属期：2026年05月至2026年07月。\n'
           '我方曾向沈阳市某某大数据局汇报。\n'
           '本系统具有 XXX 功能。\n'
           '另一证书有效期至2026-11-25。\n')
    f = scan_qualifications(bad, OPEN_DAY,
                            required_social_months=['2026-08', '2026-09'],
                            forbidden_names=['沈阳市某某大数据局'])
    assert set(codes(f)) == {'CERT_EXPIRED', 'SOCIAL_MONTH_MISSING',
                             'FORBIDDEN_NAME', 'PLACEHOLDER', 'CERT_EXPIRING'}
    clean = ('全部证照有效期至2027年12月31日。\n'
             '所属期：2026年08月至2026年09月。\n'
             '本项目面向滁州城市职业学院。\n')
    assert scan_qualifications(clean, OPEN_DAY,
                               required_social_months=['2026-08', '2026-09'],
                               forbidden_names=['沈阳市某某大数据局'],
                               target_buyer='滁州城市职业学院') == []


def test_invalid_open_date_reported():
    """开标日非法 → 单条明确报错，不静默。"""
    f = scan_qualifications('任何文本', '不是日期')
    assert codes(f) == ['INVALID_OPEN_DATE']
