# -*- coding: utf-8 -*-
"""F4 ★条款逐条响应核验 + 标书前后矛盾检测（大模型①② + 双书勾稽 + E2/E3 门控混合检索 + E6 docx 双轨页码接线）。

模块职责（对应 PRD-02 §7 F4 与 E2/E3/E6 任务卡）：
1. ★/▲ 强制条款逐条响应核验（双书勾稽，大模型①）：
   - 输入：招标文件 + 投标文件 + F1 提取的 ★/▲ 要求清单（仅消费 kind=='requirement' 及模式 A/B）；
   - 门控链路（E2/E3 同文对齐加固）：
     ① 初筛（原文子串/括号长句分句/条款号+标题/高 2-gram 重合度）直接命中 -> 0ms 直通放行；
     ② 初筛未命中的"疑似漏应答候选" -> 启动 core.hybrid_retriever 三级混合检索；
   - 缺证明材料支撑探针（E2 升级点 3a · MISSING_EVIDENCE）；
   - E6 页码双轨接线：tender/bid 为 PDF 时走 build_pdf_page_spans + offset_to_page；
     为 docx 时走 build_docx_page_spans + offset_to_docx_page，page 填 physical，
     footer_page 与 is_docx_page 同步写入结果与 detail_json，无分页锚点时保持 None。
2. 标书前后矛盾检测（书内自洽扫描，0 Token 确定性 + 大模型②）。
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

from core.bid_aligner import compare_contracts, _build_clause_units
from core.hybrid_retriever import hybrid_retrieve_windows
from core.parser import (
    build_docx_page_spans,
    build_pdf_page_spans,
    offset_to_docx_page,
    offset_to_page,
    parse_document,
)
from core.quote_verifier import normalize_zh, verify_quote
from core.tender_extractor import (
    build_review_checklist,
    extract_invalid_bid_clauses,
    extract_marked_clauses,
)

# ── 常量与扩窗配置 ────────────────────────────────────────────────────────
_REQ_EXPAND_BEFORE = 60
_REQ_EXPAND_AFTER = 360
_BID_WIN_BEFORE = 120
_BID_WIN_AFTER = 480
_ANCHOR_MIN_SCORE = 0.22

_EVIDENCE_WARNING_NOTE = '（注：请核实已附对应证明材料）'

_EVIDENCE_REQ_RE = re.compile(
    r'(?:须提供|应提供|必须提供|需提供|提供|须具有|应具有|须附|应附)'
    r'[^。；\n]{0,35}?'
    r'(?:证书|检测报告|测试报告|检验报告|截图|证明材料|证明文件|承诺函|复印件|扫描件|认证)'
)

_EVIDENCE_ANCHOR_RE = re.compile(
    r'见附件|详见|附后|见后附|见本标书|见投标文件|附件\s*[0-9一二三四五六七八九十]+'
    r'|证书编号|报告编号|认证编号|登记号|编号\s*[：:]'
    r'|有效期至|发证机关|发证单位|检验结论|检测结论|实测报告'
    r'|扫描件如下|复印件如下|截图如下|详见第\s*\d+\s*页'
)

_EVIDENCE_STRICT_ANCHOR_RE = re.compile(
    r'(?:证书编号|报告编号|认证编号|登记号|许可证编号|资质编号|备案编号)'
    r'\s*[:：]?\s*[A-Za-z0-9]{4,}'
)

_CN_NUM_MAP = {
    '零': 0, '一': 1, '壹': 1, '二': 2, '贰': 2, '两': 2, '俩': 2,
    '三': 3, '叁': 3, '四': 4, '肆': 4, '五': 5, '伍': 5,
    '六': 6, '陆': 6, '七': 7, '柒': 7, '八': 8, '捌': 8,
    '九': 9, '玖': 9, '十': 10, '拾': 10,
}

_JSON_BLOCK_RE = re.compile(r'\{.*\}', re.DOTALL)

_RESPONSE_CHECK_PROMPT = """你是招投标合规审查专家。下面给出招标文件中的一条【实质性要求原文窗口】与投标文件中检索到的【投标响应原文窗口】。
请核验投标文件对该条招标要求的响应状态，并只输出一个 JSON 对象。

【招标要求原文窗口】
{tender_window}

【招标要求核心句】
{req_text}

【投标响应原文窗口】
{bid_window}

判定规则：
1. status 只能取以下三个值之一：
   - "RESPONSE_OK"：投标窗口对该条要求作出了正面响应且无参数冲突/负偏离；
   - "MISSING_RESPONSE"：投标窗口完全未提及该条要求的内容（漏应答）；
   - "CONTRADICTION"：投标响应参数与招标要求冲突/负偏离（如招标要求质保3年，投标写质保2年；或要求并发≥5000，投标写3000）；
2. tender_quote：从【招标要求原文窗口】中逐字抄录的核心要求原句（严禁改写）；
3. bid_quote：从【投标响应原文窗口】中逐字抄录的对应响应原句（若为 MISSING_RESPONSE 则填 ""，严禁改写或编造）；
4. reason：30 字以内的判定理由。

只输出 JSON，不要任何其他文字。"""

_SELF_CONTRADICTION_PROMPT = """你是投标文件合规审查专家。下面是同一份投标文件中不同位置关于【{topic}】的两段原文摘录。
请判断这两段承诺是否存在实质性前后矛盾（如同一标的质保期、工期、响应时限或服务范围前后冲突）。

【位置 A 原文】
{text_a}

【位置 B 原文】
{text_b}

要求只输出一个 JSON 对象：
1. is_contradiction：布尔值（true 表示存在前后矛盾，false 表示一致或属不同分项正常区分）；
2. quote_a：从【位置 A 原文】逐字抄录的冲突句（严禁改写）；
3. quote_b：从【位置 B 原文】逐字抄录的冲突句（严禁改写）；
4. reason：30 字以内矛盾说明。

只输出 JSON，不要任何其他文字。"""


def _parse_llm_json(content: str) -> Optional[dict]:
    if not content:
        return None
    m = _JSON_BLOCK_RE.search(content)
    if not m:
        return None
    try:
        obj = json.loads(m.group())
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _build_pdf_page_spans(file_path: Optional[str | Path]) -> list[tuple[int, int, int]]:
    """兼容旧内部调用：PDF 返回 3 元组列表，非 PDF 返回 []。"""
    return build_pdf_page_spans(file_path)


def _offset_to_page(offset: Optional[int], page_spans: list[tuple[int, int, int]]) -> Optional[int]:
    """兼容旧内部调用：将字符偏移换算为 PDF 页码。"""
    return offset_to_page(offset, page_spans)


def _build_unified_page_context(file_path: Optional[str | Path]) -> dict[str, Any]:
    """E6 统一构建文档页码映射上下文（自动区分 PDF 与 docx 双轨页码）。"""
    if not file_path:
        return {'kind': 'none', 'pdf_spans': [], 'docx_spans': []}
    path = Path(file_path)
    if not path.is_file():
        return {'kind': 'none', 'pdf_spans': [], 'docx_spans': []}
    ext = path.suffix.lower()
    if ext == '.pdf':
        return {'kind': 'pdf', 'pdf_spans': build_pdf_page_spans(path), 'docx_spans': []}
    if ext == '.docx':
        return {'kind': 'docx', 'pdf_spans': [], 'docx_spans': build_docx_page_spans(path)}
    return {'kind': 'none', 'pdf_spans': [], 'docx_spans': []}


def _resolve_unified_page(offset: Optional[int], ctx: dict[str, Any]) -> tuple[Optional[int], Optional[int], bool]:
    """根据偏移量与文档页码上下文返回 (physical_or_pdf_page, footer_page, is_docx_page)。"""
    if offset is None or offset < 0:
        return None, None, False
    kind = ctx.get('kind', 'none')
    if kind == 'pdf':
        p = offset_to_page(offset, ctx.get('pdf_spans') or [])
        return p, None, False
    if kind == 'docx':
        hit = offset_to_docx_page(offset, ctx.get('docx_spans') or [])
        if hit is not None:
            return hit['physical'], hit['footer'], True
    return None, None, False


def _cn_to_int(s: str) -> Optional[int]:
    """将中文数字或阿拉伯数字字符串转为整数。"""
    s = (s or '').strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if len(s) == 1 and s in _CN_NUM_MAP:
        return _CN_NUM_MAP[s]
    total = 0
    cur = 0
    for ch in s:
        if ch in ('十', '拾'):
            total += (cur if cur > 0 else 1) * 10
            cur = 0
        elif ch in _CN_NUM_MAP:
            cur = _CN_NUM_MAP[ch]
        else:
            return None
    return total + cur


def _expand_req_text(full_doc_text: str, req: dict) -> tuple[str, int, int]:
    """按 _README.md 规范：若候选记录为 PDF 表格断行截短片段，按 start 向后扩窗吸附完整句子。"""
    raw_txt = (req.get('full_text') or req.get('text') or '').strip()
    start = int(req.get('start', 0))
    end = int(req.get('end', start + len(raw_txt)))
    if not full_doc_text:
        return raw_txt, start, end

    if len(raw_txt) < 18 and not raw_txt.endswith(('。', '；', '！')):
        hi = min(len(full_doc_text), start + _REQ_EXPAND_AFTER)
        slice_after = full_doc_text[start:hi]
        period_pos = slice_after.find('。')
        if period_pos != -1:
            end_pos = period_pos + 1
            rest = slice_after[end_pos:]
            m_paren = re.match(r'^\s*[（(][^）)]{1,60}[）)]', rest)
            if m_paren:
                end_pos += m_paren.end()
            expanded = slice_after[:end_pos].strip()
        else:
            lines = [ln.strip() for ln in slice_after.splitlines() if ln.strip()]
            expanded = '\n'.join(lines[:3]).strip()
        if len(expanded) > len(raw_txt):
            return expanded, start, start + len(expanded)

    return raw_txt, start, end


def _extract_core_grams(text: str) -> set[str]:
    """剥离 ★/▲ 标记与通用虚词，提取用于锚点检索的 2-gram 集合。"""
    cleaned = re.sub(r'[★▲\s0-9.、()（）【】\[\]：:，,。；;“”"\']+', '', text or '')
    for stop in ('投标人', '供应商', '采购人', '招标文件', '投标文件', '要求', '提供', '必须', '应当', '满足'):
        cleaned = cleaned.replace(stop, '')
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}


def _build_direct_hit_result(bid_text: str, pos: int, anchor_len: int, score: float = 1.0) -> dict[str, Any]:
    """根据在 bid_text 中命中的偏移量构造直通窗口字典（gate='direct'）。"""
    w_lo = max(0, pos - _BID_WIN_BEFORE)
    w_hi = min(len(bid_text), pos + anchor_len + _BID_WIN_AFTER)
    line_end = bid_text.find('\n', pos + anchor_len)
    q_end = (
        line_end
        if (line_end != -1 and line_end - pos <= 260)
        else min(len(bid_text), pos + max(anchor_len, 40) + 80)
    )
    return {
        'hit': True,
        'score': score,
        'window': bid_text[w_lo:w_hi],
        'start': pos,
        'end': q_end,
        'matched_quote': bid_text[pos:q_end].strip(),
        'gate': 'direct',
    }


def _locate_bid_window_for_req(
    req_text: str,
    req_start: int,
    req_end: int,
    tender_units: list,
    bid_units: list,
    align_map: dict[str, list[str]],
    bid_text: str,
    client: Any = None,
) -> dict[str, Any]:
    """为单条招标 ★/▲ 要求在投标书中定位最佳响应窗口（带 E3 同文对齐加固与严格门控 Gate Filter）。"""
    if not bid_text or not bid_text.strip():
        return {
            'hit': False, 'score': 0.0, 'window': '',
            'start': None, 'end': None, 'matched_quote': '', 'gate': 'miss',
        }

    req_stripped = (req_text or '').strip()
    if not req_stripped:
        return {
            'hit': False, 'score': 0.0, 'window': '',
            'start': None, 'end': None, 'matched_quote': '', 'gate': 'miss',
        }

    first_line = req_stripped.split('\n', 1)[0].strip()
    for exact_cand in (req_stripped, first_line):
        if len(exact_cand) >= 2:
            pos = bid_text.find(exact_cand)
            if pos != -1:
                return _build_direct_hit_result(bid_text, pos, len(exact_cand), score=1.0)

    raw_core = re.sub(r'^[★▲\s]+', '', first_line).strip()
    if len(raw_core) >= 2:
        pos = bid_text.find(raw_core[:20])
        if pos != -1:
            return _build_direct_hit_result(bid_text, pos, min(len(raw_core), 20), score=1.0)

    m_t = re.match(r'^[★▲]?\s*[\d一二三四五六七八九十]+\s*[、\.．]\s*[^，。；\n\s]{2,40}', first_line)
    title = re.sub(r'^[★▲\s]+', '', (m_t.group(0) if m_t else first_line)).strip().rstrip('。；：:，, ')
    if len(title) >= 4:
        pos = bid_text.find(title[:24])
        if pos != -1:
            return _build_direct_hit_result(bid_text, pos, min(len(title), 24), score=0.95)

    sub_clauses = [
        re.sub(r'^[★▲\s0-9.、()（）一二三四五六七八九十]+', '', seg).strip()
        for seg in re.split(r'[\n；;。！？（）()]+', req_stripped)
    ]
    for seg in sub_clauses:
        if len(seg) >= 6:
            pos = bid_text.find(seg[:24])
            if pos != -1:
                return _build_direct_hit_result(bid_text, pos, min(len(seg), 24), score=0.92)

    req_grams = _extract_core_grams(req_text)

    best_unit = None
    best_score = 0.0
    for u in tender_units:
        if u.end >= req_start and u.start <= req_end:
            mapped_v2_keys = align_map.get(f"v1:{u.idx}", [])
            for v2_key in mapped_v2_keys:
                v2_idx = int(v2_key.split(':')[1])
                if 0 <= v2_idx < len(bid_units):
                    v_unit = bid_units[v2_idx]
                    v_grams = _extract_core_grams(v_unit.raw_text)
                    overlap = (len(req_grams & v_grams) / max(len(req_grams), 1)) if req_grams else 0.0
                    if overlap > best_score:
                        best_score = overlap
                        best_unit = v_unit

    if best_unit is not None and best_score >= _ANCHOR_MIN_SCORE:
        w_lo = max(0, best_unit.start - _BID_WIN_BEFORE)
        w_hi = min(len(bid_text), best_unit.end + _BID_WIN_AFTER)
        return {
            'hit': True,
            'score': round(best_score, 4),
            'window': bid_text[w_lo:w_hi],
            'start': best_unit.start,
            'end': best_unit.end,
            'matched_quote': best_unit.raw_text.strip(),
            'gate': 'direct',
        }

    best_line_score = 0.0
    best_line_span: Optional[tuple[int, int, str]] = None
    if req_grams:
        cursor = 0
        for raw_line in bid_text.split('\n'):
            line_len = len(raw_line)
            stripped = raw_line.strip()
            if len(stripped) >= 4:
                l_grams = _extract_core_grams(stripped)
                if l_grams:
                    score = len(req_grams & l_grams) / len(req_grams)
                    if score > best_line_score:
                        best_line_score = score
                        best_line_span = (cursor, cursor + line_len, stripped)
            cursor += line_len + 1

        if best_line_span is not None and best_line_score >= 0.35:
            l_start, l_end, l_txt = best_line_span
            w_lo = max(0, l_start - _BID_WIN_BEFORE)
            w_hi = min(len(bid_text), l_end + _BID_WIN_AFTER)
            return {
                'hit': True,
                'score': round(best_line_score, 4),
                'window': bid_text[w_lo:w_hi],
                'start': l_start,
                'end': l_end,
                'matched_quote': l_txt,
                'gate': 'direct',
            }

    hybrid_hits = hybrid_retrieve_windows(
        query=req_text,
        doc_text=bid_text,
        client=client,
        top_k=1,
        min_score=0.38,
    )
    if hybrid_hits:
        top_h = hybrid_hits[0]
        return {
            'hit': True,
            'score': top_h['score'],
            'window': top_h['window'],
            'start': top_h['start'],
            'end': top_h['end'],
            'matched_quote': top_h['quote'],
            'gate': 'hybrid',
        }

    return {
        'hit': False,
        'score': round(best_line_score, 4),
        'window': '',
        'start': None,
        'end': None,
        'matched_quote': '',
        'gate': 'miss',
    }


def _check_missing_evidence(
    req_text: str,
    bid_quote: str,
    bid_window: str,
    bid_text: str,
) -> Optional[str]:
    """E2 升级点 3a：缺证明材料支撑探针（MISSING_EVIDENCE）。"""
    m_req = _EVIDENCE_REQ_RE.search(req_text or '')
    if not m_req:
        return None

    evidence_phrase = m_req.group(0).strip()
    core_req_stripped = re.sub(r'^[★▲\s0-9.、()（）一二三四五六七八九十]+', '', req_text or '').strip()

    residual_window = (bid_window or '').replace(req_text, ' ').replace(core_req_stripped, ' ')
    residual_quote = (bid_quote or '').replace(req_text, ' ').replace(core_req_stripped, ' ')
    residual_full = (bid_text or '').replace(req_text, ' ').replace(core_req_stripped, ' ')

    residual_window = '\n'.join(
        ln for ln in residual_window.split('\n')
        if not re.match(r'^\s*[★▲]\s*[0-9一二三四五六七八九十]+\s*[.、．]?', ln)
    )

    if (
        _EVIDENCE_ANCHOR_RE.search(residual_quote)
        or _EVIDENCE_ANCHOR_RE.search(residual_window)
        or _EVIDENCE_STRICT_ANCHOR_RE.search(residual_full)
    ):
        return None

    return (
        f"【疑似缺佐证材料】招标文件要求「{evidence_phrase}」，投标书已作口头响应，"
        f"但全书未检索到对应证书/报告/截图或「见附件」索引，建议重点核实"
    )


# ── 参数抽取器 ────────────────────────────────────────────────────────────

_WARRANTY_RE = re.compile(
    r'(?:免费)?(?:质保期|质量保证期|维保期|保修期|维护期|免费维护|免费质保)'
    r'[^0-9零一二三四五六七八九十壹贰叁肆伍陆柒捌玖两\n。；]{0,18}?'
    r'([0-9]{1,3}|[零一二三四五六七八九十壹贰叁肆伍陆柒捌玖两]{1,3})\s*(个?\s*月|年)'
)

_WARRANTY_REV_RE = re.compile(
    r'([0-9]{1,3}|[零一二三四五六七八九十壹贰叁肆伍陆柒捌玖两]{1,3})\s*(个?\s*月|年)'
    r'\s*(?:的)?\s*(?:免费)?(?:质保期|质量保证期|保修期|维保期|维护期|质保)'
)

_RESPONSE_TIME_RE = re.compile(
    r'(?:故障响应|响应时间|响应时限|接到.*?(?:通知|报修).*?后)'
    r'[^0-9零一二三四五六七八九十两\n。；]{0,15}?'
    r'([0-9]{1,3}|[一二三四五六七八九十两]{1,3})\s*(分钟|小时)'
)

_RETENTION_RATIO_RE = re.compile(
    r'(?:质保金|质量保证金|履约保证金)[^0-9\n。；]{0,20}?([0-9]{1,2}(?:\.[0-9]+)?)\s*[%％]'
)

_BID_TOTAL_MENTION_RE = re.compile(
    r'(?:投标总报价|投标总价|报价总计|总报价)'
    r'\s*(?:[（(][^）)\n]{0,10}[）)])?\s*[：:]\s*'
    r'(?:小写[：:\s]*|人民币\s*|[￥¥]\s*)?'
    r'([0-9][0-9,]*(?:\.[0-9]+)?)\s*(万元|元)'
)


def _extract_sentence_around(text: str, start: int, end: int) -> tuple[str, int, int]:
    """提取命中位置所在的完整单句或单行。"""
    lo = start
    while lo > 0 and text[lo - 1] not in '\n。；！？':
        if start - lo >= 90:
            break
        lo -= 1
    hi = end
    while hi < len(text) and text[hi] not in '\n。；！？':
        if hi - end >= 90:
            break
        hi += 1
    if hi < len(text) and text[hi] in '。；！？':
        hi += 1
    quote = text[lo:hi].strip()
    real_start = text.find(quote, lo)
    if real_start == -1:
        real_start = start
        quote = text[start:end]
    return quote, real_start, real_start + len(quote)


def _extract_warranty_mentions(text: str) -> list[dict]:
    mentions = []
    consumed = []
    for regex in (_WARRANTY_RE, _WARRANTY_REV_RE):
        for m in regex.finditer(text or ''):
            if any(s <= m.start() < e for s, e in consumed):
                continue
            num = _cn_to_int(m.group(1))
            unit = m.group(2).replace(' ', '')
            if num is None or num <= 0:
                continue
            months = num * 12 if '年' in unit else num
            quote, s_pos, e_pos = _extract_sentence_around(text, m.start(), m.end())
            consumed.append((m.start(), m.end()))
            mentions.append({
                'param': 'warranty_months',
                'label': '质保期',
                'value': months,
                'display': f"{months}个月",
                'quote': quote,
                'start': s_pos,
                'end': e_pos,
            })
    mentions.sort(key=lambda x: x['start'])
    return mentions


def _extract_response_time_mentions(text: str) -> list[dict]:
    mentions = []
    for m in _RESPONSE_TIME_RE.finditer(text or ''):
        num = _cn_to_int(m.group(1))
        unit = m.group(2)
        if num is None or num <= 0:
            continue
        minutes = num * 60 if '小时' in unit else num
        quote, s_pos, e_pos = _extract_sentence_around(text, m.start(), m.end())
        mentions.append({
            'param': 'response_minutes',
            'label': '故障响应时间',
            'value': minutes,
            'display': f"{minutes}分钟",
            'quote': quote,
            'start': s_pos,
            'end': e_pos,
        })
    return mentions


def _extract_retention_mentions(text: str) -> list[dict]:
    mentions = []
    for m in _RETENTION_RATIO_RE.finditer(text or ''):
        try:
            val = Decimal(m.group(1))
        except InvalidOperation:
            continue
        quote, s_pos, e_pos = _extract_sentence_around(text, m.start(), m.end())
        mentions.append({
            'param': 'retention_ratio',
            'label': '保证金比例',
            'value': val,
            'display': f"{val}%",
            'quote': quote,
            'start': s_pos,
            'end': e_pos,
        })
    return mentions


def _extract_total_price_mentions(text: str) -> list[dict]:
    mentions = []
    for m in _BID_TOTAL_MENTION_RE.finditer(text or ''):
        try:
            val = Decimal(m.group(1).replace(',', ''))
        except InvalidOperation:
            continue
        if m.group(2) == '万元':
            val = val * Decimal('10000')
        val = val.quantize(Decimal('0.01'))
        quote, s_pos, e_pos = _extract_sentence_around(text, m.start(), m.end())
        mentions.append({
            'param': 'bid_total_amount',
            'label': '投标总报价',
            'value': val,
            'display': f"{val}元",
            'quote': quote,
            'start': s_pos,
            'end': e_pos,
        })
    return mentions


def check_bid_self_contradictions(
    bid_text: str,
    bid_path: Optional[str | Path] = None,
    client=None,
) -> list[dict]:
    """扫描投标书内部前后承诺矛盾（质保期、总报价、响应时间、质保金比例 + LLM 语义冲突）。"""
    findings: list[dict] = []
    if not bid_text or not bid_text.strip():
        return findings

    bid_ctx = _build_unified_page_context(bid_path)

    extractors = (
        _extract_warranty_mentions,
        _extract_total_price_mentions,
        _extract_response_time_mentions,
        _extract_retention_mentions,
    )
    for ext_fn in extractors:
        mentions = ext_fn(bid_text)
        if len(mentions) < 2:
            continue
        first = mentions[0]
        for other in mentions[1:]:
            if other['value'] != first['value']:
                q1 = first['quote'] if verify_quote(first['quote'], bid_text) else ''
                q2 = other['quote'] if verify_quote(other['quote'], bid_text) else ''
                q_ok = bool(q1 and q2)
                p1, fp1, is_dx1 = _resolve_unified_page(first['start'], bid_ctx)
                p2, fp2, is_dx2 = _resolve_unified_page(other['start'], bid_ctx)
                findings.append({
                    'code': 'CONTRADICTION',
                    'severity': 'error',
                    'scope': 'bid_internal',
                    'param': first['param'],
                    'message': (
                        f"标书前后{first['label']}承诺矛盾：前文为 {first['display']}"
                        f"（「{q1}」），后文为 {other['display']}（「{q2}」）"
                    ),
                    'tender_quote': q1,
                    'bid_quote': q2,
                    'quote_verified': q_ok,
                    'tender_start': first['start'],
                    'tender_end': first['end'],
                    'tender_page': p1,
                    'tender_footer_page': fp1,
                    'tender_is_docx': is_dx1,
                    'bid_start': other['start'],
                    'bid_end': other['end'],
                    'bid_page': p2,
                    'bid_footer_page': fp2,
                    'bid_is_docx': is_dx2,
                    'footer_page': fp2 if p2 is not None else fp1,
                    'is_docx_page': is_dx2 if p2 is not None else is_dx1,
                })
                break

    if client is not None and not findings:
        warranty_paras = [
            (m.start(), _extract_sentence_around(bid_text, m.start(), m.end())[0])
            for m in re.finditer(r'(?:质保|保修|维保|售后服务)[^。\n]{6,80}[。\n]', bid_text)
        ]
        if len(warranty_paras) >= 2:
            pos_a, txt_a = warranty_paras[0]
            pos_b, txt_b = warranty_paras[-1]
            if txt_a != txt_b:
                prompt = _SELF_CONTRADICTION_PROMPT.format(
                    topic='质保与售后服务承诺', text_a=txt_a, text_b=txt_b
                )
                resp = client.chat([{'role': 'user', 'content': prompt}], model_tier='flash')
                parsed = _parse_llm_json(getattr(resp, 'content', '') or '')
                if parsed and parsed.get('is_contradiction') is True:
                    raw_qa = str(parsed.get('quote_a', '')).strip()
                    raw_qb = str(parsed.get('quote_b', '')).strip()
                    qa_ok = bool(raw_qa) and verify_quote(raw_qa, bid_text)
                    qb_ok = bool(raw_qb) and verify_quote(raw_qb, bid_text)
                    p1, fp1, is_dx1 = _resolve_unified_page(pos_a, bid_ctx)
                    p2, fp2, is_dx2 = _resolve_unified_page(pos_b, bid_ctx)
                    findings.append({
                        'code': 'CONTRADICTION',
                        'severity': 'error',
                        'scope': 'bid_internal',
                        'param': 'semantic_warranty',
                        'message': f"标书内部承诺语义矛盾：{parsed.get('reason', '前后表述冲突')}",
                        'tender_quote': raw_qa if qa_ok else '',
                        'bid_quote': raw_qb if qb_ok else '',
                        'quote_verified': (qa_ok and qb_ok),
                        'tender_start': pos_a,
                        'tender_end': pos_a + len(txt_a),
                        'tender_page': p1,
                        'tender_footer_page': fp1,
                        'tender_is_docx': is_dx1,
                        'bid_start': pos_b,
                        'bid_end': pos_b + len(txt_b),
                        'bid_page': p2,
                        'bid_footer_page': fp2,
                        'bid_is_docx': is_dx2,
                        'footer_page': fp2 if p2 is not None else fp1,
                        'is_docx_page': is_dx2 if p2 is not None else is_dx1,
                    })

    return findings


def _filter_valid_requirements(raw_reqs: list[dict]) -> list[dict]:
    """过滤掉 usage_note 与 table_marker，仅保留须逐条响应的 requirement 及模式 A/B 条款。"""
    valid = []
    for r in raw_reqs or []:
        kind = r.get('kind')
        if kind in ('usage_note', 'table_marker'):
            continue
        structured = r.get('structured')
        if isinstance(structured, dict) and structured.get('is_requirement') is False:
            continue
        valid.append(r)
    return valid


def check_star_responses(
    tender_text: str,
    bid_text: str,
    requirements: Optional[list[dict]] = None,
    client=None,
    tender_path: Optional[str | Path] = None,
    bid_path: Optional[str | Path] = None,
) -> dict[str, Any]:
    """执行招标 ★/▲ 要求 ↔ 投标响应逐条勾稽核验（公共签名与返回结构保持一字不变）。"""
    if requirements is None:
        marked = extract_marked_clauses(tender_text or '')
        wording = extract_invalid_bid_clauses(tender_text or '')
        requirements = marked + wording

    valid_reqs = _filter_valid_requirements(requirements)
    tender_ctx = _build_unified_page_context(tender_path)
    bid_ctx = _build_unified_page_context(bid_path)

    align_res = compare_contracts(tender_text or '', bid_text or '')
    _, tender_units = _build_clause_units(tender_text or '')
    _, bid_units = _build_clause_units(bid_text or '')
    align_map = align_res.get('alignment_map', {})

    findings: list[dict] = []
    results: list[dict] = []
    hit_count = 0
    direct_gate_hits = 0
    hybrid_gate_hits = 0

    for idx, req in enumerate(valid_reqs, start=1):
        expanded_req_text, r_start, r_end = _expand_req_text(tender_text, req)
        t_lo = max(0, r_start - _REQ_EXPAND_BEFORE)
        t_hi = min(len(tender_text), r_end + _REQ_EXPAND_AFTER) if tender_text else r_end
        tender_window = tender_text[t_lo:t_hi] if tender_text else expanded_req_text

        loc = _locate_bid_window_for_req(
            req_text=expanded_req_text,
            req_start=r_start,
            req_end=r_end,
            tender_units=tender_units,
            bid_units=bid_units,
            align_map=align_map,
            bid_text=bid_text,
            client=client,
        )

        t_page, t_footer, t_is_docx = _resolve_unified_page(r_start, tender_ctx)
        b_page, b_footer, b_is_docx = _resolve_unified_page(loc['start'], bid_ctx)

        raw_req_quote = (req.get('full_text') or req.get('text') or expanded_req_text).strip()
        if len(raw_req_quote) < 18 and len(expanded_req_text) > len(raw_req_quote):
            raw_req_quote = expanded_req_text
        if tender_text and not verify_quote(raw_req_quote, tender_text):
            raw_req_quote = expanded_req_text if verify_quote(expanded_req_text, tender_text) else ''

        if not loc['hit']:
            finding = {
                'code': 'MISSING_RESPONSE',
                'severity': 'error',
                'req_index': idx,
                'mark': req.get('mark', ''),
                'clause_no': req.get('clause_no', ''),
                'message': f"实质性要求漏应答：投标书中未找到对「{raw_req_quote[:60]}」的响应内容",
                'tender_quote': raw_req_quote,
                'bid_quote': '',
                'quote_verified': bool(raw_req_quote),
                'tender_start': r_start,
                'tender_end': r_end,
                'tender_page': t_page,
                'tender_footer_page': t_footer,
                'tender_is_docx': t_is_docx,
                'bid_start': None,
                'bid_end': None,
                'bid_page': None,
                'bid_footer_page': None,
                'bid_is_docx': False,
                'footer_page': t_footer,
                'is_docx_page': t_is_docx if t_page is not None else False,
                'llm_status': 'skipped' if client is None else 'confirmed_missing',
            }
            findings.append(finding)
            results.append({**finding, 'status': 'MISSING_RESPONSE'})
            continue

        hit_count += 1
        if loc.get('gate') == 'hybrid':
            hybrid_gate_hits += 1
        else:
            direct_gate_hits += 1

        bid_window = loc['window']
        default_bid_quote = loc['matched_quote']
        if bid_text and not verify_quote(default_bid_quote, bid_text):
            default_bid_quote = ''

        t_warranties = _extract_warranty_mentions(expanded_req_text)
        b_warranties = _extract_warranty_mentions(bid_window)
        det_contradiction = None
        if t_warranties and b_warranties:
            if t_warranties[0]['value'] != b_warranties[0]['value']:
                det_contradiction = (
                    f"质保期与招标要求冲突：招标要求 {t_warranties[0]['display']}，"
                    f"投标响应为 {b_warranties[0]['display']}"
                )

        if client is None:
            if det_contradiction:
                f_item = {
                    'code': 'CONTRADICTION',
                    'severity': 'error',
                    'req_index': idx,
                    'mark': req.get('mark', ''),
                    'clause_no': req.get('clause_no', ''),
                    'message': det_contradiction,
                    'tender_quote': raw_req_quote,
                    'bid_quote': default_bid_quote,
                    'quote_verified': bool(raw_req_quote and default_bid_quote),
                    'tender_start': r_start,
                    'tender_end': r_end,
                    'tender_page': t_page,
                    'tender_footer_page': t_footer,
                    'tender_is_docx': t_is_docx,
                    'bid_start': loc['start'],
                    'bid_end': loc['end'],
                    'bid_page': b_page,
                    'bid_footer_page': b_footer,
                    'bid_is_docx': b_is_docx,
                    'footer_page': b_footer if b_page is not None else t_footer,
                    'is_docx_page': b_is_docx if b_page is not None else t_is_docx,
                    'llm_status': 'skipped',
                }
                findings.append(f_item)
                results.append({**f_item, 'status': 'CONTRADICTION'})
            else:
                ev_warn_msg = _check_missing_evidence(
                    req_text=expanded_req_text,
                    bid_quote=default_bid_quote,
                    bid_window=bid_window,
                    bid_text=bid_text,
                )
                display_bid_quote = (
                    f"{default_bid_quote}{_EVIDENCE_WARNING_NOTE}"
                    if (ev_warn_msg and default_bid_quote)
                    else default_bid_quote
                )
                rec_ok = {
                    'status': 'RESPONSE_OK',
                    'req_index': idx,
                    'mark': req.get('mark', ''),
                    'clause_no': req.get('clause_no', ''),
                    'tender_quote': raw_req_quote,
                    'bid_quote': display_bid_quote,
                    'bid_quote_raw': default_bid_quote,
                    'quote_verified': bool(raw_req_quote and default_bid_quote),
                    'tender_start': r_start,
                    'tender_end': r_end,
                    'tender_page': t_page,
                    'tender_footer_page': t_footer,
                    'tender_is_docx': t_is_docx,
                    'bid_start': loc['start'],
                    'bid_end': loc['end'],
                    'bid_page': b_page,
                    'bid_footer_page': b_footer,
                    'bid_is_docx': b_is_docx,
                    'footer_page': b_footer if b_page is not None else t_footer,
                    'is_docx_page': b_is_docx if b_page is not None else t_is_docx,
                    'similarity': loc['score'],
                    'gate': loc.get('gate', 'direct'),
                    'evidence_warning': _EVIDENCE_WARNING_NOTE if ev_warn_msg else None,
                    'llm_status': 'skipped',
                }
                results.append(rec_ok)

                if ev_warn_msg:
                    findings.append({
                        'code': 'MISSING_EVIDENCE',
                        'severity': 'warning',
                        'req_index': idx,
                        'mark': req.get('mark', ''),
                        'clause_no': req.get('clause_no', ''),
                        'message': ev_warn_msg,
                        'tender_quote': raw_req_quote,
                        'bid_quote': default_bid_quote,
                        'quote_verified': bool(raw_req_quote and default_bid_quote),
                        'tender_start': r_start,
                        'tender_end': r_end,
                        'tender_page': t_page,
                        'tender_footer_page': t_footer,
                        'tender_is_docx': t_is_docx,
                        'bid_start': loc['start'],
                        'bid_end': loc['end'],
                        'bid_page': b_page,
                        'bid_footer_page': b_footer,
                        'bid_is_docx': b_is_docx,
                        'footer_page': b_footer if b_page is not None else t_footer,
                        'is_docx_page': b_is_docx if b_page is not None else t_is_docx,
                        'evidence_warning': _EVIDENCE_WARNING_NOTE,
                        'llm_status': 'skipped',
                    })
            continue

        # 大模型① 逐条判定
        prompt = _RESPONSE_CHECK_PROMPT.format(
            tender_window=tender_window,
            req_text=expanded_req_text,
            bid_window=bid_window,
        )
        resp = client.chat([{'role': 'user', 'content': prompt}], model_tier='flash')
        parsed = _parse_llm_json(getattr(resp, 'content', '') or '')

        status = (parsed or {}).get('status', 'RESPONSE_OK')
        if status not in ('RESPONSE_OK', 'MISSING_RESPONSE', 'CONTRADICTION'):
            status = 'RESPONSE_OK'

        llm_t_quote = str((parsed or {}).get('tender_quote', '')).strip()
        llm_b_quote = str((parsed or {}).get('bid_quote', '')).strip()
        reason = str((parsed or {}).get('reason', '')).strip()

        t_verified = bool(llm_t_quote) and verify_quote(llm_t_quote, tender_text)
        final_t_quote = llm_t_quote if t_verified else raw_req_quote
        t_ok = bool(final_t_quote) and verify_quote(final_t_quote, tender_text)
        if not t_ok:
            final_t_quote = ''

        if status == 'MISSING_RESPONSE':
            final_b_quote = ''
            b_ok = True
        else:
            b_ok = bool(llm_b_quote) and verify_quote(llm_b_quote, bid_text)
            final_b_quote = llm_b_quote if b_ok else ''

        quote_verified = (t_ok and b_ok)
        if status == 'MISSING_RESPONSE':
            b_real_start = None
            b_real_end = None
        else:
            found_b_pos = bid_text.find(final_b_quote) if (final_b_quote and bid_text) else -1
            b_real_start = found_b_pos if found_b_pos >= 0 else loc['start']
            b_real_end = (
                (b_real_start + len(final_b_quote))
                if (final_b_quote and found_b_pos >= 0 and b_real_start is not None)
                else loc['end']
            )

        b_real_page, b_real_footer, b_real_is_docx = _resolve_unified_page(b_real_start, bid_ctx)

        ev_warn_llm = None
        if status == 'RESPONSE_OK':
            ev_warn_llm = _check_missing_evidence(
                req_text=expanded_req_text,
                bid_quote=final_b_quote or default_bid_quote,
                bid_window=bid_window,
                bid_text=bid_text,
            )

        display_llm_b_quote = (
            f"{final_b_quote}{_EVIDENCE_WARNING_NOTE}"
            if (ev_warn_llm and final_b_quote)
            else final_b_quote
        )

        rec = {
            'status': status,
            'req_index': idx,
            'mark': req.get('mark', ''),
            'clause_no': req.get('clause_no', ''),
            'tender_quote': final_t_quote,
            'bid_quote': display_llm_b_quote,
            'bid_quote_raw': final_b_quote,
            'quote_verified': quote_verified,
            'tender_start': r_start,
            'tender_end': r_end,
            'tender_page': t_page,
            'tender_footer_page': t_footer,
            'tender_is_docx': t_is_docx,
            'bid_start': b_real_start,
            'bid_end': b_real_end,
            'bid_page': b_real_page,
            'bid_footer_page': b_real_footer,
            'bid_is_docx': b_real_is_docx,
            'footer_page': b_real_footer if b_real_page is not None else t_footer,
            'is_docx_page': b_real_is_docx if b_real_page is not None else t_is_docx,
            'reason': reason,
            'gate': loc.get('gate', 'direct'),
            'evidence_warning': _EVIDENCE_WARNING_NOTE if ev_warn_llm else None,
            'llm_status': 'done',
        }
        results.append(rec)

        if status in ('MISSING_RESPONSE', 'CONTRADICTION'):
            findings.append({
                'code': status,
                'severity': 'error',
                'req_index': idx,
                'mark': req.get('mark', ''),
                'clause_no': req.get('clause_no', ''),
                'message': reason or (
                    f"实质性条款漏应答：{final_t_quote[:50]}"
                    if status == 'MISSING_RESPONSE'
                    else f"响应内容与招标要求存在冲突：{reason}"
                ),
                'tender_quote': final_t_quote,
                'bid_quote': final_b_quote,
                'quote_verified': quote_verified,
                'tender_start': r_start,
                'tender_end': r_end,
                'tender_page': t_page,
                'tender_footer_page': t_footer,
                'tender_is_docx': t_is_docx,
                'bid_start': b_real_start,
                'bid_end': b_real_end,
                'bid_page': b_real_page,
                'bid_footer_page': b_real_footer,
                'bid_is_docx': b_real_is_docx,
                'footer_page': b_real_footer if b_real_page is not None else t_footer,
                'is_docx_page': b_real_is_docx if b_real_page is not None else t_is_docx,
            })
        elif ev_warn_llm:
            findings.append({
                'code': 'MISSING_EVIDENCE',
                'severity': 'warning',
                'req_index': idx,
                'mark': req.get('mark', ''),
                'clause_no': req.get('clause_no', ''),
                'message': ev_warn_llm,
                'tender_quote': final_t_quote,
                'bid_quote': final_b_quote,
                'quote_verified': quote_verified,
                'tender_start': r_start,
                'tender_end': r_end,
                'tender_page': t_page,
                'tender_footer_page': t_footer,
                'tender_is_docx': t_is_docx,
                'bid_start': b_real_start,
                'bid_end': b_real_end,
                'bid_page': b_real_page,
                'bid_footer_page': b_real_footer,
                'bid_is_docx': b_real_is_docx,
                'footer_page': b_real_footer if b_real_page is not None else t_footer,
                'is_docx_page': b_real_is_docx if b_real_page is not None else t_is_docx,
                'evidence_warning': _EVIDENCE_WARNING_NOTE,
            })

    total_reqs = len(valid_reqs)

    # ── E2 验收修正：混合命中跨需求污染降级 ─────────────────────────────────
    direct_spans = [
        (int(r['bid_start']), int(r['bid_end']))
        for r in results
        if r.get('gate') == 'direct' and r.get('bid_start') is not None
    ]
    downgraded: list[dict] = []
    for r in results:
        if r.get('gate') != 'hybrid' or r.get('bid_start') is None:
            continue
        h_lo, h_hi = int(r['bid_start']), int(r['bid_end'])
        span_len = max(1, h_hi - h_lo)
        contaminated = any(
            max(h_lo, d_lo) < min(h_hi, d_hi)
            and (min(h_hi, d_hi) - max(h_lo, d_lo)) / span_len > 0.3
            for d_lo, d_hi in direct_spans
        )
        if contaminated:
            downgraded.append(r)
    for r in downgraded:
        r['status'] = 'MISSING_RESPONSE'
        r['gate'] = 'hybrid_rejected'
        r['bid_quote'] = ''
        r['bid_quote_raw'] = ''
        r['evidence_warning'] = None
        r.pop('quote_verified', None)
        hit_count -= 1
        hybrid_gate_hits -= 1
        findings.append({
            'code': 'MISSING_RESPONSE',
            'severity': 'error',
            'req_index': r.get('req_index'),
            'mark': r.get('mark', ''),
            'clause_no': r.get('clause_no', ''),
            'message': (
                f"实质性要求漏应答：混合检索命中区间与其他条款的响应原文重叠"
                f"（疑似跨需求污染），判定未响应「{str(r.get('tender_quote', ''))[:60]}」"
            ),
            'tender_quote': r.get('tender_quote', ''),
            'bid_quote': '',
            'quote_verified': False,
            'tender_start': r.get('tender_start'),
            'tender_end': r.get('tender_end'),
            'tender_page': r.get('tender_page'),
            'tender_footer_page': r.get('tender_footer_page'),
            'tender_is_docx': r.get('tender_is_docx', False),
            'bid_start': r.get('bid_start'),
            'bid_end': r.get('bid_end'),
            'bid_page': r.get('bid_page'),
            'bid_footer_page': r.get('bid_footer_page'),
            'bid_is_docx': r.get('bid_is_docx', False),
            'footer_page': r.get('tender_footer_page'),
            'is_docx_page': r.get('tender_is_docx', False),
            'llm_status': r.get('llm_status', 'skipped'),
        })

    hit_rate = round(hit_count / total_reqs, 4) if total_reqs > 0 else 1.0

    return {
        'findings': findings,
        'results': results,
        'alignment_summary': {
            'total_requirements': total_reqs,
            'aligned_hits': hit_count,
            'hit_rate': hit_rate,
            'direct_gate_hits': direct_gate_hits,
            'hybrid_gate_hits': hybrid_gate_hits,
        },
    }


def run_response_check(
    tender_path: str | Path,
    bid_path: str | Path,
    requirements: Optional[list[dict]] = None,
    client=None,
) -> dict[str, Any]:
    """F4 端到端主入口：双书 ★/▲ 条款逐条响应核验 + 投标书内部前后矛盾检测。"""
    tender_doc = parse_document(str(tender_path))
    bid_doc = parse_document(str(bid_path))

    if requirements is None:
        checklist = build_review_checklist(str(tender_path))
        requirements = checklist['marked_clauses'] + checklist['invalid_bid_clauses']

    star_out = check_star_responses(
        tender_text=tender_doc.text,
        bid_text=bid_doc.text,
        requirements=requirements,
        client=client,
        tender_path=tender_path,
        bid_path=bid_path,
    )

    internal_contradictions = check_bid_self_contradictions(
        bid_text=bid_doc.text,
        bid_path=bid_path,
        client=client,
    )

    all_findings = list(star_out['findings']) + list(internal_contradictions)
    return {
        'findings': all_findings,
        'star_results': star_out['results'],
        'self_contradictions': internal_contradictions,
        'alignment_summary': star_out['alignment_summary'],
    }


def dry_run_corpus_alignment(corpus_dir: str | Path = 'data/corpus/m0_samples') -> list[dict]:
    """11 份招标文件确定性对齐链路试跑（0 Token）。"""
    c_dir = Path(corpus_dir)
    if not c_dir.exists():
        return []
    files = sorted([p for p in c_dir.iterdir() if p.suffix.lower() in ('.docx', '.pdf')])
    report = []
    for f in files:
        checklist = build_review_checklist(str(f))
        marked_reqs = [c for c in checklist['marked_clauses'] if c.get('kind') == 'requirement']
        wording_reqs = checklist['invalid_bid_clauses']
        all_reqs = marked_reqs + wording_reqs

        doc = parse_document(str(f))
        out = check_star_responses(
            tender_text=doc.text,
            bid_text=doc.text,
            requirements=all_reqs,
            client=None,
            tender_path=f,
            bid_path=f,
        )
        summary = out['alignment_summary']
        report.append({
            'file': f.name,
            'star_triangle_count': len(marked_reqs),
            'wording_ab_count': len(wording_reqs),
            'total_requirements': summary['total_requirements'],
            'aligned_hits': summary['aligned_hits'],
            'hit_rate': summary['hit_rate'],
        })
    return report


if __name__ == '__main__':
    rows = dry_run_corpus_alignment()
    print(f"=== F4 确定性对齐链路试跑报告（共 {len(rows)} 份语料，0 Token）===")
    for r in rows:
        print(
            f"- {r['file']}: ★/▲要求={r['star_triangle_count']} | "
            f"模式A/B={r['wording_ab_count']} | 总要求={r['total_requirements']} | "
            f"对齐命中={r['aligned_hits']}/{r['total_requirements']} ({r['hit_rate']*100:.1f}%)"
        )