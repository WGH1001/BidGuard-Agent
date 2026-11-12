# -*- coding: utf-8 -*-
"""F1 招标文件废标红线与★/▲条款提取器 —— 确定性层（0 Token，纯规则）。

模块定位（PRD-02 §7 F1 第一层）：
    在 LLM 分段抽取之前，用确定性规则把整本招标文件中的强制条款"圈出来"，
    产出结构化候选清单。三层职责：
    ① ★/▲ 标记条款提取（标记型语料，含 E3 碎片过滤与 PDF 印刷页码映射）；
    ② 无标记措辞型强制条款提取（全文无任何 ★/▲ 标记，模式 A / 模式 B）；
    ③ 章节与审查区定位（资格审查 / 符合性审查 / 评标办法·评分细则表），供
       LLM 分段层按区域投喂、防止整本直塞导致注意力衰减（PRD R-01）。

实现规格（签名不得修改）：
    load_document(file_path) -> ParsedDoc
    locate_chapters(text) -> list[dict]              # 第X章标题与偏移
    extract_marked_clauses(text) -> list[dict]       # ★/▲ 标记条款
    extract_invalid_bid_clauses(text) -> list[dict]  # 无标记措辞型（模式A/B）
    locate_keyword_sections(text, keywords) -> list[dict]  # 审查区/评分区定位
    build_review_checklist(file_path) -> dict        # 四类候选汇总（一次成稿）
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
import re

from core.interfaces import ParsedDoc
from core.parser import build_pdf_page_spans, offset_to_page, parse_document

# ★/▲ 标记（语料实证：#4 南京病理 ★19/▲10、#9 芜湖 ★6/▲3、#7 连云港 ▲62、#11 滁州托育 ▲26）
_MARK_RE = re.compile(r'[★▲]')

# 条款编号头（向前吸附用）：（十四）/ (14) / 2.1 / 第X条 / 一、 等
_CLAUSE_HEADER_RE = re.compile(
    r'（[一二三四五六七八九十百零两]+）'
    r'|\([一二三四五六七八九十百零两]+\)'
    r'|（\d{1,3}）'
    r'|\(\d{1,3}\)'
    r'|第[一二三四五六七八九十百千零两]+条'
    r'|[一二三四五六七八九十百零两]+、'
    r'|\d{1,2}\.\d{1,2}'
)

# 章标题：第一章 投标邀请 / 第五章 评标方法与评标标准
_CHAPTER_RE = re.compile(r'第[一二三四五六七八九十百零两]+章\s*[^\n第]{0,40}')

# 模式 A：实质性要求模板句（徐州份核验：句式固定、自带承诺函名称）
_PATTERN_A_RE = re.compile(r'不允许偏离的实质性要求')
_PROMISE_DOC_RE = re.compile(r'《([^》]{2,30})》')

# 模式 B：无效投标触发词（语料实证变体，要求带"处理"落点防误伤泛论）
_PATTERN_B_RE = re.compile(
    r'无效投标处理|投标无效处理|作无效投标|视为无效投标|按无效投标|无效响应处理'
)

# 标记使用说明语（元说明，非采购要求本身，F1 输出要求清单时应排除）
_USAGE_NOTE_RE = re.compile(
    r'品目清单'
    r'|根据采购需求.{0,8}[“"]?[▲★][”"]?功能要求'
    r'|打[★▲]指标'                      # 评分说明：打★指标为必须满足项 / 打▲指标为重要指标
    r'|标有\s*[“"]?\s*[★▲]\s*[”"]?\s*的'  # 实质性说明：标有"★"的参数为实质性参数（亳州实证含空格+引号）
    r'|[★▲]为(政府)?强制采购'           # 节能说明：▲为政府强制采购节能产品
    r'|标注\s*[“"]?\s*[▲★]\s*[”"]?'     # 标的说明：标注"▲"的标的为主要标的（芜湖 2026-11-04 终审新增）
    r'|带\s*[★▲]\s*的项'                # 划型说明：带★的项为行业（芜湖中小企业划型附表）
    # —— 2026-11-04 LLM 分歧人工核定新增（第三轮收口，均为全国通用政采模板）——
    r'|打\s*[“"]?\s*[★▲]\s*[”"]?\s*内容'      # 偏离表填写说明：实质性要求（打"★"内容）作出明确响应
    r'|[“"]?★[”"]?内容为实质性要求'           # 符合性说明：本文件中的"★"内容为实质性要求
    r'|每有一项不满足扣'                       # 扣分规则：标"▲"项每有一项不满足扣0.5分
    r'|项加\d+分'                              # 加分规则：每1个"▲"项加2分
    r'|未标\s*[“"]?\s*[★▲]\s*[”"]?\s*的'      # 一般参数说明：未标"▲"的为一般参数
)

# 表格行标记碎片（kind=table_marker，2026-11-04 用户终审芜湖实测定案）：
# 中小企业划型附表的行业名"工业★/仓储业★"——标记在行尾。
_TABLE_TRAILING_RE = re.compile(r'[★▲]\s*$')

# E3 §2.3 碎片过滤：剥离 ★/▲ 符号与空白后，仅剩 1~2 个汉字且无编号/无谓语/无数值单位的孤立残词（如 "▲值回"）
# 注意：绝不误伤滁州物业 3 字真功能项（"▲残值回"、"▲资金使"）与南京病理带编号短要求（"★（1）常规制片"）
# '值回'（滁州'▲残值回充'断行截断）入黑名单；'中共无'（芜湖）不在列——
# M0 人工终审口径（tests/test_tender_extractor.py 锁定）：其与真功能项语法同构，
# 提取器不硬判，保持 requirement 留人工定稿，过滤它即违反零回归底线。
_FRAGMENT_WORD_BLACKLIST = frozenset({'值回'})


def _is_meaningless_short_fragment(merged_text: str) -> bool:
    """E3 §2.3：判断是否为单字/双字无动宾语义的表格断行残词（如 '▲值回'）。
    若命中则降级为 usage_note（不进 requirement 清单，同时保持 ★/▲ 符号总数守恒）。
    """
    stripped = re.sub(r'[★▲\s]+', '', merged_text or '')
    if not stripped:
        return True
    if stripped in _FRAGMENT_WORD_BLACKLIST:
        return True
    # 若含编号、括号、数字、标点，说明是带结构短要求（如 "（1）常规制片"），绝不判定为碎片
    if re.search(r'[0-9（()）、.．：:，,。；;≥≤%]', stripped):
        return False
    # 剥离标记后仅 1~2 个纯汉字的孤立残片（如 "值回"），无完整动宾或技术指标语义
    if len(stripped) <= 2:
        return True
    return False


def _classify_kind(merged_text: str) -> str:
    """标记条款三分类：usage_note（标记使用元说明或孤立短残词）/ table_marker（附表行
    尾标记碎片）/ requirement（投标人须响应的实质要求）。"""
    if _USAGE_NOTE_RE.search(merged_text):
        return 'usage_note'
    first_line = merged_text.split('\n', 1)[0].strip()
    if _TABLE_TRAILING_RE.search(first_line):
        return 'table_marker'
    if _is_meaningless_short_fragment(merged_text):
        return 'usage_note'
    return 'requirement'

# 章节语义关键词（定位 评标办法 / 资格审查 / 符合性审查 / 评分细则）
_EVAL_CHAPTER_KEYS = ('评标办法', '评标方法', '评标标准', '评分细则')
_QUALIFY_KEYS = ('资格性审查', '资格审查', '申请人的资格要求', '供应商资格要求')
_COMPLIANCE_KEYS = ('符合性审查', '实质性要求审查')

# ── E23 §2.1：招标文件开标/投标截止日期 0-Token 确定性提取（通用结构规则 + 防误抓护栏）──
_OPEN_DATE_ANCHOR_RE = re.compile(
    r'(?:提交投标文件截止时间[、，,\s]*开标时间(?:和地点|及地点)?'
    r'|递交投标文件截止时间[、，,\s]*开标时间(?:和地点|及地点)?'
    r'|开\s*标\s*(?:及|与)\s*截\s*标\s*时\s*间(?:和地点|及地点)?'
    r'|开\s*标\s*时\s*间(?:和地点|及地点)?'
    r'|开\s*标\s*日\s*期'
    r'|截\s*标\s*时\s*间'
    r'|截\s*标\s*日\s*期'
    r'|提交投标文件截止时间'
    r'|递交投标文件截止时间'
    r'|投标文件递交截止时间'
    r'|投标截止时间'
    r'|递交响应文件截止时间'
    r'|提交响应文件截止时间'
    r'|响应文件提交截止时间'
    r'|响应文件开启时间'
    r'|磋商(?:响应)?文件(?:递交|提交)截止时间'
    r'|报价截止时间)'
)

_OPEN_DATE_VALUE_RE = re.compile(
    r'(20[2-3]\s*\d)\s*[年\-/.]\s*([01]?\s*\d)\s*[月\-/.]\s*([0-3]?\s*\d)\s*日?'
)

_OPEN_DATE_CN_VALUE_RE = re.compile(
    r'([二两][〇零oO0-9一二三四五六七八九]{3})\s*年\s*'
    r'([正一二三四五六七八九十冬腊]{1,3})\s*月\s*'
    r'([一二三四五六七八九十廿]{1,3})\s*日?'
)

# 锚点与日期之间若出现新的非开标章节标题（如前文无日期的资格句后接"获取招标文件"），立即截断搜索窗口防串行误抓
_NON_OPEN_SECTION_STOP_RE = re.compile(
    r'(?:获取(?:招标|采购|磋商|谈判)文件|招标文件(?:的)?获取|采购文件(?:的)?获取|公告期限|投标人资格要求|申请人的资格要求)'
)

_CN_DIGIT_MAP = {
    '〇': 0, '零': 0, 'o': 0, 'O': 0, '0': 0,
    '一': 1, '壹': 1, '1': 1,
    '二': 2, '贰': 2, '两': 2, '2': 2,
    '三': 3, '叁': 3, '3': 3,
    '四': 4, '肆': 4, '4': 4,
    '五': 5, '伍': 5, '5': 5,
    '六': 6, '陆': 6, '6': 6,
    '七': 7, '柒': 7, '7': 7,
    '八': 8, '捌': 8, '8': 8,
    '九': 9, '玖': 9, '9': 9,
}


def _parse_cn_numeral_int(raw: str, *, is_year: bool = False) -> Optional[int]:
    """将中文数字日期段（如 '二〇二六' / '十一' / '二十五'）转为整数。"""
    s = re.sub(r'\s+', '', raw or '')
    if not s:
        return None
    if is_year:
        if len(s) != 4:
            return None
        digits: list[str] = []
        for ch in s:
            d = _CN_DIGIT_MAP.get(ch)
            if d is None:
                return None
            digits.append(str(d))
        return int(''.join(digits))

    if s in ('正',):
        return 1
    if s in ('冬',):
        return 11
    if s in ('腊',):
        return 12
    if '廿' in s:
        tail = s.replace('廿', '')
        return 20 + (_CN_DIGIT_MAP.get(tail, 0) if tail else 0)
    if '十' in s:
        left, right = s.split('十', 1)
        tens = 1 if not left else _CN_DIGIT_MAP.get(left)
        ones = 0 if not right else _CN_DIGIT_MAP.get(right)
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    if len(s) == 1 and s in _CN_DIGIT_MAP:
        return _CN_DIGIT_MAP[s]
    return None


def _is_valid_gap_and_not_range(window: str, date_start: int, date_end: int) -> bool:
    """校验锚点与日期间的噪声可控，且该日期不紧跟'至/到/—'+另一日期（排除获取时间区间）。"""
    between = window[:date_start]
    cleaned_between = re.sub(
        r'[\s:：、，,。；;（）()\[\]【】\-—.．0-9一二三四五六七八九十]|时间|日期|地点|北京|上午|下午|截标|开标',
        '',
        between,
    )
    if len(cleaned_between) > 6:
        return False

    after_date = window[date_end:date_end + 28]
    if re.match(r'^\s*(?:至|到|—|-|~|～)\s*(?:20[2-3]\s*\d|[01]?\s*\d\s*月|[二两][〇零oO0-9一二三四五六七八九]{3}\s*年|[正一二三四五六七八九十冬腊]{1,3}\s*月)', after_date):
        return False
    return True


def extract_open_date_with_evidence(text: str) -> Optional[dict[str, Any]]:
    """E23 §2.1：从招标文件全文提取开标/投标截止日期，并返回带原文片段与偏移的证据对象。

    通用泛化设计与明确规则边界说明：
    1. 不针对特定样本硬编码，基于全国政采/国企标准招标锚点同义词族提取；
    2. 允许锚点与日期之间跨换行（\\n）、列表序号（1./（1））、时间标签与 PDF 短页码；
    3. 排除前接'截止/截至'后接'前/内/止'的资格回顾期句式，以及'YYYY年MM月DD日至...'发售区间；
    4. 同时兼容阿拉伯数字日期（2026-10-16 / 2026年10月16日）与中文大写日期（二〇二六年十一月五日）；
    5. 已知规则层边界：若招标正文之后另附延期/更正公告修改开标日、表格双栏隔断超过 120 字符，
       或扫描件 OCR 将年份数字损毁（如 2O26），需依赖人工手填/修正开标日（支持手填优先与冲突告警）。

    Returns:
        {'open_date': 'YYYY-MM-DD', 'quote': 原文逐字片段, 'offset': int} 或 None
    """
    if not text:
        return None

    for m_anchor in _OPEN_DATE_ANCHOR_RE.finditer(text):
        a_start, a_end = m_anchor.start(), m_anchor.end()

        # 护栏 1：排除资格要求中的相对时间描述（如 "截止提交投标文件截止时间前..." / "...截止时间前三年内"）
        prefix_4 = text[max(0, a_start - 4):a_start]
        suffix_10 = text[a_end:min(len(text), a_end + 10)]
        if re.search(r'^\s*(?:前|以前|之前|内|止|起)', suffix_10):
            continue
        if ('截止' in prefix_4 or '截至' in prefix_4) and not re.search(r'^\s*[：:\n1-9（(]', suffix_10):
            continue

        window = text[a_end:min(len(text), a_end + 120)]
        m_stop = _NON_OPEN_SECTION_STOP_RE.search(window)
        if m_stop:
            window = window[:m_stop.start()]

        for m_date in _OPEN_DATE_VALUE_RE.finditer(window):
            if not _is_valid_gap_and_not_range(window, m_date.start(), m_date.end()):
                continue
            y_str = re.sub(r'\s+', '', m_date.group(1))
            m_str = re.sub(r'\s+', '', m_date.group(2))
            d_str = re.sub(r'\s+', '', m_date.group(3))
            try:
                dt = datetime(int(y_str), int(m_str), int(d_str))
                quote = text[a_start:a_end + m_date.end()]
                return {
                    'open_date': dt.strftime('%Y-%m-%d'),
                    'quote': quote,
                    'offset': a_start,
                }
            except ValueError:
                continue

        # 次级回退：同窗口内匹配中文大写年月日（如 "二〇二六年十一月五日"）
        for m_cn in _OPEN_DATE_CN_VALUE_RE.finditer(window):
            if not _is_valid_gap_and_not_range(window, m_cn.start(), m_cn.end()):
                continue
            year = _parse_cn_numeral_int(m_cn.group(1), is_year=True)
            month = _parse_cn_numeral_int(m_cn.group(2), is_year=False)
            day = _parse_cn_numeral_int(m_cn.group(3), is_year=False)
            if year is None or month is None or day is None:
                continue
            try:
                dt = datetime(year, month, day)
                quote = text[a_start:a_end + m_cn.end()]
                return {
                    'open_date': dt.strftime('%Y-%m-%d'),
                    'quote': quote,
                    'offset': a_start,
                }
            except ValueError:
                continue

    return None


def extract_open_date_from_tender_text(text: str) -> Optional[str]:
    """E23 §2.1：从招标文件全文提取开标日期或投标截止日期（返回 YYYY-MM-DD，未识别返回 None）。"""
    ev = extract_open_date_with_evidence(text)
    return ev['open_date'] if ev else None


def load_document(file_path: str) -> ParsedDoc:
    """解析招标文件为 ParsedDoc（复用 core.parser，含扫描件 OCR 兜底）。"""
    return parse_document(file_path)


def _find_clause_header_before(text: str, pos: int, window: int = 800) -> str:
    """在 pos 之前 window 字符窗口内找最近的条款编号头，返回编号原文（无则空串）。"""
    seg_start = max(0, pos - window)
    segment = text[seg_start:pos]
    last = ''
    for m in _CLAUSE_HEADER_RE.finditer(segment):
        last = m.group(0)
    return last


def _expand_enclosing_clause(text: str, hit_start: int, hit_end: int,
                             max_len: int = 600) -> tuple[int, int, str]:
    """把命中点扩展为所在编号条款的 [start, end) 与条款编号。"""
    clause_no = _find_clause_header_before(text, hit_start)
    start = hit_start
    if clause_no:
        idx = text.rfind(clause_no, max(0, hit_start - 800), hit_start)
        if idx >= 0:
            start = idx
    nxt = _CLAUSE_HEADER_RE.search(text, hit_end, hit_end + max_len)
    end = nxt.start() if nxt else min(len(text), hit_end + max_len)
    period = text.rfind('。', start, end)
    if period > start + 20:
        end = period + 1
    if end - start > max_len:
        end = start + max_len
    return start, end, clause_no


def locate_chapters(text: str) -> list[dict]:
    """定位全部"第X章 标题"。"""
    chapters = []
    seen_spans = set()
    for m in _CHAPTER_RE.finditer(text):
        key = (m.start(), m.end())
        if key in seen_spans:
            continue
        seen_spans.add(key)
        raw = m.group(0)
        parts = raw.split(None, 1)
        index = parts[0]
        title = parts[1].strip() if len(parts) > 1 else ''
        chapters.append({'index': index, 'title': title, 'start': m.start()})
    return chapters


def extract_marked_clauses(text: str) -> list[dict]:
    """提取 ★/▲ 标记条款（标记型语料的确定性主通道）。"""
    lines = []
    offset = 0
    for raw_line in text.split('\n'):
        lines.append((offset, raw_line))
        offset += len(raw_line) + 1

    hits = []
    # 预扫描：说明语行独立成档（每行仅一条），★/▲ 双通道均跳过这些行
    note_line_idx: set[int] = set()
    for idx, (line_start, line_text) in enumerate(lines):
        if _MARK_RE.search(line_text) and _USAGE_NOTE_RE.search(line_text):
            note_line_idx.add(idx)
            hits.append({
                'mark': _MARK_RE.search(line_text).group(),
                'text': line_text.strip(),
                'start': line_start,
                'end': line_start + len(line_text),
                'clause_no': _find_clause_header_before(text, line_start),
                'kind': 'usage_note',
            })

    # 按标记类型各自独立扫描合并：★ 与 ▲ 是两类不同强度的强制条款，严禁混并
    for mark in ('★', '▲'):
        i = 0
        while i < len(lines):
            line_start, line_text = lines[i]
            if mark not in line_text or i in note_line_idx:
                i += 1
                continue
            j = i
            merged_end = line_start + len(line_text)
            merged_lines = [line_text.strip()]
            while j + 1 < len(lines):
                nxt_start, nxt_text = lines[j + 1]
                # 双向防混并：①说明语行/孤立碎片行不并入要求记录；②说明语记录也不吞并后续要求行
                if (mark in nxt_text and len(nxt_text.strip()) < 300
                        and not _USAGE_NOTE_RE.search(nxt_text)
                        and not _is_meaningless_short_fragment(nxt_text)
                        and not _USAGE_NOTE_RE.search('\n'.join(merged_lines))
                        and not _is_meaningless_short_fragment('\n'.join(merged_lines))):
                    merged_lines.append(nxt_text.strip())
                    merged_end = nxt_start + len(nxt_text)
                    j += 1
                else:
                    break
            clause_no = _find_clause_header_before(text, line_start)
            merged_text = '\n'.join(merged_lines)
            hits.append({
                'mark': mark,
                'text': merged_text,
                'start': line_start,
                'end': merged_end,
                'clause_no': clause_no,
                'kind': _classify_kind(merged_text),
            })
            i = j + 1
    hits.sort(key=lambda h: h['start'])
    return hits


def extract_invalid_bid_clauses(text: str) -> list[dict]:
    """提取无标记措辞型强制条款（全文无任何 ★/▲ 标记）（徐州份核验沉淀的模式 A / 模式 B）。"""
    records = []

    for m in _PATTERN_A_RE.finditer(text):
        start, end, clause_no = _expand_enclosing_clause(text, m.start(), m.end())
        segment = text[m.start():min(len(text), m.end() + 200)]
        promise = ''
        pm = _PROMISE_DOC_RE.search(segment)
        if pm:
            promise = pm.group(1)
        records.append({
            'pattern': 'A', 'text': text[start:end], 'start': start, 'end': end,
            'clause_no': clause_no, 'promise_doc': promise,
        })

    for m in _PATTERN_B_RE.finditer(text):
        start, end, clause_no = _expand_enclosing_clause(text, m.start(), m.end())
        records.append({
            'pattern': 'B', 'text': text[start:end], 'start': start, 'end': end,
            'clause_no': clause_no, 'promise_doc': '',
        })

    records.sort(key=lambda r: (r['start'], 0 if r['pattern'] == 'A' else 1))
    deduped = []
    for r in records:
        if deduped and r['start'] < deduped[-1]['end']:
            continue
        deduped.append(r)
    return deduped


def locate_keyword_sections(text: str, keywords: tuple[str, ...],
                            context_after: int = 1200) -> list[dict]:
    """定位关键词出现的区段（资格审查 / 符合性审查 / 评分细则 等）。"""
    hits = []
    for kw in keywords:
        begin = 0
        while True:
            idx = text.find(kw, begin)
            if idx < 0:
                break
            hits.append({
                'keyword': kw,
                'start': idx,
                'excerpt': text[idx:idx + context_after],
            })
            begin = idx + len(kw)
    hits.sort(key=lambda h: h['start'])
    return hits


def build_review_checklist(file_path: str) -> dict:
    """对一份招标文件跑确定性层全流程，汇总四类候选并附带双轨页码（PDF 印刷/物理页码 + docx 估算页码）。"""
    from core.response_checker import build_unified_page_context, resolve_unified_page

    doc = load_document(file_path)
    text = doc.text
    page_ctx = build_unified_page_context(file_path)

    def _attach_pages(items: list[dict]) -> list[dict]:
        for it in items:
            it['page'] = resolve_unified_page(it.get('start'), page_ctx)[0]
        return items

    chapters = _attach_pages(locate_chapters(text))
    # 评标章：过滤目录行（目录条目带点线引导符或以页码结尾），取正文最后一处命中
    eval_candidates = [ch for ch in chapters if any(k in ch['title'] for k in _EVAL_CHAPTER_KEYS)]
    real = [ch for ch in eval_candidates
            if '…' not in ch['title'] and not re.search(r'[.·\t]*\d+\s*$', ch['title'])]
    eval_chapter = real[-1] if real else (eval_candidates[-1] if eval_candidates else None)
    open_date_ev = extract_open_date_with_evidence(text)
    return {
        'file': file_path,
        'source_type': doc.source_type,
        'pages': doc.pages,
        'chars': len(text),
        'open_date': open_date_ev['open_date'] if open_date_ev else None,
        'open_date_source': 'tender_extracted' if open_date_ev else None,
        'open_date_quote': open_date_ev['quote'] if open_date_ev else None,
        'chapters': chapters,
        'marked_clauses': _attach_pages(extract_marked_clauses(text)),
        'invalid_bid_clauses': _attach_pages(extract_invalid_bid_clauses(text)),
        'qualification_hits': _attach_pages(locate_keyword_sections(text, _QUALIFY_KEYS)),
        'compliance_hits': _attach_pages(locate_keyword_sections(text, _COMPLIANCE_KEYS)),
        'eval_chapter': eval_chapter,
        'scoring_hits': _attach_pages(locate_keyword_sections(text, ('评分细则', '评分标准', '评分办法'))),
        'scoring_tables': extract_scoring_tables(file_path),
    }


# ── 评分细则表结构化（0 Token，docx 表格型 + E1 100分强守恒校准）──────────
_SCORE_TABLE_SCORE_KEYS = ('分值', '分数', '得分', '标准分', '满分')
_SCORE_TABLE_FACTOR_KEYS = ('评审因素', '评分内容', '评标内容', '评审内容', '评分因素')
_SCORE_TABLE_DETAIL_KEYS = ('评审细则', '评分标准', '评标标准', '评审标准')
_SCORE_NUM_RE = re.compile(r'\d+(?:\.\d+)?')

_PRICE_FACTOR_KEYWORDS = ('价格', '报价', '投标报价', '评标基准价')
_PROSE_PRICE_SCORE_PATTERNS = (
    re.compile(
        r'(?:价格分(?:值)?|投标报价(?:分|得分)?|报价分|价格部分|价格评审)'
        r'[^0-9\n。；]{0,35}?'
        r'(?:满分(?:为)?|分值(?:为)?|占|为|[（(]|[:：])\s*(\d{1,2}(?:\.\d+)?)\s*分'
    ),
    re.compile(
        r'(?:价格分(?:值)?|投标报价(?:分)?|报价分|价格部分)'
        r'\s*(\d{1,2}(?:\.\d+)?)\s*分'
    ),
    re.compile(
        r'评标基准价[^。\n]{0,35}?价格分(?:为)?(?:满分)?\s*[：:为]?\s*(\d{1,2}(?:\.\d+)?)\s*分'
    ),
    re.compile(
        r'(?:价格(?:分|得分)|投标报价(?:分|得分)?)\s*[=＝]\s*[（(]?\s*评标基准价\s*[/／÷]\s*投标报价\s*[)）]?\s*[*×xX]\s*(\d{1,2}(?:\.\d+)?)\s*分?'
    ),
)


def _dedup_row_cells(row) -> list:
    """按 tc 元素本体去重取一行单元格（vMerge 延续格重复返回，与 parser 同款修复）。"""
    seen = set()
    cells = []
    for cell in row.cells:
        if id(cell._tc) in seen:
            continue
        seen.add(id(cell._tc))
        cells.append(cell)
    return cells


def _map_score_columns(header_texts: list) -> dict:
    """按表头关键字映射列号：{'no':i,'factor':i,'detail':i,'score':i,'type':i}。"""
    col = {}
    for i, h in enumerate(header_texts):
        if col.get('score') is None and any(k in h for k in _SCORE_TABLE_SCORE_KEYS):
            col['score'] = i
        elif col.get('factor') is None and any(k in h for k in _SCORE_TABLE_FACTOR_KEYS):
            col['factor'] = i
        elif col.get('detail') is None and any(k in h for k in _SCORE_TABLE_DETAIL_KEYS):
            col['detail'] = i
        elif col.get('no') is None and h.strip() in ('序号', '编号'):
            col['no'] = i
        elif col.get('type') is None and '类型' in h:
            col['type'] = i
    return col


def _has_price_scoring_item(items: list[dict]) -> bool:
    """检查已提取的表格评分项中是否已包含价格/报价分项。"""
    for it in items:
        combined = f"{it.get('section', '')} {it.get('factor', '')} {it.get('no', '')}"
        if any(k in combined for k in _PRICE_FACTOR_KEYWORDS):
            return True
    return False


def _extract_prose_price_candidates(doc) -> list[tuple[Decimal, str]]:
    """从 docx 正文段落及表格文本中提取候选价格分值及所在原句。"""
    paras = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
    full_para_text = '\n'.join(paras)
    chapters = locate_chapters(full_para_text) if full_para_text else []
    eval_candidates = [ch for ch in chapters if any(k in ch['title'] for k in _EVAL_CHAPTER_KEYS)]
    real_eval = [
        ch for ch in eval_candidates
        if '…' not in ch['title'] and not re.search(r'[.·\t]*\d+\s*$', ch['title'])
    ]
    eval_ch = real_eval[-1] if real_eval else (eval_candidates[-1] if eval_candidates else None)

    search_regions = []
    if eval_ch is not None:
        search_regions.append(full_para_text[eval_ch['start']:])
    if full_para_text:
        search_regions.append(full_para_text)

    table_texts = []
    for _t in doc.tables:
        for _row in _t.rows:
            for _cell in _dedup_row_cells(_row):
                if _cell.text and _cell.text.strip():
                    table_texts.append(_cell.text.strip())
    if table_texts:
        search_regions.append('\n'.join(table_texts))

    found: list[tuple[Decimal, str]] = []
    seen_scores: set[Decimal] = set()

    for region in search_regions:
        for pat in _PROSE_PRICE_SCORE_PATTERNS:
            for m in pat.finditer(region):
                num_str = m.group(1)
                try:
                    val = Decimal(num_str)
                except InvalidOperation:
                    continue
                if val <= Decimal('0') or val >= Decimal('100'):
                    continue
                if val in seen_scores:
                    continue
                line_lo = region.rfind('\n', 0, m.start()) + 1
                line_hi = region.find('\n', m.end())
                if line_hi < 0:
                    line_hi = len(region)
                sentence = region[line_lo:line_hi].strip()
                seen_scores.add(val)
                found.append((val, sentence))
        if found:
            break

    return found


def extract_scoring_tables(file_path: str) -> list[dict]:
    """结构化 docx 评分细则表（表格型语料 0 Token 主通道 + E1 100分强守恒校准）。"""
    if not file_path.lower().endswith('.docx'):
        return []
    from docx import Document
    doc = Document(file_path)
    items = []
    for ti, table in enumerate(doc.tables):
        if not table.rows:
            continue
        header_cells = _dedup_row_cells(table.rows[0])
        header_texts = [c.text.strip() for c in header_cells]
        col = _map_score_columns(header_texts)
        if col.get('score') is None or (col.get('factor') is None and col.get('detail') is None):
            continue
        section = ''
        for ri, row in enumerate(table.rows[1:], start=1):
            cells = _dedup_row_cells(row)
            texts = [c.text.strip() for c in cells]
            nonempty = [t for t in texts if t]
            if len(nonempty) <= 1:
                section = nonempty[0] if nonempty else section
                continue
            def _cell(key):
                i = col.get(key)
                return texts[i] if i is not None and i < len(texts) else ''
            score_raw = _cell('score')
            nums = _SCORE_NUM_RE.findall(score_raw)
            items.append({
                'table_index': ti,
                'row_index': ri,
                'no': _cell('no'),
                'section': section,
                'factor': _cell('factor'),
                'detail': _cell('detail'),
                'score': score_raw,
                'score_max': max((float(n) for n in nums), default=None),
                'type': _cell('type'),
                'page': None,
            })

    table_sum = Decimal('0')
    for it in items:
        s_max = it.get('score_max')
        if s_max is not None:
            try:
                table_sum += Decimal(str(s_max))
            except InvalidOperation:
                pass

    if items and Decimal('0') < table_sum < Decimal('100'):
        has_price_item = _has_price_scoring_item(items)
        none_price_rows = [
            it for it in items
            if it.get('score_max') is None
            and any(
                k in f"{it.get('section', '')} {it.get('factor', '')} {it.get('detail', '')}"
                for k in _PRICE_FACTOR_KEYWORDS
            )
        ]
        prose_candidates = _extract_prose_price_candidates(doc)

        if has_price_item and len(none_price_rows) == 1:
            for prose_score, _prose_sentence in prose_candidates:
                if table_sum + prose_score == Decimal('100'):
                    row = none_price_rows[0]
                    row['score_max'] = float(prose_score)
                    if not row.get('score'):
                        row['score'] = str(int(prose_score)) if prose_score == int(prose_score) else str(prose_score)
                    row['type'] = ((row.get('type') or '') + '（分值由正文 100 分守恒校准补齐）').strip()
                    break
        elif not has_price_item:
            for prose_score, prose_sentence in prose_candidates:
                if table_sum + prose_score == Decimal('100'):
                    score_str = (
                        str(int(prose_score))
                        if prose_score == int(prose_score)
                        else str(prose_score)
                    )
                    items.append({
                        'table_index': items[0].get('table_index', 0),
                        'row_index': 0,
                        'no': '价格',
                        'section': '价格分（正文提取）',
                        'factor': '价格分（正文提取）',
                        'detail': prose_sentence,
                        'score': score_str,
                        'score_max': float(prose_score),
                        'type': '价格分（正文提取）',
                        'page': None,
                    })
                    break

    return items