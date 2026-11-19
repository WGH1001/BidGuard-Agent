# -*- coding: utf-8 -*-
"""E42 · 标书评分预估与提分建议引擎（0-Token 确定性底座 + 局部窗口受控 LLM 评估 + Word 诊断报告导出）。

核心铁律（AGENTS.md 铁律 1/2/3/4 & E42 卡）：
1. 全程 Decimal 运算（ROUND_HALF_UP 量化 0.01 / 0.1），严禁 float 参与分值加总与钳位；
   单条预估分强制钳位 `0.00 <= estimated_score <= max_score`；
2. 价格分（price_formula）诚实标为 `status='pending_open_bid'`、`estimated_score=None`，
   严禁捏造竞争对手开标报价或评标基准价；
3. 一票否决风险先导联动：自动汇聚 F2（超限价/算术错）、F3（证照过期/串名）、F4（★实质负偏离）
   与 E43（`source_module='BLIND_BID'` / `BLIND_BID_*` 暗标违规）的 `consequence='veto'` 发现；
4. 所有投标正文摘录强制经 `quote_verifier.verify_quote()` 核验，未过核验一律清空 `quote=''` 并标 `quote_verified=False`；
5. 0-Token 离线降级（`client=None`）或单条模型异常时，主观项与模糊扣分项诚实标为 `status='need_llm'`
   并附 `status_reason`（`'no_llm_client'|'llm_call_failed'|'ambiguous_deduction_rule'`），绝不阻断整份报告；
6. 强免责声明：前端与导出 Word 报告封面/页眉均固定标注「AI 质量自评预估（非最终评委实际打分）」。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path
import re
from typing import Any, Callable, Literal, Optional

from docx import Document
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from core.bid_composer import (
    _extract_pdf_scoring_items,
    _is_procedural_topic,
)
from core.hybrid_retriever import hybrid_retrieve_windows
from core.parser import parse_document
from core.price_extractor import run_price_check
from core.quote_verifier import normalize_zh, verify_quote
from core.response_checker import (
    build_unified_page_context,
    resolve_unified_page,
)
from core.tender_extractor import build_review_checklist

SCORE_DISCLAIMER_TEXT = 'AI 质量自评预估（非最终评委实际打分）'

CriterionCategory = Literal['price', 'business', 'technical', 'service', 'qualification']
EvaluationMode = Literal['price_formula', 'objective_deduction', 'subjective_quality']
ItemScoreStatus = Literal['evaluated', 'pending_open_bid', 'veto_risk', 'need_llm']

_DEC_ZERO = Decimal('0.00')
_DEC_HUNDRED = Decimal('100.00')
_QUANT_01 = Decimal('0.1')
_QUANT_001 = Decimal('0.01')

_SCORE_NUM_RE = re.compile(
    r'(?:满分|分值|最高|得|占)?\s*(?:\d+(?:\.\d+)?\s*[-~—至]\s*)?(\d{1,3}(?:\.\d{1,2})?)\s*分?'
)
_PROSE_ITEM_LINE_RE = re.compile(
    r'^(?:[（(]?[一二三四五六七八九十\d]{1,3}[）)、.．]\s*)?'
    r'([^\n：:（(。；;]{2,32}?)\s*[（(]\s*(?:满分|分值)?\s*(\d{1,2}(?:\.\d{1,2})?)\s*分\s*[）)]'
    r'\s*[：:]?\s*(.*)$'
)
_PAREN_SCORE_ONLY_RE = re.compile(
    r'^([^\n：:（(。；;]{0,14}?)\s*[（(]\s*(?:满分|分值)?\s*(\d{1,2}(?:\.\d{1,2})?)\s*分\s*[）)]\s*$'
)
_STANDALONE_SCORE_LINE_RE = re.compile(
    r'^(?:\d+(?:\.\d+)?\s*[-~—至]\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?)\s*分$'
)
_PAGE_NOISE_LINE_RE = re.compile(
    r'^(?:第\s*\d+\s*页\s*共\s*\d+\s*页|\d+\s*$|[0-9a-f]{6,}-[0-9a-f\-]+$|安徽省政府采购项目公开招标文件.*$)'
)
_TABLE_HEADER_STOP_WORDS = (
    '序号', '条款序号', '评分因素', '评审项目', '评审因素', '评价指标',
    '评标因素', '分值', '评分细则', '计分标准', '评审标准', '评标标准',
)
_PRICE_PROSE_SCORE_RE = re.compile(
    r'(?:'
    r'(?:价格分|投标报价得分|报价分|价格部分|报价部分|价格评审)[^\n。；;]{0,28}?(\d{1,2}(?:\.\d{1,2})?)\s*分'
    r'|'
    r'(?:^|\n)\s*(?:\d+\s*\n\s*)?(?:价格|报价)\s*(?:\n\s*)?[（(]\s*(?:满分|分值)?\s*(\d{1,2}(?:\.\d{1,2})?)\s*分\s*[）)]'
    r'|'
    r'(?:有效价格得分|价格得分|报价得分)\s*[=＝]\s*[^\n。；;]{0,55}[×xX\*]\s*(\d{1,2}(?:\.\d{1,2})?)'
    r')'
)
_DECLARED_TOTAL_RE = re.compile(
    r'(?:总分(?:为|满分)?|满分(?:为)?|评分总分|合计)\s*(\d{2,3}(?:\.\d{1,2})?)\s*分'
)

# 确定性客观扣分规则：如“每有一项不满足扣 2 分”、“每缺1项扣0.5分”
_DEDUCTION_PER_ITEM_RE = re.compile(
    r'每\s*(?:有\s*)?(?:[一1]|壹)\s*(?:项|条|处|个|点)?\s*'
    r'(?:不满足|负偏离|偏离|未响应|缺漏|不符合|未达到)[^。；;\n]{0,20}?'
    r'扣\s*(\d+(?:\.\d+)?)\s*分'
)
_AMBIGUOUS_DEDUCTION_SIGNAL_RE = re.compile(r'扣\s*(?:\d+(?:\.\d+)?\s*)?分|酌情扣|按比例扣')

# E44 §2.1：按规范证书同义词组匹配（支持扫描图 OCR 中的 GB/T19001 / ISO9001 等写法，同时覆盖三体系）
_CERT_SYNONYM_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ('质量管理体系', ('质量管理体系', 'iso9001', 'iso 9001', 'gb/t19001', 'gbt19001')),
    ('环境管理体系', ('环境管理体系', 'iso14001', 'iso 14001', 'gb/t24001', 'gbt24001')),
    (
        '职业健康安全管理体系',
        ('职业健康安全管理体系', '职业安全健康管理体系', 'iso45001', 'iso 45001', 'gb/t45001', 'ohsas18001'),
    ),
    ('信息安全管理体系', ('信息安全管理体系', 'iso27001', 'iso/iec27001', 'iso/iec 27001')),
    ('信息技术服务管理体系', ('信息技术服务管理体系', 'iso20000', 'iso/iec20000', 'itss')),
    ('CMMI', ('cmmi',)),
    ('CCRC', ('ccrc',)),
    ('高新技术企业', ('高新技术企业',)),
    ('软件著作权', ('软件著作权', '软著')),
    ('发明专利', ('发明专利',)),
    ('信息系统建设和服务能力', ('信息系统建设和服务能力', 'cs1', 'cs2', 'cs3', 'cs4')),
)

_CERT_KEYWORDS = tuple(
    alias for _canon, aliases in _CERT_SYNONYM_GROUPS for alias in aliases
)

_LEADING_SECTION_PREFIX_RE = re.compile(
    r'^(?:[（(]?[一二三四五六七八九十\d]{1,2}[）)、.．\s]*)?'
    r'(商务要求|商务部分|商务评分|技术要求|技术部分|技术评分|服务方案|政策分|资信分|综合实力|价格部分)'
    r'\s*[（(]\s*\d+(?:\.\d+)?\s*分\s*[）)]\s*(.+)$'
)
_LEADING_BROKEN_PAREN_RE = re.compile(r'^[^（(]{1,42}[）)]\s*(.{2,40})$')
_TOC_LINE_RE = re.compile(
    r'(?:\t\s*\d{1,4}\s*$|[\.·…]{3,}\s*\d{1,4}\s*$|'
    r'^\s*(?:第[一二三四五六七八九十\d]+[章部分节]|(?:\d+(?:\.\d+)*|[一二三四五六七八九十]+)[、.．\s])\s*[^\n]{2,36}\s+\d{1,4}\s*$)'
)
_GROUP_TOTAL_HEADER_NAMES = frozenset({
    '技术', '技术部分', '技术要求', '技术分', '商务', '商务要求', '商务部分', '商务分',
    '政策分', '政策', '服务方案', '综合评审', '资信分', '价格部分',
})


def _to_dec(val: Any, default: Decimal = _DEC_ZERO) -> Decimal:
    if isinstance(val, Decimal):
        return val.quantize(_QUANT_001, rounding=ROUND_HALF_UP)
    if val is None:
        return default
    s = str(val).strip().replace(',', '')
    if not s:
        return default
    try:
        return Decimal(s).quantize(_QUANT_001, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        m = _SCORE_NUM_RE.search(s)
        if m:
            try:
                return Decimal(m.group(1)).quantize(_QUANT_001, rounding=ROUND_HALF_UP)
            except (InvalidOperation, ValueError):
                return default
        return default


def _fmt_dec(val: Optional[Decimal]) -> Optional[str]:
    if val is None:
        return None
    return str(val.quantize(_QUANT_001, rounding=ROUND_HALF_UP))


def _obj_field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _clean_factor_title(raw_factor: str, section: str = '') -> tuple[str, str]:
    """E44 §2.1：清洗 PDF 跨行抽取带来的前导规则尾碎片与大类汇总前缀，保留大类到 section。"""
    fac = re.sub(r'\s+', ' ', str(raw_factor or '').strip())
    sec = re.sub(r'\s+', ' ', str(section or '').strip())

    # 1. 剥离如 "商务要求（13 分）相关业绩" / "政策分（2 分）节能环保"
    m_sec = _LEADING_SECTION_PREFIX_RE.match(fac)
    if m_sec:
        extracted_sec = m_sec.group(1).strip()
        rest = m_sec.group(2).strip()
        if len(rest) >= 2:
            if not sec or sec in ('PDF评分细则', '评分细则', '综合评审', 'PDF综合评分表'):
                sec = extracted_sec
            fac = rest

    # 2. 剥离前导残缺右括号碎片，如 "容清单、签署页，复印件加盖公章）企业管理体系" / "核验）综合实力"
    m_broken = _LEADING_BROKEN_PAREN_RE.match(fac)
    if m_broken:
        right = m_broken.group(1).strip()
        if len(right) >= 2:
            fac = right

    fac = fac.strip(' ：:、，,。；;）)')
    return fac, sec or '评分细则'


def _extract_price_score_from_text(text: str, eval_start: Optional[int] = None) -> Optional[tuple[Decimal, str]]:
    """从招标文件评标章或全文抽取价格分满分（兼容跨行 `价格\\n（30 分）` 与 `×30` 计分公式）。"""
    if not text:
        return None
    segments: list[str] = []
    if eval_start is not None and 0 <= eval_start < len(text):
        segments.append(text[eval_start:eval_start + 15000])
    m_eval = re.search(r'(?:评分细则|评分标准|评分办法|详细评审|综合评审)', text)
    if m_eval:
        segments.append(text[m_eval.start():m_eval.start() + 12000])
    segments.append(text)

    for seg in segments:
        for m in _PRICE_PROSE_SCORE_RE.finditer(seg):
            raw_num = m.group(1) or m.group(2) or m.group(3)
            if not raw_num:
                continue
            p_sc = _to_dec(raw_num, _DEC_ZERO)
            if _DEC_ZERO < p_sc <= Decimal('60.00'):
                ctx_lo = max(0, m.start() - 10)
                ctx_hi = min(len(seg), m.end() + 100)
                rule_snippet = re.sub(r'\s+', ' ', seg[ctx_lo:ctx_hi]).strip()
                return p_sc, rule_snippet[:240]
    return None


def _classify_criterion(factor: str, section: str, rule_text: str) -> tuple[CriterionCategory, EvaluationMode]:
    merged = f"{section} {factor} {rule_text}"
    head = f"{section} {factor}"
    if any(k in head for k in ('价格', '报价', '评标价', '投标总价')):
        return 'price', 'price_formula'

    # 优先依据标题/大类判别类别，避免“服务体系”误撞 qualification 或“合同签订”误将进度计划判为 business
    if any(k in head for k in ('售后', '运维', '培训', '质保', '保修', '驻场', '应急', '服务')):
        cat: CriterionCategory = 'service'
    elif any(k in head for k in ('进度', '工期', '实施', '技术', '架构', '系统', '安全', '需求', '响应程度', '总体方案')):
        cat = 'technical'
    elif any(k in head for k in ('证书', '认证', '资质', '管理体系', 'ISO', 'CMMI', 'ITSS', 'CCRC', '软著', '著作权', '专利', '节能', '环保', '政策')):
        cat = 'qualification'
    elif any(k in head for k in ('业绩', '案例', '同类项目', '类似项目', '信誉', '财务', '商务')):
        cat = 'business'
    elif any(k in merged for k in ('认证证书', '管理体系认证', '资质证书', 'ISO', 'CMMI', 'ITSS', 'CCRC', '软著', '著作权', '专利')):
        cat = 'qualification'
    elif any(k in merged for k in ('类似业绩', '同类项目', '类似项目', '业绩', '成功案例', '信誉', '财务')):
        cat = 'business'
    elif any(k in merged for k in ('售后', '运维', '培训', '质保', '保修', '驻场', '应急', '响应时间')):
        cat = 'service'
    else:
        cat = 'technical'

    if (
        _DEDUCTION_PER_ITEM_RE.search(merged)
        or _AMBIGUOUS_DEDUCTION_SIGNAL_RE.search(merged)
        or any(k in head for k in ('技术参数响应', '偏离情况', '实质性响应', '指标响应', '需求的响应程度'))
        or (cat == 'qualification' and any(ck in merged.lower() for ck in _CERT_KEYWORDS))
    ):
        return cat, 'objective_deduction'

    return cat, 'subjective_quality'


def _extract_prose_line_criteria(text: str) -> list[dict[str, Any]]:
    """从 PDF / 段落型评分办法文本中提取两类典型排版评分项：
    1) 标题带括号分值型（如亳州 PDF：`实施方案（5 分）` 或断行 `技术参数响应\\n情况（51 分）`），自动跳过无正文的大类汇总行（如 `服务方案（85 分）`）；
    2) 三列表格分值殿后型（如滁州 PDF：`1.需求分析方\\n案` + 多行标准 + 末尾独立行 `0-3 分`）及分值居中型（`价格\\n10 分\\n计分标准...`）。
    """
    out: list[dict[str, Any]] = []
    if not text:
        return out

    # 若文本含“资格性检查/符合性检查”，优先切到“综合评审/评分细则/详细评审”之后再抽分值项
    start_m = re.search(r'(?:综合评审|评分细则|评分标准|评分规则|详细评审|评标方法和标准)', text)
    work_text = text[start_m.start():] if start_m else text

    raw_lines = [ln.strip() for ln in work_text.splitlines() if ln.strip()]
    lines = [ln for ln in raw_lines if not _PAGE_NOISE_LINE_RE.match(ln)]

    # ── 模式 A：标题带 `（X 分）`（支持标题跨 2 行断行 + 自动过滤紧邻子标题的大类总分头）──
    header_hits: list[tuple[int, int, str, str, str]] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        # 跳过子项内嵌说明如 "1.项目经理（1 人，满分2 分）" 当其位于大项内部时
        m_one = _PROSE_ITEM_LINE_RE.match(ln)
        if m_one and not re.search(r'[，,]\s*满分', ln):
            f_title = m_one.group(1).strip()
            sc_str = m_one.group(2).strip()
            inline_det = m_one.group(3).strip()
            header_hits.append((i, i + 1, f_title, sc_str, inline_det))
            i += 1
            continue
        if i + 1 < len(lines):
            m_two = _PAREN_SCORE_ONLY_RE.match(lines[i + 1])
            if (
                m_two
                and 2 <= len(ln) <= 16
                and not any(ch in ln for ch in ('。', '；', '：', ':'))
                and not _STANDALONE_SCORE_LINE_RE.match(ln)
            ):
                f_title = f"{ln}{m_two.group(1).strip()}".strip()
                sc_str = m_two.group(2).strip()
                header_hits.append((i, i + 2, f_title, sc_str, ''))
                i += 2
                continue
        i += 1

    current_section = '综合评审'
    # 同时检查在第一个括号标题前是否有 `价格 \n 10 分`
    first_hdr_line = header_hits[0][0] if header_hits else len(lines)
    for k in range(min(first_hdr_line, 25)):
        if _STANDALONE_SCORE_LINE_RE.match(lines[k]) and k >= 1:
            prev_ln = lines[k - 1]
            if '价格' in prev_ln or '报价' in prev_ln:
                det_lns = []
                for fwd in range(k + 1, min(first_hdr_line, k + 8)):
                    det_lns.append(lines[fwd])
                out.append({
                    'section': '价格评审',
                    'factor': prev_ln,
                    'score': lines[k],
                    'detail': ' '.join(det_lns) or prev_ln,
                })
                break

    for h_idx, (st_line, end_line, f_title, sc_str, inline_det) in enumerate(header_hits):
        next_st = header_hits[h_idx + 1][0] if h_idx + 1 < len(header_hits) else min(len(lines), end_line + 18)
        body_lines = [lines[p] for p in range(end_line, next_st) if lines[p]]
        if inline_det:
            body_lines.insert(0, inline_det)
        # 若两个 `（X 分）` 标题紧挨着且中间无实质规则描述，说明前一个是“服务方案（85分）”这类大类章节头
        if not body_lines and h_idx + 1 < len(header_hits):
            current_section = f_title
            continue
        out.append({
            'section': current_section,
            'factor': f_title,
            'score': sc_str,
            'detail': ' '.join(body_lines[:10]) or f_title,
        })

    if len(out) >= 3:
        return out

    # ── 模式 B：三列表格分值殿后型（如滁州 PDF：`1.需求分析方\n案` + 正文 + `0-3 分`）──
    block_items: list[dict[str, Any]] = []
    block_start = 0
    for idx, ln in enumerate(lines):
        if not _STANDALONE_SCORE_LINE_RE.match(ln):
            continue
        score_str = ln
        seg = lines[block_start:idx]
        block_start = idx + 1
        if not seg:
            continue
        # 过滤表头词与大类分值头
        cleaned_seg = [
            s for s in seg
            if not any(w == s or s.startswith(w) for w in _TABLE_HEADER_STOP_WORDS)
            and not _PAREN_SCORE_ONLY_RE.match(s)
        ]
        if not cleaned_seg:
            continue
        # 若紧邻上一行就是简短标题（如 `价格 \n 10 分`）
        if len(cleaned_seg[-1]) <= 14 and not any(c in cleaned_seg[-1] for c in ('。', '；', '，', '：')):
            f_title = cleaned_seg[-1]
            detail_txt = ' '.join(cleaned_seg[:-1]) or f_title
        else:
            # 从段首吸附 1~3 行短标题（如 `2.投标人履约\n能力` 或 `1.需求分析方\n案`）
            title_parts: list[str] = []
            det_start = 0
            for s_i, s_ln in enumerate(cleaned_seg[:4]):
                if (
                    len(s_ln) <= 14
                    and not any(c in s_ln for c in ('。', '；', '，', '：', '（1）', '①', '②'))
                ):
                    title_parts.append(s_ln)
                    det_start = s_i + 1
                    if len(''.join(title_parts)) >= 6:
                        break
                else:
                    break
            f_title = ''.join(title_parts).strip()
            detail_txt = ' '.join(cleaned_seg[det_start:det_start + 10]).strip()
        if len(f_title) >= 2:
            block_items.append({
                'section': 'PDF综合评分表',
                'factor': f_title,
                'score': score_str,
                'detail': detail_txt or f_title,
            })

    return block_items if len(block_items) >= len(out) else out


def collect_scoring_criteria_items(
    tender_path: str | Path,
    *,
    f1_res: Optional[dict[str, Any]] = None,
    client: Any = None,
    tender_parsed_doc: Any = None,
) -> dict[str, Any]:
    """E42 §2.1.1：统一整合 .docx 评分表、段落型评分项与 PDF 评分细则（含价格分）。"""
    t_path = Path(tender_path)
    if tender_parsed_doc is not None and hasattr(tender_parsed_doc, 'text'):
        tender_text = str(tender_parsed_doc.text or '')
    elif t_path.is_file():
        tender_text = parse_document(str(t_path), ocr_images=False).text
    else:
        tender_text = ''

    if f1_res is None and t_path.is_file():
        f1_res = build_review_checklist(str(t_path))
    checklist = f1_res or {}

    raw_candidates: list[dict[str, Any]] = []

    # 1. 优先消费 F1 表格型评分细则
    for st in checklist.get('scoring_tables') or []:
        raw_candidates.append({
            'section': str(st.get('section') or st.get('type') or '评分细则').strip(),
            'factor': str(st.get('factor') or '').strip(),
            'score_raw': st.get('score_max') if st.get('score_max') is not None else st.get('score'),
            'detail': str(st.get('detail') or '').strip(),
            'source': 'scoring_table',
        })

    # 2. 融合 F1 段落型评分细则（若有）
    for sp in checklist.get('scoring_prose') or []:
        raw_candidates.append({
            'section': str(sp.get('section') or sp.get('category') or '段落评分细则').strip(),
            'factor': str(sp.get('factor') or sp.get('item') or sp.get('criterion') or sp.get('title') or '').strip(),
            'score_raw': sp.get('score_max') or sp.get('score') or sp.get('weight') or sp.get('points'),
            'detail': str(sp.get('detail') or sp.get('rule') or sp.get('quote') or '').strip(),
            'source': 'scoring_prose',
        })

    # 3. 若不足 2 项，从 PDF 评分摘录（include_price=True）与评标章节正文补充抽取
    if len(raw_candidates) < 2:
        excerpts = [
            str(sh.get('excerpt') or '')
            for sh in (checklist.get('scoring_hits') or [])
            if sh.get('excerpt')
        ]
        eval_ch = checklist.get('eval_chapter')
        if isinstance(eval_ch, dict) and eval_ch.get('start') is not None and tender_text:
            s_off = max(0, int(eval_ch['start']))
            excerpts.append(tender_text[s_off:s_off + 15000])
        elif tender_text:
            excerpts.append(tender_text[:20000])

        pdf_items = _extract_pdf_scoring_items(excerpts, include_price=True)
        for f_title, desc, sc_str in pdf_items:
            raw_candidates.append({
                'section': 'PDF评分细则',
                'factor': f_title,
                'score_raw': sc_str,
                'detail': desc,
                'source': 'pdf_scoring_extract',
            })

        if len(raw_candidates) < 3:
            for ex in excerpts:
                for p_item in _extract_prose_line_criteria(ex):
                    raw_candidates.append({
                        'section': p_item['section'],
                        'factor': p_item['factor'],
                        'score_raw': p_item['score'],
                        'detail': p_item['detail'],
                        'source': 'prose_line_extract',
                    })

    # 清洗、去重、量化 max_score
    criteria: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    has_price_item = False

    for cand in raw_candidates:
        factor_raw = re.sub(r'\s+', ' ', str(cand.get('factor') or '').strip())
        detail = re.sub(r'\s+', ' ', str(cand.get('detail') or '').strip())
        section_raw = re.sub(r'\s+', ' ', str(cand.get('section') or '评分细则').strip())
        factor, section = _clean_factor_title(factor_raw, section_raw)
        if not factor and detail:
            factor = detail[:28]
        if not factor:
            continue
        if factor in ('合计', '总分', '总计', '小计', '满分', '评分因素', '评审因素', '评分项'):
            continue

        max_sc = _to_dec(cand.get('score_raw'), _DEC_ZERO)
        if max_sc <= _DEC_ZERO or max_sc > _DEC_HUNDRED:
            # 尝试从 factor 或 detail 尾部抓取分值
            m_fb = _SCORE_NUM_RE.search(f"{factor} {detail[:60]}")
            if m_fb:
                max_sc = _to_dec(m_fb.group(1), _DEC_ZERO)
        if max_sc <= _DEC_ZERO or max_sc > _DEC_HUNDRED:
            continue

        cat, eval_mode = _classify_criterion(factor, section, detail)
        if cat != 'price' and _is_procedural_topic(factor) and not any(
            k in factor for k in ('方案', '技术', '服务', '售后', '业绩', '认证', '证书', '实施', '运维', '架构', '响应', '安全', '培训', '体系', '进度', '节能', '环保')
        ):
            continue

        dedup_key = f"{normalize_zh(factor)[:24]}_{max_sc}"
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        if cat == 'price':
            has_price_item = True

        criteria.append({
            'criterion_id': f"crit_{len(criteria) + 1}",
            'category': cat,
            'section': section,
            'factor': factor[:80],
            'max_score': max_sc,
            'scoring_rule_text': (detail or factor)[:600],
            'evaluation_mode': eval_mode,
            'source': cand.get('source', 'scoring_table'),
        })

    # 若 PDF/段落中仅抽出了技术商务项而价格分写在正文/跨行表头中，精准补齐价格分项
    if criteria and not has_price_item and tender_text:
        eval_ch_obj = checklist.get('eval_chapter')
        eval_start_off = (
            int(eval_ch_obj['start'])
            if isinstance(eval_ch_obj, dict) and eval_ch_obj.get('start') is not None
            else None
        )
        price_hit = _extract_price_score_from_text(tender_text, eval_start=eval_start_off)
        if price_hit is not None:
            p_sc, p_rule = price_hit
            criteria.insert(0, {
                'criterion_id': 'crit_price_0',
                'category': 'price',
                'section': '价格评审',
                'factor': '价格分',
                'max_score': p_sc,
                'scoring_rule_text': p_rule,
                'evaluation_mode': 'price_formula',
                'source': 'prose_price_extract',
            })
            for idx, it in enumerate(criteria, start=1):
                it['criterion_id'] = f"crit_{idx}"

    declared_total = _DEC_HUNDRED
    if tender_text:
        m_tot = _DECLARED_TOTAL_RE.search(tender_text)
        if m_tot:
            cand_tot = _to_dec(m_tot.group(1), _DEC_HUNDRED)
            if Decimal('50.00') <= cand_tot <= Decimal('1000.00'):
                declared_total = cand_tot

    items_sum = sum((it['max_score'] for it in criteria), _DEC_ZERO).quantize(_QUANT_001, rounding=ROUND_HALF_UP)
    score_scale_warning: Optional[str] = None
    if not criteria:
        score_scale_warning = '未在招标文件中自动识别到结构化评分细则分值，请确认本项目是否采用最低评标价法或扫描件未清晰识别。'
    elif items_sum != declared_total:
        score_scale_warning = (
            f"提取到的评分项满分合计为 {items_sum} 分，与招标总分基准 {declared_total} 分不一致"
            "（可能存在段落跨窗超计或大项小计并存），预估分仅供参考，请结合招标原表核对。"
        )

    return {
        'declared_total_max': declared_total,
        'items_sum': items_sum,
        'score_scale_warning': score_scale_warning,
        'criteria': criteria,
    }


def _collect_veto_alerts(
    *,
    f2_res: Optional[dict[str, Any]],
    f3_findings: Optional[list[Any]],
    f4_findings: Optional[list[Any]],
    blind_bid_findings: Optional[list[Any]],
) -> list[dict[str, Any]]:
    """汇总 F2 / F3 / F4 / E43 暗标的一票否决（consequence='veto'）阻断告警。"""
    alerts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def _push(src_mod: str, code: str, sev: str, clause_no: str, msg: str, sug: str, conseq: str) -> None:
        is_blind = src_mod == 'BLIND_BID' or code.startswith('BLIND_BID_')
        if is_blind:
            src_mod = 'BLIND_BID'
        if conseq != 'veto' and not (src_mod in ('F2', 'F3', 'BLIND_BID') and sev == 'error'):
            return
        k = (src_mod, code, msg[:80])
        if k in seen:
            return
        seen.add(k)
        alerts.append({
            'source_module': src_mod,
            'code': code,
            'severity': sev or 'error',
            'clause_no': clause_no or '全文核查',
            'message': msg,
            'suggestion': sug or '请优先整改消除该一票否决废标风险',
            'consequence': 'veto',
        })

    if isinstance(f2_res, dict):
        for pf in f2_res.get('findings') or []:
            sev = str(_obj_field(pf, 'severity', 'error') or 'error')
            if sev == 'error':
                _push(
                    'F2',
                    str(_obj_field(pf, 'code', 'PRICE_ERROR') or 'PRICE_ERROR'),
                    sev,
                    '开标一览表/分项报价表',
                    str(_obj_field(pf, 'message', '') or '报价算术或限价核验未通过'),
                    '请核对开标一览表与分项报价表并修正算术或限价偏差',
                    'veto',
                )

    for default_mod, flist in (
        ('F3', f3_findings or []),
        ('F4', f4_findings or []),
        ('BLIND_BID', blind_bid_findings or []),
    ):
        for f in flist:
            feedback = str(_obj_field(f, 'feedback', '') or '').lower()
            if feedback == 'rejected':
                continue
            src = str(_obj_field(f, 'source_module', default_mod) or default_mod).upper()
            code = str(_obj_field(f, 'code', '') or '').upper()
            sev = str(_obj_field(f, 'severity', 'error') or 'error').lower()
            conseq = str(_obj_field(f, 'consequence', '') or ('veto' if sev == 'error' else 'none')).lower()
            cno = str(_obj_field(f, 'clause_no', '') or '')
            msg = str(_obj_field(f, 'message', '') or '')
            sug = str(_obj_field(f, 'suggestion', '') or '')
            _push(src, code, sev, cno, msg, sug, conseq)

    return alerts


def _is_toc_line(line: str) -> bool:
    """判定单行是否为目录项（含制表符+页码、省略号点线+页码或编号标题+尾页码）。"""
    raw = str(line or '')
    stripped = raw.strip()
    if not stripped:
        return False
    if _TOC_LINE_RE.search(raw):
        return True
    return False


def _is_toc_region(bid_text: str, pos: int) -> bool:
    """判定字符偏移 pos 附近是否属于目录页密集区域（防目录碎片抢占正文证据窗口）。"""
    if not bid_text or pos < 0:
        return False
    lo = max(0, pos - 320)
    hi = min(len(bid_text), pos + 320)
    lines = [ln for ln in bid_text[lo:hi].splitlines() if ln.strip()]
    if not lines:
        return False
    # 若当前所在行本身就是目录行，直接排除
    line_start = bid_text.rfind('\n', 0, pos) + 1
    line_end = bid_text.find('\n', pos)
    if line_end == -1:
        line_end = len(bid_text)
    if _is_toc_line(bid_text[line_start:line_end]):
        return True
    toc_cnt = sum(1 for ln in lines if _is_toc_line(ln))
    return toc_cnt >= 3 and (toc_cnt / len(lines)) >= 0.45


def _truncate_clean_boundary(text: str, max_len: int) -> str:
    """按完整换行或句号边界收口，杜绝拦腰截断词语导致模型误判原文未写完。"""
    s = (text or '').strip()
    if len(s) <= max_len:
        return s
    head = s[:max_len]
    floor = int(max_len * 0.62)
    best_pos = -1
    for sep in ('\n', '。', '；', '！', '？', '.'):
        idx = head.rfind(sep)
        if idx >= floor and idx > best_pos:
            best_pos = idx
    if best_pos >= floor:
        return head[:best_pos + 1].strip()
    return head.strip()


def _extract_docx_table_blocks(bid_path: Path) -> list[dict[str, Any]]:
    """一次性抽取 .docx 全部表格块（按 cell._tc 本体去重，严格遵循 AGENTS.md 铁律 6）。"""
    if not bid_path.is_file() or bid_path.suffix.lower() != '.docx':
        return []
    blocks: list[dict[str, Any]] = []
    try:
        doc = Document(str(bid_path))
        for ti, tb in enumerate(doc.tables):
            rows_txt: list[str] = []
            raw_cells: list[str] = []
            seen_tcs = set()
            for row in tb.rows:
                r_cells: list[str] = []
                for cell in row.cells:
                    tc = cell._tc
                    if tc in seen_tcs:
                        continue
                    seen_tcs.add(tc)
                    c_txt = re.sub(r'\s+', ' ', cell.text.strip())
                    if c_txt:
                        r_cells.append(c_txt)
                        raw_cells.append(cell.text.strip())
                if r_cells:
                    rows_txt.append(' | '.join(r_cells))
            if len(rows_txt) >= 2:
                joined = '\n'.join(rows_txt[:22])
                blocks.append({
                    'table_index': ti,
                    'header': rows_txt[0],
                    'text': _truncate_clean_boundary(joined, 1500),
                    'anchor_cell': next((c for c in raw_cells if len(c) >= 6), raw_cells[0] if raw_cells else ''),
                })
    except Exception:
        return []
    return blocks


def _build_section_anchor_terms(factor: str, category: str) -> tuple[list[str], list[str]]:
    """为各典型评分因素生成 (章节标题定位词列表, 关联表格特征词列表)。"""
    fac = factor or ''
    heading_terms: list[str] = []
    table_terms: list[str] = []

    if any(k in fac for k in ('业绩', '案例', '合同')) or category == 'business':
        # 优先定位 10.3 业绩证明 / 合同扫描件 OCR 段落，完整《业绩案例一览表》由下方表格聚合直取
        heading_terms.extend(['业绩证明', '10.3', '类似项目业绩', '同类项目业绩', '项目业绩', '业绩案例一览表'])
        table_terms.extend(['项目名称 | 用户名称', '合同金额', '业绩', '签订日期', '用户名称', '用户联系电话'])
    if any(k in fac for k in ('售后', '维保', '运维', '质保')) or (category == 'service' and '培训' not in fac):
        heading_terms.extend(['售后服务方案', '售后服务', '维修响应时间', '售后服务响应时间', '应急响应服务', '故障级别', '免费质保期'])
        table_terms.extend(['故障级别', '响应时间', '服务摘要', '质保期限', '应急响应'])
    if any(k in fac for k in ('进度', '工期', '计划')):
        heading_terms.extend(['项目进度计划', '进度计划', '工作计划', '实施计划', '项目启动阶段'])
        table_terms.extend(['计划内容', 'T+', '执行时间', '阶段 | 责任人', '里程碑'])
    if any(k in fac for k in ('实施', '部署', '建设方案', '技术方案', '总体方案')):
        heading_terms.extend(['实施方案', '系统设计与架构规划', '实施部署', '产品质量及安全保证措施'])
        table_terms.extend(['计划内容', '项目实施阶段', '设备部署', '验收'])
    if '培训' in fac:
        heading_terms.extend(['培训方案', '技术培训', '培训内容', '培训计划', '培训目标'])
        table_terms.extend(['培训', '课时', '课程', '培训对象'])
    if any(k in fac for k in ('管理体系', '资质', '证书', '认证', '节能', '环保')):
        heading_terms.extend(['资格证书', '10.2', '质量管理体系', '节能产品', '环境标志'])
        table_terms.extend(['证书', '认证', 'ISO', '管理体系'])

    # 补充因子名自身清洗词作为通用标题锚点
    clean_fac = re.sub(r'[（(][^）)]*[）)]', '', fac).strip()
    if 2 <= len(clean_fac) <= 16 and clean_fac not in heading_terms:
        heading_terms.insert(0, clean_fac)

    return heading_terms, table_terms


def _collect_section_and_table_windows(
    factor: str,
    category: str,
    bid_text: str,
    bid_page_ctx: Any,
    docx_tables: Optional[list[dict[str, Any]]] = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """E44 §2.1：确定性章/表结构化证据聚合（过滤目录行 + 整表入窗 + 句边界收口 + verify_quote 核验）。"""
    if not bid_text or not bid_text.strip():
        return [], []

    heading_terms, table_terms = _build_section_anchor_terms(factor, category)
    win_texts: list[str] = []
    verified_quotes: list[dict[str, Any]] = []
    seen_starts: list[int] = []
    seen_q: set[str] = set()

    def _add_quote(raw_snippet: str, off: Optional[int]) -> None:
        snip = _truncate_clean_boundary(raw_snippet, 180)
        if not snip or snip in seen_q:
            return
        if not verify_quote(snip, bid_text):
            return
        seen_q.add(snip)
        real_off = off
        if real_off is None:
            pos = bid_text.find(snip[:36])
            real_off = pos if pos != -1 else None
        page = (
            resolve_unified_page(int(real_off), bid_page_ctx)[0]
            if (real_off is not None and bid_page_ctx is not None)
            else None
        )
        verified_quotes.append({
            'quote': snip,
            'quote_verified': True,
            'offset': real_off,
            'page': page,
        })

    # 1. 匹配非目录区章节标题与正文块
    lines_with_off: list[tuple[int, str]] = []
    cursor = 0
    for raw_ln in bid_text.splitlines(keepends=True):
        s_ln = raw_ln.strip()
        if s_ln:
            lines_with_off.append((cursor, raw_ln))
        cursor += len(raw_ln)

    for term in heading_terms:
        if len(win_texts) >= 3:
            break
        term_low = term.lower()
        for off, raw_ln in lines_with_off:
            s_ln = raw_ln.strip()
            if len(s_ln) > 48:
                continue
            if term_low not in s_ln.lower():
                continue
            if _is_toc_region(bid_text, off):
                continue
            # 跳过重复近邻命中
            if any(abs(off - prev) < 400 for prev in seen_starts):
                continue
            # 确认标题后确实有实质正文（跳过紧挨下一标题的空占位头，除非后面有 OCR/正文）
            chunk_raw = bid_text[off:min(len(bid_text), off + 1650)]
            non_toc_lines = [ln.strip() for ln in chunk_raw.splitlines() if ln.strip() and not _is_toc_line(ln)]
            if len(non_toc_lines) < 2:
                continue
            # 针对“6 业绩案例一览表”若后面立刻跳到“7 投标人基本情况表”（因为表格被 parser 放到了末尾），
            # 仍保留该非空章节段或让下方表格检索精准接棒
            cleaned_win = _truncate_clean_boundary('\n'.join(non_toc_lines), 1500)
            if len(cleaned_win) < 40:
                continue
            # 过滤《投标函》中的附件清单枚举段（如 "6 业绩案例一览表 7 投标人基本情况表 ... 在此，签字代表宣布同意如下："），防止滑偏到投标函页
            if '投标函' not in (factor or '') and any(
                noise in cleaned_win[:300]
                for noise in ('签字代表宣布同意如下', '所附投标价格表中规定', '中标服务费承诺函')
            ):
                continue
            seen_starts.append(off)
            win_texts.append(cleaned_win)
            # 挑首段实质句（跳过纯 4 字短标题）作为逐字溯源引用
            quote_cand = '\n'.join(non_toc_lines[:3])
            _add_quote(quote_cand, off)
            if len(win_texts) >= 3:
                break

    # 2. 匹配 .docx 结构化表格块（解决 python-docx 中表格与正文段落分离存储导致漏招整表的根因）
    if docx_tables and table_terms:
        matched_tables: list[tuple[int, dict[str, Any]]] = []
        for tb in docx_tables:
            hdr_and_head = f"{tb['header']}\n{tb['text'][:450]}".lower()
            hit_score = sum(1 for kw in table_terms if kw.lower() in hdr_and_head)
            if hit_score > 0:
                matched_tables.append((hit_score, tb))
        matched_tables.sort(key=lambda x: x[0], reverse=True)
        for _sc, tb in matched_tables[:2]:
            tbl_block = f"【投标书内嵌表格 #{tb['table_index'] + 1}】\n{tb['text']}"
            win_texts.insert(0, tbl_block)
            # 锚定表格单元格在 bid_text 中的连续原文用于严格 verify_quote 核验，并优先置于引用首位
            anc = str(tb.get('anchor_cell') or '').strip()
            if anc:
                pos = bid_text.find(anc)
                if pos != -1:
                    raw_slice = bid_text[pos:min(len(bid_text), pos + 200)]
                    before_len = len(verified_quotes)
                    _add_quote(raw_slice, pos)
                    if len(verified_quotes) > before_len:
                        verified_quotes.insert(0, verified_quotes.pop())

    return win_texts[:4], verified_quotes[:3]


def _retrieve_verified_local_quotes(
    query: str,
    bid_text: str,
    bid_page_ctx: Any,
    *,
    client: Any = None,
    top_k: int = 3,
    factor: str = '',
    category: str = '',
    docx_tables: Optional[list[dict[str, Any]]] = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """E44 §2.1：章表结构化聚合 + 目录隔离 + 透传 client 激活 L2 向量精排 Rerank（无向量自动降级 BM25）。"""
    if not bid_text or not bid_text.strip():
        return [], []

    # 1. 先执行结构化非目录章节 + 整表聚合补召回
    sec_wins, sec_quotes = _collect_section_and_table_windows(
        factor=factor,
        category=category,
        bid_text=bid_text,
        bid_page_ctx=bid_page_ctx,
        docx_tables=docx_tables,
    )

    # 2. 透传 client 调用三级混合检索（有 embedding 自动激活 L2 Rerank，否则 0-Token BM25）
    windows = hybrid_retrieve_windows(
        query,
        bid_text,
        client=client,
        top_k=max(top_k, 5),
        min_score=0.28,
        window_pad=360,
    )

    win_texts: list[str] = list(sec_wins)
    verified_quotes: list[dict[str, Any]] = list(sec_quotes)
    seen_q: set[str] = {str(q.get('quote') or '') for q in verified_quotes}
    seen_win_heads: set[str] = {w[:60] for w in win_texts}

    for w in windows:
        off = w.get('start')
        if off is not None and _is_toc_region(bid_text, int(off)):
            continue
        w_txt = str(w.get('window') or w.get('text') or '').strip()
        if not w_txt:
            continue
        # 剔除窗口内残留目录行
        non_toc_lns = [ln for ln in w_txt.splitlines() if ln.strip() and not _is_toc_line(ln)]
        if not non_toc_lns:
            continue
        clean_win = _truncate_clean_boundary('\n'.join(non_toc_lns), 1400)
        if not clean_win:
            continue
        if clean_win[:60] not in seen_win_heads and len(win_texts) < 5:
            seen_win_heads.add(clean_win[:60])
            win_texts.append(clean_win)

        # 取命中原句或实质句段做原文逐字摘录
        raw_snip = str(w.get('quote') or non_toc_lns[0] or '').strip()
        snippet = _truncate_clean_boundary(raw_snip, 180)
        if not snippet or snippet in seen_q or _is_toc_line(snippet):
            continue
        if verify_quote(snippet, bid_text):
            seen_q.add(snippet)
            real_off = off
            if real_off is None:
                found_pos = bid_text.find(snippet[:40])
                real_off = found_pos if found_pos != -1 else None
            page = (
                resolve_unified_page(int(real_off), bid_page_ctx)[0]
                if (real_off is not None and bid_page_ctx is not None)
                else None
            )
            verified_quotes.append({
                'quote': snippet,
                'quote_verified': True,
                'offset': real_off,
                'page': page,
            })

    return win_texts[:4], verified_quotes[:3]


def _parse_llm_json(raw_text: str) -> Optional[dict[str, Any]]:
    cleaned = (raw_text or '').strip()
    if not cleaned:
        return None
    cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*```$', '', cleaned)
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except Exception:
        m = re.search(r'\{.*\}', cleaned, flags=re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(0))
                return obj if isinstance(obj, dict) else None
            except Exception:
                return None
    return None


def _evaluate_criterion_with_llm(
    client: Any,
    crit: dict[str, Any],
    win_texts: list[str],
    local_quotes: list[dict[str, Any]],
    bid_text: str,
    bid_page_ctx: Any,
    f4_neg_count: int,
) -> dict[str, Any]:
    """对单条主观/複合评分项调用 client.chat（仅喂局部窗口），输出经 Decimal 钳位与 verify_quote 校验的结果。"""
    max_sc: Decimal = crit['max_score']
    evidence_block = '\n---\n'.join(win_texts[:4]) if win_texts else '（未检索到明显对应段落）'
    evidence_block = _truncate_clean_boundary(evidence_block, 4200)
    prompt = (
        "你是严谨的招投标技术评审专家。请仅根据【招标评分细则】与【我方投标书局部证据窗口】，"
        "客观评估我方在该评分项的预估得分、失分原因与可执行提分建议。\n"
        f"【评分因素】：{crit['factor']}（满分：{max_sc} 分，类别：{crit['category']}）\n"
        f"【评分标准细则】：{crit['scoring_rule_text']}\n"
        f"【关联实质性条款负偏离计数】：{f4_neg_count} 项\n"
        f"【我方投标书局部证据窗口】：\n{evidence_block}\n\n"
        "严格要求：\n"
        f"1. `estimated_score` 必须在 0 到 {max_sc} 之间（保留1~2位小数）；若证据完全缺失给低分，若有详实量化方案与表格/合同/证书佐证给合理高分；\n"
        "2. 若证据窗口已包含《业绩案例一览表》、合同扫描 OCR 文本、四级故障响应表或 T+n 里程碑表，请直接依据表中具体项数、时限与交付物客观赋分；严禁因窗口截断边界臆测“原文未写完或语句截断”；\n"
        "3. `evidence_quotes` 必须从上方【我方投标书局部证据窗口】逐字原样摘录 1~2 句（严禁改字、加前缀或编造）；\n"
        "4. 直接输出纯 JSON 对象：\n"
        '{"estimated_score": 8.5, "deduction_reasons": ["失分点1"], "improvement_suggestions": ["提分建议1"], "evidence_quotes": ["原文逐字摘录"]}'
    )
    resp = client.chat([{'role': 'user', 'content': prompt}], model_tier='flash')
    parsed = _parse_llm_json(str(getattr(resp, 'content', '') or ''))
    if not isinstance(parsed, dict) or 'estimated_score' not in parsed:
        raise ValueError('LLM 未返回有效评分 JSON')

    raw_est = _to_dec(parsed.get('estimated_score'), Decimal('-1'))
    if raw_est < _DEC_ZERO:
        # 负分钳位到 0.00
        clamped = _DEC_ZERO
    else:
        clamped = max(_DEC_ZERO, min(max_sc, raw_est)).quantize(_QUANT_001, rounding=ROUND_HALF_UP)

    reasons = [str(r).strip() for r in (parsed.get('deduction_reasons') or []) if str(r or '').strip()][:4]
    suggestions = [str(s).strip() for s in (parsed.get('improvement_suggestions') or []) if str(s or '').strip()][:4]

    verified_ev: list[dict[str, Any]] = []
    for raw_q in (parsed.get('evidence_quotes') or [])[:3]:
        q_str = str(raw_q['quote'] if isinstance(raw_q, dict) else raw_q or '').strip()
        if not q_str:
            continue
        if verify_quote(q_str, bid_text):
            pos = bid_text.find(q_str[:30])
            off = pos if pos != -1 else None
            page = resolve_unified_page(int(off), bid_page_ctx)[0] if (off is not None and bid_page_ctx is not None) else None
            verified_ev.append({
                'quote': q_str[:220],
                'quote_verified': True,
                'offset': off,
                'page': page,
            })
        else:
            # 铁律 3：未过 verify_quote 核验一律清空 quote 并标 quote_verified=False
            verified_ev.append({
                'quote': '',
                'quote_verified': False,
                'offset': None,
                'page': None,
            })

    if not any(e.get('quote_verified') for e in verified_ev) and local_quotes:
        verified_ev.extend(local_quotes[:1])

    ratio_pct = (
        ((clamped / max_sc) * _DEC_HUNDRED).quantize(_QUANT_01, rounding=ROUND_HALF_UP)
        if max_sc > _DEC_ZERO
        else Decimal('0.0')
    )
    if not reasons:
        reasons = ['当前正文章节已基本覆盖该评分点核心要求'] if clamped >= max_sc * Decimal('0.85') else ['局部方案量化深度或佐证材料仍有提升空间']
    if not suggestions:
        suggestions = ['建议进一步补充可执行量化指标、架构图表或验收标准以稳固高分']

    return {
        'estimated_score': clamped,
        'status': 'evaluated',
        'status_reason': 'llm_local_window_evaluated',
        'score_ratio_pct': float(ratio_pct),
        'deduction_reasons': reasons,
        'improvement_suggestions': suggestions,
        'evidence_quotes': verified_ev,
    }


def estimate_bid_score(
    tender_path: str | Path,
    bid_path: str | Path,
    *,
    f1_res: Optional[dict[str, Any]] = None,
    f2_res: Optional[dict[str, Any]] = None,
    f3_findings: Optional[list[Any]] = None,
    f4_findings: Optional[list[Any]] = None,
    blind_bid_findings: Optional[list[Any]] = None,
    client: Any = None,
    tender_parsed_doc: Any = None,
    bid_parsed_doc: Any = None,
    progress_cb: Optional[Callable[[int, str], None]] = None,
    cancel_check: Optional[Callable[[], None]] = None,
) -> dict[str, Any]:
    """E42 §2.1.2：逐项评估投标书正文质量，联动一票否决告警并输出 Decimal 守恒预估报告。"""
    def _emit(pct: int, detail: str) -> None:
        if cancel_check is not None:
            cancel_check()
        if progress_cb is not None:
            progress_cb(pct, detail)

    _emit(10, '正在解析招标评分细则与投标书正文…')
    t_path = Path(tender_path)
    b_path = Path(bid_path)
    if tender_parsed_doc is None and t_path.is_file():
        tender_parsed_doc = parse_document(str(t_path), ocr_images=False)
    if bid_parsed_doc is None and b_path.is_file():
        def _on_bid_ocr_prog(done_cnt: int, total_cnt: int, cached_cnt: int) -> None:
            if total_cnt > 0:
                pct = 10 + min(12, int((done_cnt / max(1, total_cnt)) * 12))
                _emit(pct, f"正在识别投标书内嵌证照与合同扫描件（{done_cnt}/{total_cnt}，命中缓存 {cached_cnt}）…")
        bid_parsed_doc = parse_document(str(b_path), ocr_images=True, progress_cb=_on_bid_ocr_prog)

    bid_text = str(getattr(bid_parsed_doc, 'text', '') or '')
    bid_page_ctx = build_unified_page_context(str(b_path)) if b_path.is_file() else None
    bid_docx_tables = _extract_docx_table_blocks(b_path) if b_path.is_file() else []

    if f2_res is None and b_path.is_file() and t_path.is_file():
        try:
            f2_res = run_price_check(
                b_path,
                t_path,
                parsed_doc=bid_parsed_doc,
                tender_parsed_doc=tender_parsed_doc,
                client=None,
            )
        except Exception:
            f2_res = {'status': 'NEED_MANUAL', 'findings': []}

    _emit(25, '正在汇聚 F2/F3/F4/E43 一票否决风险与评分细则表…')
    veto_alerts = _collect_veto_alerts(
        f2_res=f2_res,
        f3_findings=f3_findings,
        f4_findings=f4_findings,
        blind_bid_findings=blind_bid_findings,
    )

    collected = collect_scoring_criteria_items(
        t_path,
        f1_res=f1_res,
        client=client,
        tender_parsed_doc=tender_parsed_doc,
    )
    criteria: list[dict[str, Any]] = collected['criteria']
    declared_total_max: Decimal = collected['declared_total_max']
    items_sum: Decimal = collected['items_sum']
    score_scale_warning: Optional[str] = collected['score_scale_warning']

    # 统计 F4 负偏离/漏应答数量供客观扣分项使用
    f4_neg_items: list[Any] = []
    for f in f4_findings or []:
        if str(_obj_field(f, 'feedback', '') or '').lower() == 'rejected':
            continue
        src = str(_obj_field(f, 'source_module', 'F4') or 'F4').upper()
        code = str(_obj_field(f, 'code', '') or '').upper()
        sev = str(_obj_field(f, 'severity', '') or '').lower()
        if src == 'F4' and (sev in ('error', 'warning') or code in ('MISSING_RESPONSE', 'CONTRADICTION', 'MISSING_EVIDENCE')):
            f4_neg_items.append(f)

    f3_expired_certs: list[str] = [
        str(_obj_field(f, 'message', '') or '')
        for f in (f3_findings or [])
        if str(_obj_field(f, 'code', '') or '').upper() == 'EXPIRED_CERT'
    ]

    evaluated_items: list[dict[str, Any]] = [None] * len(criteria)  # type: ignore[list-item]
    llm_jobs: list[tuple[int, dict[str, Any], list[str], list[dict[str, Any]]]] = []

    _emit(40, f'正在对 {len(criteria)} 个评分细则项进行本地混合检索与客观项核算出分…')
    for idx, crit in enumerate(criteria):
        if cancel_check is not None:
            cancel_check()
        cid = crit['criterion_id']
        factor = crit['factor']
        cat = crit['category']
        max_sc: Decimal = crit['max_score']
        rule_txt = crit['scoring_rule_text']
        eval_mode = crit['evaluation_mode']

        # (1) 价格分：严禁捏造竞争对手报价基准，诚实置 pending_open_bid
        if eval_mode == 'price_formula' or cat == 'price':
            f2_err = [a for a in veto_alerts if a['source_module'] == 'F2']
            if f2_err:
                reasons = [f"报价核验发现异常：{f2_err[0]['message']}"]
                sugs = ['请立即修正《开标一览表》与分项报价明细表，消除限价或算术废标风险']
            else:
                p_status = str((f2_res or {}).get('status') or 'NEED_MANUAL')
                reasons = [
                    '评标基准价取决于开标现场各有效投标人报价均值/最低价，投前无法确定竞争对手报价，系统严禁捏造具体价格得分'
                ]
                sugs = [
                    f"本项满分 {max_sc} 分（当前报价算术状态：{p_status}）；请确保总报价不超最高限价且大小写与分项汇总 100% 一致"
                ]
            evaluated_items[idx] = {
                'criterion_id': cid,
                'section': crit['section'],
                'factor': factor,
                'category': cat,
                'evaluation_mode': 'price_formula',
                'max_score': _fmt_dec(max_sc),
                'estimated_score': None,
                'status': 'pending_open_bid',
                'status_reason': 'depends_on_open_bid_benchmark',
                'score_ratio_pct': None,
                'deduction_reasons': reasons,
                'improvement_suggestions': sugs,
                'evidence_quotes': [],
            }
            continue

        query_str = f"{factor} {rule_txt[:160]}".strip()
        win_texts, local_quotes = _retrieve_verified_local_quotes(
            query_str,
            bid_text,
            bid_page_ctx,
            client=client,
            top_k=3,
            factor=factor,
            category=cat,
            docx_tables=bid_docx_tables,
        )

        # (2) 客观扣分 / 证书匹配项
        if eval_mode == 'objective_deduction':
            m_ded = _DEDUCTION_PER_ITEM_RE.search(f"{factor} {rule_txt}")
            if m_ded:
                unit_ded = _to_dec(m_ded.group(1), _DEC_ZERO)
                neg_cnt = len(f4_neg_items)
                deducted = min(max_sc, (unit_ded * Decimal(neg_cnt)).quantize(_QUANT_001, rounding=ROUND_HALF_UP))
                est_sc = max(_DEC_ZERO, (max_sc - deducted).quantize(_QUANT_001, rounding=ROUND_HALF_UP))
                has_star_veto = any(str(_obj_field(f, 'consequence', '') or '') == 'veto' for f in f4_neg_items)
                st_val: ItemScoreStatus = 'veto_risk' if (has_star_veto and est_sc == _DEC_ZERO) else 'evaluated'
                ratio = (
                    ((est_sc / max_sc) * _DEC_HUNDRED).quantize(_QUANT_01, rounding=ROUND_HALF_UP)
                    if max_sc > _DEC_ZERO
                    else Decimal('0.0')
                )
                reasons = (
                    [f"依据「每项不满足扣 {unit_ded} 分」规则，结合 F4 检出 {neg_cnt} 项偏离/漏应答，扣减 {deducted} 分"]
                    if neg_cnt > 0
                    else ['F4 实质性/技术条款响应核验未发现负偏离，按满分预估']
                )
                sugs = (
                    ['请对照 F4 风险卡片与《技术偏离表》逐条补齐漏应答条款与证明材料索引']
                    if neg_cnt > 0
                    else ['保持技术偏离表与正文应答页码索引一致']
                )
                evaluated_items[idx] = {
                    'criterion_id': cid,
                    'section': crit['section'],
                    'factor': factor,
                    'category': cat,
                    'evaluation_mode': eval_mode,
                    'max_score': _fmt_dec(max_sc),
                    'estimated_score': _fmt_dec(est_sc),
                    'status': st_val,
                    'status_reason': 'objective_f4_deduction',
                    'score_ratio_pct': float(ratio),
                    'deduction_reasons': reasons,
                    'improvement_suggestions': sugs,
                    'evidence_quotes': local_quotes,
                }
                continue

            # 若包含模糊“扣...分”但正则无法拆解阶梯 -> 严禁硬猜数字，诚实标 need_llm
            if _AMBIGUOUS_DEDUCTION_SIGNAL_RE.search(f"{factor} {rule_txt}"):
                evaluated_items[idx] = {
                    'criterion_id': cid,
                    'section': crit['section'],
                    'factor': factor,
                    'category': cat,
                    'evaluation_mode': eval_mode,
                    'max_score': _fmt_dec(max_sc),
                    'estimated_score': None,
                    'status': 'need_llm',
                    'status_reason': 'ambiguous_deduction_rule',
                    'score_ratio_pct': None,
                    'deduction_reasons': ['该客观扣分规则存在多档或相对排名条件，确定性规则拒绝硬猜扣分分值'],
                    'improvement_suggestions': [f'请对照招标细则「{rule_txt[:80]}」人工核对偏离档位'],
                    'evidence_quotes': local_quotes,
                }
                continue

            # E44 §2.1：按证书同义词组匹配（支持扫描图 OCR 中的 ISO9001/GB/T19001 等，精确分项给分）
            crit_lower = f"{factor} {rule_txt}".lower()
            bid_lower = bid_text.lower()
            required_cert_groups: list[tuple[str, tuple[str, ...]]] = [
                (canon, aliases)
                for canon, aliases in _CERT_SYNONYM_GROUPS
                if any(al in crit_lower for al in aliases)
            ]
            if required_cert_groups:
                found_canons: list[str] = []
                cert_quotes: list[dict[str, Any]] = []
                for canon, aliases in required_cert_groups:
                    hit_pos = -1
                    for al in aliases:
                        p = bid_lower.find(al)
                        if p != -1:
                            # 避免命中诸如 "未提供质量管理体系" / "质量管理体系（无）" 的显式否定声明
                            ctx_s = bid_text[max(0, p - 10):min(len(bid_text), p + len(al) + 12)]
                            if re.search(r'(?:未提供|无此|（无）|\(无\))', ctx_s):
                                continue
                            hit_pos = p
                            break
                    if hit_pos != -1:
                        found_canons.append(canon)
                        ln_s = bid_text.rfind('\n', 0, hit_pos) + 1
                        ln_e = bid_text.find('\n', hit_pos)
                        if ln_e == -1:
                            ln_e = len(bid_text)
                        q_raw = _truncate_clean_boundary(bid_text[ln_s:min(ln_e, ln_s + 180)], 180)
                        if q_raw and verify_quote(q_raw, bid_text):
                            pg = (
                                resolve_unified_page(int(ln_s), bid_page_ctx)[0]
                                if bid_page_ctx is not None
                                else None
                            )
                            cert_quotes.append({
                                'quote': q_raw,
                                'quote_verified': True,
                                'offset': ln_s,
                                'page': pg,
                            })

                matched_canons = [c for c, _ in required_cert_groups]
                expired_hit = any(
                    any(
                        al in exp.lower()
                        for canon, aliases in required_cert_groups
                        if canon in found_canons
                        for al in aliases
                    )
                    for exp in f3_expired_certs
                )
                if expired_hit:
                    est_sc = _DEC_ZERO
                    reasons = ['投标书虽提及相关证书，但 F3 检出证照存在过期风险，面临不得分或废标']
                    sugs = ['请立即更新有效期内的新证扫描件并替换至标书资格/商务章节']
                elif len(found_canons) == len(matched_canons):
                    est_sc = max_sc
                    reasons = [f"投标书正文/扫描件已匹配全部所需资质认证（{', '.join(found_canons)}）"]
                    sugs = ['请确认已附上清晰证书扫描件及全国认证认可信息公共服务平台查询截图']
                elif found_canons:
                    est_sc = (max_sc * Decimal(len(found_canons)) / Decimal(len(matched_canons))).quantize(
                        _QUANT_001, rounding=ROUND_HALF_UP
                    )
                    missing_canons = [c for c in matched_canons if c not in found_canons]
                    reasons = [f"已检出 {', '.join(found_canons)}，但正文中及扫描件未检索到 {', '.join(missing_canons)}"]
                    sugs = [f"请前往「企业证照台账」核对是否具备 {', '.join(missing_canons)} 并补入商务资质章节"]
                else:
                    est_sc = _DEC_ZERO
                    reasons = [f"投标书正文及扫描件中均未检索到所需资质/认证（{', '.join(matched_canons)}）"]
                    sugs = [f"请补充 {', '.join(matched_canons)} 证书复印件及有效期内证明"]

                ratio = (
                    ((est_sc / max_sc) * _DEC_HUNDRED).quantize(_QUANT_01, rounding=ROUND_HALF_UP)
                    if max_sc > _DEC_ZERO
                    else Decimal('0.0')
                )
                evaluated_items[idx] = {
                    'criterion_id': cid,
                    'section': crit['section'],
                    'factor': factor,
                    'category': cat,
                    'evaluation_mode': eval_mode,
                    'max_score': _fmt_dec(max_sc),
                    'estimated_score': _fmt_dec(est_sc),
                    'status': 'evaluated',
                    'status_reason': 'deterministic_cert_match',
                    'score_ratio_pct': float(ratio),
                    'deduction_reasons': reasons,
                    'improvement_suggestions': sugs,
                    'evidence_quotes': cert_quotes or local_quotes,
                }
                continue

        # (3) 主观质量项（或需模型研判项）
        if client is None:
            has_ev = len(local_quotes) > 0
            evaluated_items[idx] = {
                'criterion_id': cid,
                'section': crit['section'],
                'factor': factor,
                'category': cat,
                'evaluation_mode': eval_mode,
                'max_score': _fmt_dec(max_sc),
                'estimated_score': None,
                'status': 'need_llm',
                'status_reason': 'no_llm_client',
                'score_ratio_pct': None,
                'deduction_reasons': [
                    '当前处于 0-Token 无模型模式，已提取本地混合检索证据窗口，主观方案质量分值待开启大模型评估'
                    if has_ev
                    else '当前处于 0-Token 模式，且投标书正文中未检索到显著匹配段落（疑似缺漏章节）'
                ],
                'improvement_suggestions': [
                    f"建议对照评分细则「{factor}」开启大模型自评，或前往 F8 标书编制中心针对该评分点补强量化方案"
                ],
                'evidence_quotes': local_quotes,
            }
        else:
            llm_jobs.append((idx, crit, win_texts, local_quotes))

    # 并发执行受控局部窗口 LLM 主观评估
    if llm_jobs and client is not None:
        total_llm = len(llm_jobs)
        done_llm = 0

        def _run_llm_one(job: tuple[int, dict[str, Any], list[str], list[dict[str, Any]]]) -> tuple[int, dict[str, Any]]:
            j_idx, j_crit, j_wins, j_quotes = job
            if cancel_check is not None:
                cancel_check()
            res_dict = _evaluate_criterion_with_llm(
                client,
                j_crit,
                j_wins,
                j_quotes,
                bid_text,
                bid_page_ctx,
                len(f4_neg_items),
            )
            return j_idx, res_dict

        with ThreadPoolExecutor(max_workers=min(4, max(1, total_llm))) as pool:
            fut_map = {pool.submit(_run_llm_one, jb): jb for jb in llm_jobs}
            try:
                for fut, jb in fut_map.items():
                    j_idx, j_crit, _j_wins, j_quotes = jb
                    cid = j_crit['criterion_id']
                    max_sc = j_crit['max_score']
                    try:
                        _, llm_out = fut.result()
                        evaluated_items[j_idx] = {
                            'criterion_id': cid,
                            'section': j_crit['section'],
                            'factor': j_crit['factor'],
                            'category': j_crit['category'],
                            'evaluation_mode': j_crit['evaluation_mode'],
                            'max_score': _fmt_dec(max_sc),
                            'estimated_score': _fmt_dec(llm_out['estimated_score']),
                            'status': llm_out['status'],
                            'status_reason': llm_out['status_reason'],
                            'score_ratio_pct': llm_out['score_ratio_pct'],
                            'deduction_reasons': llm_out['deduction_reasons'],
                            'improvement_suggestions': llm_out['improvement_suggestions'],
                            'evidence_quotes': llm_out['evidence_quotes'],
                        }
                    except BaseException as exc:
                        if not isinstance(exc, Exception):
                            raise
                        # 单条评分失败/超时优雅降级，严禁阻断整份报告
                        evaluated_items[j_idx] = {
                            'criterion_id': cid,
                            'section': j_crit['section'],
                            'factor': j_crit['factor'],
                            'category': j_crit['category'],
                            'evaluation_mode': j_crit['evaluation_mode'],
                            'max_score': _fmt_dec(max_sc),
                            'estimated_score': None,
                            'status': 'need_llm',
                            'status_reason': 'llm_call_failed',
                            'score_ratio_pct': None,
                            'deduction_reasons': [f"单条模型评估未完成（已保留本地检索证据）：{str(exc)[:80]}"],
                            'improvement_suggestions': [f"请结合下方证据摘录人工复核「{j_crit['factor']}」响应完整度"],
                            'evidence_quotes': j_quotes,
                        }
                    done_llm += 1
                    pct = 50 + int((done_llm / max(1, total_llm)) * 40)
                    _emit(pct, f"正在评估主观评分项（{done_llm}/{total_llm}）…")
            except BaseException:
                pool.shutdown(wait=False, cancel_futures=True)
                raise

    # 汇总 Decimal 守恒计算
    assessed_max = _DEC_ZERO
    assessed_est = _DEC_ZERO
    price_pending_max = _DEC_ZERO
    need_llm_max = _DEC_ZERO

    priority_candidates: list[tuple[Decimal, dict[str, Any]]] = []

    for it in evaluated_items:
        if it is None:
            continue
        m_sc = _to_dec(it['max_score'], _DEC_ZERO)
        st = it['status']
        if st == 'pending_open_bid':
            price_pending_max = (price_pending_max + m_sc).quantize(_QUANT_001, rounding=ROUND_HALF_UP)
            continue
        if st == 'need_llm' or it['estimated_score'] is None:
            need_llm_max = (need_llm_max + m_sc).quantize(_QUANT_001, rounding=ROUND_HALF_UP)
            if not it.get('evidence_quotes'):
                priority_candidates.append((
                    m_sc,
                    {
                        'criterion_id': it['criterion_id'],
                        'factor': it['factor'],
                        'category': it['category'],
                        'max_score': _fmt_dec(m_sc),
                        'estimated_score': None,
                        'gap_score': _fmt_dec(m_sc),
                        'status': st,
                        'action_summary': (it.get('improvement_suggestions') or ['补充该评分项专项响应章节'])[0],
                        'f8_composer_hint': f"可前往 F8 标书编制中心针对「{it['factor']}」补充量化方案与案例佐证",
                    },
                ))
            continue

        e_sc = _to_dec(it['estimated_score'], _DEC_ZERO)
        e_sc = max(_DEC_ZERO, min(m_sc, e_sc)).quantize(_QUANT_001, rounding=ROUND_HALF_UP)
        it['estimated_score'] = _fmt_dec(e_sc)
        assessed_max = (assessed_max + m_sc).quantize(_QUANT_001, rounding=ROUND_HALF_UP)
        assessed_est = (assessed_est + e_sc).quantize(_QUANT_001, rounding=ROUND_HALF_UP)

        gap = (m_sc - e_sc).quantize(_QUANT_001, rounding=ROUND_HALF_UP)
        if gap > _DEC_ZERO:
            priority_candidates.append((
                gap,
                {
                    'criterion_id': it['criterion_id'],
                    'factor': it['factor'],
                    'category': it['category'],
                    'max_score': _fmt_dec(m_sc),
                    'estimated_score': _fmt_dec(e_sc),
                    'gap_score': _fmt_dec(gap),
                    'status': st,
                    'action_summary': (it.get('improvement_suggestions') or ['针对失分点补齐技术参数与证明材料'])[0],
                    'f8_composer_hint': f"可前往 F8 标书编制中心针对「{it['factor']}」强化点对点应答（预计可挽回空间 {gap} 分）",
                },
            ))

    priority_candidates.sort(key=lambda pair: pair[0], reverse=True)
    top_priorities = [p for _, p in priority_candidates[:5]]

    assessed_ratio_pct = (
        float(((assessed_est / assessed_max) * _DEC_HUNDRED).quantize(_QUANT_01, rounding=ROUND_HALF_UP))
        if assessed_max > _DEC_ZERO
        else None
    )

    _emit(98, '已完成评分预估与提分诊断汇总')
    return {
        'status': 'completed',
        'progress': 100,
        'stage_detail': '评分预估与提分诊断完成',
        'error_msg': None,
        'disclaimer': SCORE_DISCLAIMER_TEXT,
        'llm_status': 'done' if client is not None else 'skipped',
        'has_veto_risk': len(veto_alerts) > 0,
        'veto_alerts': veto_alerts,
        'total_declared_max_score': _fmt_dec(declared_total_max),
        'items_sum_max_score': _fmt_dec(items_sum),
        'assessed_max_score': _fmt_dec(assessed_max),
        'assessed_estimated_score': _fmt_dec(assessed_est),
        'assessed_ratio_pct': assessed_ratio_pct,
        'price_pending_max_score': _fmt_dec(price_pending_max),
        'need_llm_max_score': _fmt_dec(need_llm_max),
        'score_scale_warning': score_scale_warning,
        'items': [it for it in evaluated_items if it is not None],
        'top_priorities': top_priorities,
    }


def _set_cell_shading(cell: Any, fill_hex: str) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), fill_hex.lstrip('#'))
    tcPr.append(shd)


def export_score_estimate_docx(
    estimate_result: dict[str, Any],
    output_path: str | Path,
    *,
    task_title: str = '标书体检任务',
) -> Path:
    """E42 §2.1.3：导出《标书预估得分与提分诊断报告.docx》，封面与页眉强标注免责声明。"""
    doc = Document()
    normal = doc.styles['Normal']
    normal.font.name = '微软雅黑'
    normal.font.size = Pt(10.5)

    for sec in doc.sections:
        sec.top_margin = Inches(0.9)
        sec.bottom_margin = Inches(0.85)
        sec.left_margin = Inches(0.9)
        sec.right_margin = Inches(0.9)
        hdr = sec.header.paragraphs[0] if sec.header.paragraphs else sec.header.add_paragraph()
        hdr.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        r_hdr = hdr.add_run(f"【{SCORE_DISCLAIMER_TEXT}】 · {task_title}")
        r_hdr.font.size = Pt(8.5)
        r_hdr.font.color.rgb = RGBColor(0xB4, 0x53, 0x09)

    # 顶部封面标题与强标注免责声明框
    h_title = doc.add_heading('标书预估得分与提分诊断报告', level=1)
    h_title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    p_disc = doc.add_paragraph()
    p_disc.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r_disc = p_disc.add_run(
        f"重要声明：{SCORE_DISCLAIMER_TEXT}。本报告基于招标评分细则与我方投标正文局部证据自动比对生成，"
        "旨在辅助投前查漏补缺与针对性提分，不代表评标委员会最终打分承诺。"
    )
    r_disc.bold = True
    r_disc.font.size = Pt(10.0)
    r_disc.font.color.rgb = RGBColor(0xDC, 0x26, 0x26)

    # 一、评估总览
    doc.add_heading('一、评分预估总览与分值守恒核对', level=2)
    ov_tbl = doc.add_table(rows=5, cols=2)
    ov_tbl.style = 'Table Grid'
    ov_tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    ov_pairs = (
        ('任务名称', task_title),
        ('非价格项预估得分 / 已评估满分', f"{estimate_result.get('assessed_estimated_score', '0.00')} / {estimate_result.get('assessed_max_score', '0.00')} 分"),
        ('价格分（待开标现场基准价确定）', f"{estimate_result.get('price_pending_max_score', '0.00')} 分（pending_open_bid）"),
        ('待模型/人工复核项满分合计', f"{estimate_result.get('need_llm_max_score', '0.00')} 分"),
        ('一票否决/废标红线风险数', f"{len(estimate_result.get('veto_alerts') or [])} 项"),
    )
    for r_i, (k, v) in enumerate(ov_pairs):
        c0, c1 = ov_tbl.rows[r_i].cells[0], ov_tbl.rows[r_i].cells[1]
        _set_cell_shading(c0, 'F1F5F9')
        c0.paragraphs[0].add_run(k).bold = True
        c1.paragraphs[0].add_run(str(v))

    if estimate_result.get('score_scale_warning'):
        p_warn = doc.add_paragraph()
        r_w = p_warn.add_run(f"分值校准提示：{estimate_result['score_scale_warning']}")
        r_w.font.color.rgb = RGBColor(0xB4, 0x53, 0x09)

    # 二、一票否决警示表
    veto_alerts = estimate_result.get('veto_alerts') or []
    doc.add_heading('二、一票否决与废标红线先导警示（含 F2/F3/F4/E43 暗标）', level=2)
    if veto_alerts:
        v_tbl = doc.add_table(rows=1, cols=4)
        v_tbl.style = 'Table Grid'
        for idx, h in enumerate(('来源模块', '风险代码', '风险描述', '整改建议')):
            c = v_tbl.rows[0].cells[idx]
            _set_cell_shading(c, 'FEE2E2')
            c.paragraphs[0].add_run(h).bold = True
        for va in veto_alerts:
            rc = v_tbl.add_row().cells
            rc[0].paragraphs[0].add_run(str(va.get('source_module') or ''))
            rc[1].paragraphs[0].add_run(str(va.get('code') or ''))
            rc[2].paragraphs[0].add_run(str(va.get('message') or ''))
            rc[3].paragraphs[0].add_run(str(va.get('suggestion') or ''))
    else:
        doc.add_paragraph('未检出触发 consequence="veto" 的一票否决风险项。')

    # 三、重点提分抓手 Top-5
    priorities = estimate_result.get('top_priorities') or []
    doc.add_heading('三、重点提分行动抓手（按可挽回分值空间排序）', level=2)
    if priorities:
        for idx, pr in enumerate(priorities, start=1):
            doc.add_paragraph(
                f"{idx}. 【{pr.get('factor')}】（满分 {pr.get('max_score')} 分｜可挽回空间 {pr.get('gap_score')} 分）："
                f"{pr.get('action_summary')}（{pr.get('f8_composer_hint', '')}）"
            )
    else:
        doc.add_paragraph('当前已评估项得分率较高，暂无显著失分缺口。')

    # 四、逐项评分预估与提分建议明细表
    doc.add_heading('四、评分细则逐项评估与原文依据明细表', level=2)
    items = estimate_result.get('items') or []
    if items:
        it_tbl = doc.add_table(rows=1, cols=7)
        it_tbl.style = 'Table Grid'
        headers = ('类别', '评分因素', '满分', '预估得分', '状态', '失分原因与提分建议', '投标正文依据摘录')
        for idx, h in enumerate(headers):
            c = it_tbl.rows[0].cells[idx]
            c.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            _set_cell_shading(c, 'F1F5F9')
            c.paragraphs[0].add_run(h).bold = True
        for it in items:
            rc = it_tbl.add_row().cells
            rc[0].paragraphs[0].add_run(str(it.get('category') or ''))
            rc[1].paragraphs[0].add_run(str(it.get('factor') or ''))
            rc[2].paragraphs[0].add_run(str(it.get('max_score') or ''))
            est = it.get('estimated_score')
            rc[3].paragraphs[0].add_run(str(est) if est is not None else '待定')
            rc[4].paragraphs[0].add_run(str(it.get('status') or ''))
            reasons_txt = '；'.join(it.get('deduction_reasons') or [])
            sugs_txt = '；'.join(it.get('improvement_suggestions') or [])
            rc[5].paragraphs[0].add_run(f"【原因】{reasons_txt}\n【建议】{sugs_txt}")
            ev_list = it.get('evidence_quotes') or []
            verified_lines = [
                f"[P{ev['page']}] {ev['quote']}" if ev.get('page') else str(ev.get('quote') or '')
                for ev in ev_list
                if ev.get('quote_verified') and ev.get('quote')
            ]
            rc[6].paragraphs[0].add_run('\n'.join(verified_lines) if verified_lines else '原文未直引')
    else:
        doc.add_paragraph('未提取到结构化评分细则条目。')

    out_p = Path(output_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_p))
    return out_p
