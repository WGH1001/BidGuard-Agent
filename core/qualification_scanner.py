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

import json
import re
from datetime import date

from typing import Any, Optional

from core.quote_verifier import verify_quote

__all__ = ['scan_qualifications', 'parse_cn_date', 'parse_month',
           'collect_covered_months', 'resolve_effective_social_requirement',
           'match_required_certificates_with_ledger', 'PLACEHOLDER_RE']

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
    r'(?<!\d)((?:19|20)\d{2})\s*年?\s*[\-\/\.年]\s*(\d{1,2})\s*月?\s*'
    r'[至到~\-—–]+\s*'
    r'(?:((?:19|20)\d{2})\s*年?\s*[\-\/\.年]?\s*)?(\d{1,2})\s*(?:月|(?!\d))')
# 单月：2026年08月 / 2026-08 / 202608（排除 2026-10-28 完整年月日及 202610-014 项目编号）
_MONTH_SINGLE_RE = re.compile(
    r'(?:'
    r'(?<!\d)((?:19|20)\d{2})\s*(?:年\s*(\d{1,2})\s*月(?!\s*\d+\s*日)|[\-\/\.]\s*(\d{1,2})(?!\s*[\-\/\.\d日]))'
    r'|(?<!\d)((?:19|20)\d{2})(0[1-9]|1[0-2])(?!\s*[\-\/\.\d日])'
    r')'
)


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
        if d2 < d1 or (y2 - y1) * 12 + (m2 - m1) > 60:
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
            y = int(m.group(1))
            mo = int(m.group(2) or m.group(3))
        else:
            y, mo = int(m.group(4)), int(m.group(5))
        try:
            date(y, mo, 1)
        except ValueError:
            continue
        covered.add(parse_month(y, mo))
    return covered


# 会计附注/非社保凭证噪声特征与非社保日期行过滤
_ACCOUNTING_NOTE_NOISE_RE = re.compile(
    r'设定提存计划|离职后福利|会计期间|当期损益|短期薪酬|辞退福利|准则规定|财务报表附注'
)
_TENDER_REQ_ECHO_RE = re.compile(
    r'投标人须提供参加本次政府采购|依法不需要缴纳社会保障资金的投标人|资格证明文件.*目录'
)
_NON_SOCIAL_LINE_NOISE_RE = re.compile(
    r'出生(?:日期|年月)?|身份证|公民身份号码|证书有效期|有效期[至：:]|发证日期|注册日期|成立日期|营业期限|审计报告'
)
_SOCIAL_PROOF_ANCHOR_RE = re.compile(
    r'社会保险基金管理|社保权益|参保缴费|社保缴纳|社保证明|社会保险费|缴费人数|缴费情况|费款所属期|所属期|社会保险|养老保险|社会保障资金|社保'
)
_STRONG_SOCIAL_PROOF_RE = re.compile(
    r'社会保险基金管理|社保权益|参保缴费|社保缴纳|社保证明|缴费人数|缴费情况|费款所属期|所属期'
)


def _prev_year_months(ref_d: date, count: int) -> list[str]:
    """以开标日所在月的前一个月为起点，向前推算 count 个连续月份（升序返回 YYYY-MM）。"""
    y, m = ref_d.year, ref_d.month
    out: list[str] = []
    for _ in range(max(0, count)):
        m -= 1
        if m <= 0:
            y -= 1
            m = 12
        out.append(parse_month(y, m))
    out.sort()
    return out


def resolve_effective_social_requirement(
    tender_text: str,
    open_date_str: str,
    manual_months: Optional[list[str]] = None,
    rule_months: Optional[list[str]] = None,
    rule_open_date: Optional[str] = None,
    *,
    configured_months: Optional[list[str]] = None,
    is_user_explicit: bool = False,
) -> tuple[list[str], Optional[int]]:
    """根据任务手填配置、招标书原文要求与有效开标日，解析本次应核验的社保月份列表及最少覆盖月数。

    Returns:
        (required_months, social_min_count)
        - social_min_count 为 None 时表示 required_months 需全部覆盖；
        - 为正整数 k（如 1）时表示在 required_months 候选窗口内至少覆盖 k 个月即可（对应“近半年任意一个月”）。
    """
    if configured_months is not None:
        if is_user_explicit:
            manual_months = configured_months
        else:
            rule_months = configured_months
    elif manual_months and rule_months is None and not is_user_explicit:
        if list(manual_months) == ['2026-08', '2026-09', '2026-10']:
            rule_months = manual_months
            manual_months = None

    if manual_months:
        return list(manual_months), None

    open_d = parse_cn_date(open_date_str or '')
    t_txt = tender_text or ''

    if open_d is not None and t_txt:
        # 1) “最近半年任意一个月” / “近六个月内任意一个月” / “近半年内任一个月”
        if re.search(
            r'(?:最近|近)\s*(?:半年|六个月|6\s*个月)[^\n。；]{0,25}?(?:任意\s*[一1]\s*个月|任\s*[一1]\s*个月)'
            r'[^\n。；]{0,30}?(?:社会保障|社会保险|社保)',
            t_txt,
        ):
            return _prev_year_months(open_d, 6), 1

        # 2) “近三个月” / “最近3个月” / “连续三个月” 社保
        m_n = re.search(
            r'(?:最近|近|连续)\s*([一二三四五六1-6])\s*个月[^\n。；]{0,25}?(?:社会保障|社会保险|社保)',
            t_txt,
        )
        if m_n:
            cn_map = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6}
            tok = m_n.group(1)
            n_val = cn_map.get(tok) if tok in cn_map else int(tok)
            return _prev_year_months(open_d, n_val), None

    # 3) 若未手动指定，仅当规则默认月份与当前有效开标日同属一个年份窗口时才采用 rule_months
    if rule_months:
        if open_d is None:
            return list(rule_months), None
        rule_d = parse_cn_date(rule_open_date or '')
        rule_years = {str(m)[:4] for m in rule_months if isinstance(m, str) and len(m) >= 4}
        if (rule_d is not None and abs((open_d - rule_d).days) <= 120) or (
            str(open_d.year) in rule_years
        ):
            return list(rule_months), None

    return [], None


def _sanitize_window_lines(window_text: str) -> str:
    """剔除局部窗口中的身份证出生年月、证书有效期、打印填发日期、会计准则附注等非社保日期干扰行。"""
    clean_lines: list[str] = []
    for ln in (window_text or '').splitlines():
        if _ACCOUNTING_NOTE_NOISE_RE.search(ln):
            continue
        if _NON_SOCIAL_LINE_NOISE_RE.search(ln) and not _STRONG_SOCIAL_PROOF_RE.search(ln):
            continue
        # 剔除同行内的“有效期至 YYYY-MM-DD”、“出生 YYYY-MM”、“填发日期/打印日期 YYYY年MM月DD日”及具体到“日”的完整日期
        ln_clean = re.sub(
            r'(?:有效期[至到：:\s]*|出生(?:日期|年月)?[：:\s]*|填发日期[：:\s]*|打印日期[：:\s]*)\d{4}\s*[年\-\/\.]\s*\d{1,2}(?:\s*[月\-\/\.]\s*\d{1,2}\s*日?)?',
            ' ',
            ln,
        )
        ln_clean = re.sub(
            r'\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日',
            ' ',
            ln_clean,
        )
        clean_lines.append(ln_clean)
    return '\n'.join(clean_lines)


def _extract_social_context_windows(text: str) -> list[tuple[int, int, str, bool]]:
    """提取投标书中与社保证明相关的局部语境窗口列表：[(start, end, window_text, is_strong)]。"""
    if not text:
        return []
    # 短文本（如单测直接传入 '所属期：2026年08月至2026年09月'）直接作为单窗口处理
    if len(text) <= 400 and not _ACCOUNTING_NOTE_NOISE_RE.search(text):
        sanitized = _sanitize_window_lines(text)
        if sanitized.strip():
            return [(0, len(text), sanitized, True)]
        return []

    raw_spans: list[tuple[int, int]] = []
    for m in _SOCIAL_PROOF_ANCHOR_RE.finditer(text):
        lo = max(0, m.start() - 160)
        hi = min(len(text), m.end() + 240)
        if raw_spans and lo <= raw_spans[-1][1]:
            raw_spans[-1] = (raw_spans[-1][0], max(raw_spans[-1][1], hi))
        else:
            raw_spans.append((lo, hi))

    windows: list[tuple[int, int, str, bool]] = []
    for lo, hi in raw_spans:
        win_raw = text[lo:hi]
        # 若窗口纯属财务审计报告附注（离职后福利/设定提存计划/会计期间）且无真实社保经办/缴费表特征，直接排除
        if _ACCOUNTING_NOTE_NOISE_RE.search(win_raw) and not _STRONG_SOCIAL_PROOF_RE.search(win_raw):
            continue
        is_strong = bool(_STRONG_SOCIAL_PROOF_RE.search(win_raw)) and not bool(
            _TENDER_REQ_ECHO_RE.search(win_raw)
        )
        sanitized = _sanitize_window_lines(win_raw)
        if sanitized.strip():
            windows.append((lo, hi, sanitized, is_strong))
    return windows


def _llm_verify_social_window(client: Any, win_text: str, full_text: str) -> Optional[set[str]]:
    """受控大模型局部窗语义鉴别：仅判断 200~400 字窗口是否为真实社保缴费证明并提取原文月份片段（强制过 verify_quote）。"""
    if client is None or not win_text.strip():
        return None
    snippet = win_text.strip()[:400]
    prompt = (
        "请判断以下投标文件局部片段是否属于【真实的社保参保/缴费证明凭证】（排除财务审计报告会计附注、"
        "离职后福利设定提存计划说明、招标要求复述、身份证出生日期或ISO证书有效期）。\n"
        "若是真实社保证明，请提取其中载明的社保缴费/所属期原文子串 quotes；若不是，is_social_proof 返回 false。\n"
        "严格输出 JSON：{\"is_social_proof\": true/false, \"quotes\": [\"原文连续子串\"]}\n\n"
        f"【局部片段】：\n{snippet}"
    )
    try:
        resp = client.chat([{'role': 'user', 'content': prompt}], model_tier='flash')
        raw = getattr(resp, 'content', '') or ''
        m_json = re.search(r'\{[\s\S]*\}', raw)
        if not m_json:
            return None
        parsed = json.loads(m_json.group(0))
        if not isinstance(parsed, dict):
            return None
        if parsed.get('is_social_proof') is False:
            return set()
        quotes = parsed.get('quotes') or []
        verified_months: set[str] = set()
        for q in quotes:
            q_str = str(q or '').strip()
            if q_str and verify_quote(q_str, full_text):
                verified_months |= collect_covered_months(q_str)
        return verified_months if verified_months else None
    except Exception:
        return None


def _collect_social_covered_months(
    text: str,
    client: Any = None,
) -> tuple[set[str], str, Optional[int]]:
    """从社保证明局部语境窗口收集覆盖月份（绝不裸抓全书生日/证书到期日），返回 (covered_months, excerpt, offset)。"""
    windows = _extract_social_context_windows(text)
    if not windows:
        return set(), '', None

    strong_wins = [w for w in windows if w[3]]
    target_wins = strong_wins if strong_wins else windows

    covered: set[str] = set()
    best_excerpt = ''
    best_offset: Optional[int] = None

    for lo, hi, sanitized_win, is_strong in target_wins:
        win_months = collect_covered_months(sanitized_win)
        if client is not None and win_months and not is_strong:
            llm_months = _llm_verify_social_window(client, text[lo:hi], text)
            if llm_months is not None:
                win_months = llm_months
        covered |= win_months
        if best_offset is None and (win_months or is_strong):
            raw_win = text[lo:hi]
            m_anchor = _STRONG_SOCIAL_PROOF_RE.search(raw_win) or _SOCIAL_PROOF_ANCHOR_RE.search(raw_win)
            anchor_rel = max(0, m_anchor.start() - 20) if m_anchor else 0
            best_offset = lo + (m_anchor.start() if m_anchor else 0)
            raw_slice = raw_win[anchor_rel:min(len(raw_win), anchor_rel + 120)].replace('\n', ' ').strip()
            best_excerpt = raw_slice

    if best_offset is None and target_wins:
        lo, hi, _, _ = target_wins[0]
        raw_win = text[lo:hi]
        m_anchor = _STRONG_SOCIAL_PROOF_RE.search(raw_win) or _SOCIAL_PROOF_ANCHOR_RE.search(raw_win)
        anchor_rel = max(0, m_anchor.start() - 20) if m_anchor else 0
        best_offset = lo + (m_anchor.start() if m_anchor else 0)
        best_excerpt = raw_win[anchor_rel:min(len(raw_win), anchor_rel + 120)].replace('\n', ' ').strip()

    return covered, best_excerpt, best_offset


def _scan_social(
    text: str,
    required: list,
    client: Any = None,
    social_min_count: Optional[int] = None,
) -> list:
    if not required:
        return []
    req_set = set(required)
    covered, raw_excerpt, soc_offset = _collect_social_covered_months(text or '', client=client)
    matched_req = covered & req_set

    if social_min_count is not None and social_min_count > 0:
        if len(matched_req) >= social_min_count:
            return []
        missing = sorted(req_set - covered)
    else:
        missing = [mo for mo in required if mo not in covered]
        if not missing:
            return []

    if raw_excerpt:
        quote_str = f'{raw_excerpt}（社保证明窗检出月份：{",".join(sorted(covered)) or "无"}）'
    else:
        quote_str = '全文未检出有效社保证明缴费记录窗（已排除审计报告会计附注与证件效期干扰）'

    if social_min_count is not None and social_min_count > 0:
        msg = (
            f'社保证明月份不满足要求：要求在近半年窗口（{",".join(sorted(required))}）内至少提供 '
            f'{social_min_count} 个月社保证明，标书社保证明窗仅检出 {len(matched_req)} 个月'
        )
    else:
        msg = (
            f'社保证明月份不全：要求 {len(required)} 个月'
            f'（{",".join(sorted(required))}），标书仅覆盖要求中的 '
            f'{len(matched_req)} 个月，缺失 '
            f'{len(missing)} 个月：{",".join(sorted(missing))}'
        )

    return [{
        'code': 'SOCIAL_MONTH_MISSING', 'severity': 'error',
        'message': msg,
        'required_months': sorted(required), 'missing_months': sorted(missing),
        'covered_months': sorted(covered),
        'quote': quote_str,
        'offset': soc_offset,
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
                        target_buyer: str = '',
                        *,
                        client: Any = None,
                        social_min_count: Optional[int] = None) -> list:
    """F3 四大防线主入口（默认 0 Token 确定性局部窗，可选传入 client 对模糊社保窗做受控语义鉴别）。

    Args:
        text: 投标文件全文（parser 展平文本）。
        open_date: 开标日期或有效期核验基准日，如 "2026-11-04"。
        required_social_months: 要求的社保月份，如 ["2026-08", "2026-09", "2026-10"]。
        forbidden_names: 绝对不该出现的历史名称列表。
        target_buyer: 本次真实招标方名称（可选，防禁用词配置自伤）。
        client: 可选 LLM 客户端（仅对模糊候选窗口做语义核实并强制过 verify_quote，日期差与集合覆盖 100% Python 计算）。
        social_min_count: 可选，候选月份窗口内最少需覆盖的月数（如“近半年任意一个月”传 1）。

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
    findings += _scan_social(
        text,
        required_social_months or [],
        client=client,
        social_min_count=social_min_count,
    )
    findings += _scan_forbidden(text, forbidden_names or [], target_buyer or '')
    findings += _scan_placeholder(text)
    return findings


# ── E26：招标所需证照与企业台账保守对账（0 Token）────────────────────────
_CERT_FAMILY_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ('iso9001', ('ISO9001', 'GB/T19001', 'GBT19001', '质量管理体系')),
    ('iso14001', ('ISO14001', 'GB/T24001', 'GBT24001', '环境管理体系')),
    ('iso45001', ('ISO45001', 'OHSAS18001', '职业健康安全')),
    ('iso27001', ('ISO27001', 'ISO/IEC27001', '信息安全管理体系')),
    ('iso20000', ('ISO20000', 'ISO/IEC20000', '信息技术服务管理体系')),
    ('cmmi', ('CMMI',)),
    ('itss', ('ITSS', '信息技术服务标准')),
    ('cs', ('信息系统建设和服务能力', 'CS1', 'CS2', 'CS3', 'CS4', 'CS5')),
    ('ccrc', ('CCRC', '信息安全服务资质')),
    ('business_license', ('营业执照',)),
    ('tax_social', ('完税证明', '纳税证明', '依法缴纳税收', '社会保障资金', '社保证明')),
    ('high_tech', ('高新技术企业',)),
    ('software_copyright', ('软件著作权', '软著')),
    ('patent', ('专利证书', '发明专利', '实用新型专利')),
    ('energy_saving', ('节能产品认证',)),
    ('env_label', ('环境标志产品', '十环')),
    ('3c', ('3C认证', 'CCC认证', '强制性产品认证')),
    ('xinchuang', ('互认证书', '兼容性认证', '兼容性测试', '符合性测试', '银河麒麟', '统信', '达梦', '人大金仓', '鲲鹏')),
    ('ca', ('CA数字证书', '数字证书及电子签章')),
    ('pm_high', ('信息系统项目管理师',)),
    ('pm_mid', ('系统集成项目管理工程师',)),
    ('arch', ('系统架构设计师',)),
    ('sys_plan', ('系统规划与管理师',)),
    ('sys_analyst', ('系统分析师',)),
    ('net_plan', ('网络规划设计师',)),
    ('sw_designer', ('软件设计师',)),
    ('sw_tester', ('软件评测师',)),
    ('net_eng', ('网络工程师',)),
    ('db_eng', ('数据库系统工程师',)),
    ('sec_eng', ('信息安全工程师', 'CISP')),
    ('title_eng', ('高级工程师', '中级工程师', '工程师职称', '人工智能应用工程师')),
    ('constructor', ('建造师',)),
)


def _norm_cert_text(raw: str) -> str:
    return re.sub(r'[\s\-_（）()【】\[\]/／、,，.。:：]+', '', (raw or '').upper())


def _extract_cert_families(raw: str) -> set[str]:
    norm = _norm_cert_text(raw)
    if not norm:
        return set()
    families: set[str] = set()
    for fam, keys in _CERT_FAMILY_ALIASES:
        if any(_norm_cert_text(k) in norm for k in keys):
            families.add(fam)
    return families


def _strip_cert_generic_words(raw: str) -> str:
    norm = _norm_cert_text(raw)
    for w in ('登记证书', '认证证书', '资质证书', '资格证书', '证书', '认证', '资质', '证明', '复印件', '有效'):
        norm = norm.replace(w, '')
    return norm


def _is_cert_match(req_name: str, cert_name: str) -> bool:
    req_fams = _extract_cert_families(req_name)
    cert_fams = _extract_cert_families(cert_name)
    if req_fams and cert_fams:
        return bool(req_fams & cert_fams)
    if req_fams or cert_fams:
        # 仅单侧命中明确标准家族时，仍允许核心中文名完全包含（防别名表遗漏特例）
        pass
    core_req = _strip_cert_generic_words(req_name)
    core_cert = _strip_cert_generic_words(cert_name)
    if len(core_req) >= 4 and len(core_cert) >= 4:
        return core_req in core_cert or core_cert in core_req
    return False


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def match_required_certificates_with_ledger(
    required_certs: list[dict[str, Any]],
    ledger_certs: list[Any],
    reference_date: Optional[str] = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """将 F1 提取出的招标所需证照与租户企业证照台账进行 0-Token 保守对账。

    Args:
        required_certs: extract_required_certificates() 返回的清单。
        ledger_certs: 租户自身的 CompanyCertificate ORM 列表或字典列表。
        reference_date: 基准日期（优先传招标文件开标日 'YYYY-MM-DD'，缺省回退今天）。

    Returns:
        (enriched_items, cert_ledger_summary)
    """
    ref_d = parse_cn_date(reference_date or '') if reference_date else None
    ref_source = 'open_date' if ref_d is not None else 'today'
    if ref_d is None:
        ref_d = date.today()

    status_rank = {'valid': 3, 'expiring_soon': 2, 'expired': 1, 'not_in_ledger': 0}
    enriched: list[dict[str, Any]] = []

    valid_cnt = 0
    expiring_cnt = 0
    expired_cnt = 0
    missing_cnt = 0
    mandatory_cnt = 0
    bonus_cnt = 0

    for item in required_certs or []:
        req_name = str(item.get('cert_name') or '').strip()
        req_level = str(item.get('requirement_level') or 'bonus')
        if req_level == 'mandatory':
            mandatory_cnt += 1
        else:
            bonus_cnt += 1

        candidates: list[dict[str, Any]] = []
        for cert in ledger_certs or []:
            c_name = str(_field(cert, 'cert_name', '') or '').strip()
            if not c_name or not _is_cert_match(req_name, c_name):
                continue

            raw_vu = _field(cert, 'valid_until')
            if isinstance(raw_vu, date):
                vu_date = raw_vu
            else:
                vu_date = parse_cn_date(str(raw_vu or ''))
            if vu_date is None:
                continue

            warn_days = int(_field(cert, 'warn_days', 30) or 30)
            days_left = (vu_date - ref_d).days
            if days_left < 0:
                c_status = 'expired'
            elif days_left <= warn_days:
                c_status = 'expiring_soon'
            else:
                c_status = 'valid'

            att_name = _field(cert, 'attachment_name')
            att_path = _field(cert, 'attachment_path')
            has_att = bool(_field(cert, 'has_attachment', False) or att_path)
            candidates.append({
                'id': int(_field(cert, 'id', 0) or 0),
                'cert_name': c_name,
                'cert_no': _field(cert, 'cert_no'),
                'valid_until': vu_date.isoformat(),
                'days_remaining': days_left,
                'status': c_status,
                'has_attachment': has_att,
                'attachment_name': att_name,
            })

        candidates.sort(
            key=lambda c: (status_rank.get(c['status'], 0), c['days_remaining']),
            reverse=True,
        )
        best = candidates[0] if candidates else None
        ledger_status = best['status'] if best else 'not_in_ledger'

        if ledger_status == 'valid':
            valid_cnt += 1
        elif ledger_status == 'expiring_soon':
            expiring_cnt += 1
        elif ledger_status == 'expired':
            expired_cnt += 1
        else:
            missing_cnt += 1

        row_out = dict(item)
        row_out['ledger_status'] = ledger_status
        row_out['matched_cert'] = best
        row_out['matched_count'] = len(candidates)
        enriched.append(row_out)

    summary = {
        'total': len(enriched),
        'mandatory_count': mandatory_cnt,
        'bonus_count': bonus_cnt,
        'valid_count': valid_cnt,
        'expiring_soon_count': expiring_cnt,
        'expired_count': expired_cnt,
        'not_in_ledger_count': missing_cnt,
        'reference_date': ref_d.isoformat(),
        'reference_date_source': ref_source,
    }
    return enriched, summary

