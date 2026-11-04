# -*- coding: utf-8 -*-
"""F3 证照有效期核验与跨项目串名穿帮扫描（0 Token 确定性引擎）。

纯正则/文本扫描，毫秒级、零模型调用。四大防线：

1. CERT_EXPIRED / CERT_EXPIRING —— 证照有效期扫描：捕获"有效期至 X"及
   "有效期：A 至 B"区间（取端点 B），与开标日比较；已过期一票否决（FATAL），
   剩余不足 30 天预警（WARNING）。
2. SOCIAL_MONTH_MISSING —— 社保月份区间扫描：解析"所属期：2026年05月至
   2026年07月"等区间与单月表达，汇成实际覆盖月份集合；要求月份未全覆盖
   一票否决（FATAL）。
3. FORBIDDEN_NAME —— 跨项目串名排他扫描：多模式匹配历史项目/单位名，
   命中即报（含 ±30 字符上下文引用与偏移）。target_buyer 提供防呆：与
   本项目采购人名冲突的禁用词自动跳过（防止配置错误自伤）。
4. PLACEHOLDER —— 低级占位符穿帮扫描：XXX/TODO/[请填写]/[甲方单位名称]
   等模板残留。

与 F2 一致的返回契约：findings 列表，空列表 = 全部通过；每条
{'code', 'severity': 'error'|'warning', 'message', **detail}。
（卡内 FATAL 对应 severity='error'，WARNING 对应 'warning'。）
"""
from __future__ import annotations

import re
from datetime import date

__all__ = ['scan_qualifications', 'parse_cn_date', 'parse_month',
           'collect_covered_months', 'PLACEHOLDER_RE']

_DAYS_NEAR_WARN = 30
_CTX = 30                       # 串名命中上下文半径

# ── 日期解析 ────────────────────────────────────────────────────────────

_DATE_RE = re.compile(
    r'(\d{4})\s*[年\-\/\.]\s*(\d{1,2})\s*[月\-\/\.]\s*(\d{1,2})\s*日?')


def parse_cn_date(text: str):
    """解析中式日期写法（2027-05-31 / 2027.5.31 / 2027年5月31日 / 2027/5/31）。

    非法日期（2026-13-45 等）返回 None，绝不抛异常。
    """
    m = _DATE_RE.search(text or '')
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(y, mo, d)
    except ValueError:
        return None


# ── 防线 1：证照有效期 ──────────────────────────────────────────────────

# "有效期至2027年5月31日" / "有效期至：2027-05-31止" / "有效期2027.5.31"
_CERT_TO_RE = re.compile(r'有效期\s*[至到]?\s*[：:为]?\s*'
                         r'(\d{4}\s*[年\-\/\.]\s*\d{1,2}\s*[月\-\/\.]\s*\d{1,2}\s*日?)')
# 区间写法："有效期：2025.06.01-2026.06.01" / "有效期2025年6月1日至2026年6月1日"
_CERT_RANGE_RE = re.compile(
    r'有效期\s*[：:]?\s*'
    r'(\d{4}\s*[年\-\/\.]\s*\d{1,2}\s*[月\-\/\.]\s*\d{1,2}\s*日?)'
    r'\s*[至到~\-—–]+\s*'
    r'(\d{4}\s*[年\-\/\.]\s*\d{1,2}\s*[月\-\/\.]\s*\d{1,2}\s*日?)')


def _scan_expiry(text: str, open_d: date) -> list:
    findings = []
    seen_offsets = set()

    def add(end_text, match_start, match_end, label):
        d = parse_cn_date(end_text)
        if d is None:                      # 非法日期忽略不崩
            return
        # 区间止期与"有效期至"重复命中同一位置时去重
        for off in seen_offsets:
            if abs(off - match_start) < 12:
                return
        seen_offsets.add(match_start)
        remain = (d - open_d).days
        ctx_lo = max(0, match_start - _CTX)
        quote = text[ctx_lo:match_end].replace('\n', ' ')
        if remain < 0:
            findings.append({
                'code': 'CERT_EXPIRED', 'severity': 'error',
                'message': f'证照已过期：{label}有效期至 {d.isoformat()}，'
                           f'晚于开标日 {open_d.isoformat()} 已过期 {-remain} 天',
                'quote': quote, 'offset': match_start,
                'expire_date': d.isoformat(), 'days_overdue': -remain,
            })
        elif remain <= _DAYS_NEAR_WARN:
            findings.append({
                'code': 'CERT_EXPIRING', 'severity': 'warning',
                'message': f'证照临期预警：{label}有效期至 {d.isoformat()}，'
                           f'距开标日仅剩 {remain} 天（≤{_DAYS_NEAR_WARN} 天）',
                'quote': quote, 'offset': match_start,
                'expire_date': d.isoformat(), 'days_remaining': remain,
            })

    for m in _CERT_RANGE_RE.finditer(text):
        add(m.group(2), m.start(2), m.end(2), '证书区间止期')
    for m in _CERT_TO_RE.finditer(text):
        add(m.group(1), m.start(1), m.end(1), '证照')
    return findings


# ── 防线 2：社保月份区间 ────────────────────────────────────────────────

# 区间：2026年05月至2026年07月 / 2026.05-2026.07 / 2026年5月到7月（同年省略年份）
_MONTH_RANGE_RE = re.compile(
    r'(\d{4})\s*年?\s*[\-\/\.年]\s*(\d{1,2})\s*月?\s*'
    r'[至到~\-—–]+\s*'
    r'(?:(\d{4})\s*年?\s*[\-\/\.年]?\s*)?(\d{1,2})\s*月?')
# 单月：2026年08月 / 2026-08 / 202608
_MONTH_SINGLE_RE = re.compile(r'(?:(\d{4})\s*年?\s*[\-\/\.]?\s*(\d{1,2})\s*月?|\b(\d{4})(0[1-9]|1[0-2])\b)')


def parse_month(y: int, m: int) -> str:
    """月份规范表示 'YYYY-MM'。"""
    return f'{y:04d}-{m:02d}'


def collect_covered_months(text: str) -> set:
    """提取文本中出现的所有月份（区间展开为逐月集合）。非法月份忽略。"""
    covered = set()
    consumed = []                        # 区间跨度，防区间内单月重复展开（集合语义不受影响，留作审计）
    for m in _MONTH_RANGE_RE.finditer(text):
        y1, m1, m2 = int(m.group(1)), int(m.group(2)), int(m.group(4))
        y2 = int(m.group(3)) if m.group(3) else y1   # "2026年5月到7月"：止月沿用起始年
        try:
            d1, d2 = date(y1, m1, 1), date(y2, m2, 1)
        except ValueError:
            continue
        if d2 < d1:
            continue
        cur_y, cur_m = y1, m1
        while (cur_y, cur_m) <= (y2, m2):
            covered.add(parse_month(cur_y, cur_m))
            cur_m += 1
            if cur_m > 12:
                cur_y, cur_m = cur_y + 1, 1
        consumed.append((m.start(), m.end()))
    # 单月表达（跳过已落入区间跨度的部分）
    for m in _MONTH_SINGLE_RE.finditer(text):
        if any(s <= m.start() < e for s, e in consumed):
            continue
        if m.group(1):
            y, mo = int(m.group(1)), int(m.group(2))
        else:
            y, mo = int(m.group(3)), int(m.group(4))
        try:
            date(y, mo, 1)
        except ValueError:
            continue
        covered.add(parse_month(y, mo))
    return covered


def _scan_social(text: str, required: list) -> list:
    if not required:
        return []
    covered = collect_covered_months(text)
    missing = [mo for mo in required if mo not in covered]
    if not missing:
        return []
    return [{
        'code': 'SOCIAL_MONTH_MISSING', 'severity': 'error',
        'message': f'社保证明月份不全：要求 {len(required)} 个月'
                   f'（{",".join(sorted(required))}），标书仅覆盖要求中的 '
                   f'{len(covered & set(required))} 个月，缺失 '
                   f'{len(missing)} 个月：{",".join(sorted(missing))}',
        'required_months': sorted(required), 'missing_months': sorted(missing),
        'covered_months': sorted(covered),
        'quote': f'标书社保月份覆盖：{",".join(sorted(covered)) or "（未检出任何月份）"}',
    }]


# ── 防线 3：跨项目串名 ──────────────────────────────────────────────────

def _scan_forbidden(text: str, forbidden: list, target_buyer: str) -> list:
    findings = []
    for name in forbidden or []:
        name = (name or '').strip()
        if not name:
            continue
        # 防呆：禁用词与本项目采购人名重叠（配置把本项目名当成了禁用词）则跳过
        if target_buyer and (name in target_buyer or target_buyer in name):
            continue
        start = 0
        while True:
            idx = text.find(name, start)
            if idx < 0:
                break
            lo, hi = max(0, idx - _CTX), min(len(text), idx + len(name) + _CTX)
            findings.append({
                'code': 'FORBIDDEN_NAME', 'severity': 'error',
                'message': f'跨项目串名穿帮：正文残留外部名称"{name}"'
                           f'（评审现场高危事故点）',
                'quote': text[lo:hi].replace('\n', ' '), 'offset': idx,
                'forbidden_name': name,
            })
            start = idx + len(name)
    return findings


# ── 防线 4：低级占位符 ──────────────────────────────────────────────────

PLACEHOLDER_RE = re.compile(
    r'[X×x]{3}'                         # XXX / ××× / xxx
    r'|\bTODO\b'
    r'|\[[^\[\]]{0,20}请填写[^\[\]]{0,10}\]'
    r'|\[[^\[\]]{0,10}(甲方|单位名称|招标人|采购人)[^\[\]]{0,10}\]'
    r'|【[^【】]{0,20}请填写[^【】]{0,10}】',
    re.IGNORECASE)


def _scan_placeholder(text: str) -> list:
    findings = []
    seen = set()
    for m in PLACEHOLDER_RE.finditer(text):
        token = m.group(0)
        key = (token, m.start() // 20)   # 近邻同类去重（XXX 连排只报一次）
        if key in seen:
            continue
        seen.add(key)
        lo, hi = max(0, m.start() - _CTX), min(len(text), m.end() + _CTX)
        findings.append({
            'code': 'PLACEHOLDER', 'severity': 'warning',
            'message': f'模板占位符未清理：残留"{token}"（低级穿帮）',
            'quote': text[lo:hi].replace('\n', ' '), 'offset': m.start(),
            'placeholder': token,
        })
    return findings


# ── 主入口 ──────────────────────────────────────────────────────────────

def scan_qualifications(text: str,
                        open_date: str,
                        required_social_months: list = None,
                        forbidden_names: list = None,
                        target_buyer: str = '') -> list:
    """F3 四大防线主入口（0 Token）。

    Args:
        text: 投标文件全文（parser 展平文本）。
        open_date: 开标日期或有效期核验基准日，如 "2026-11-04"。
        required_social_months: 要求的社保月份，如 ["2026-08", "2026-09", "2026-10"]。
        forbidden_names: 绝对不该出现的历史名称列表。
        target_buyer: 本次真实招标方名称（可选，防禁用词配置自伤）。

    Returns:
        findings 列表（格式同 F2），空列表 = 全部通过。
    """
    open_d = parse_cn_date(open_date or '')
    if open_d is None:
        return [{'code': 'INVALID_OPEN_DATE', 'severity': 'error',
                 'message': f'开标日期无法解析：{open_date!r}（应为 2026-11-04 类格式）'}]
    text = text or ''
    findings = []
    findings += _scan_expiry(text, open_d)
    findings += _scan_social(text, required_social_months or [], )
    findings += _scan_forbidden(text, forbidden_names or [], target_buyer or '')
    findings += _scan_placeholder(text)
    return findings
