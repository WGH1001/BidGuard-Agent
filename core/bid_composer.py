# -*- coding: utf-8 -*-
"""E37 / E38 / F8 · 标书编制输出与 AI Skill 动态编排引擎。

核心架构：
1. 统一章节蓝图 IR（BidBlueprint / BlueprintSection）：
   - 绑定租户写作 Skill（writing_instructions + layout_spec）决定章法策略与 Word 视觉排版；
   - `use_ai=True` 且 `client != None` 时：由大模型结合招标结构/评分细则/采购需求 + Skill 指令
     动态规划专属章节树并并发撰写技术与商务初稿正文与辅助表格，同时执行严格防漏覆盖审计；
   - `client is None` 时：无缝降级为 0-Token 确定性蓝图（按 Skill 版式渲染，不重复堆砌），
     仍严格修复 `eval_rule`/评分细则截断串行问题；
2. 三条防废标铁律（任何 AI / Skill 均不可越线）：
   - 报价表数字零编造：总报价/单价严格占位 `[待人工填报金额...]`，限价全程 `Decimal(ROUND_HALF_UP)`；
   - 证照与业绩零虚构：只引用 `CompanyCertificate` 台账及 V2 `LocalKnowledgeSnippetProvider` 经 `verify_quote` 核验片段，
     对 AI 草稿内未经授权出现的币种金额/合同编号自动脱敏锁定 `[待人工核实]`；
   - ★/▲ 实质性条款必全覆盖：确定性核对所有一票否决要求均挂接至偏离表及响应章节；
3. 专业 Word 排版（export_composed_bid_docx）：
   - 独立居中封面 + Page Break + 原生 Heading 1/2/3 与目录（TOC） + 定制页边距/列宽/行距/单元格内边距；
   - 内部人工核对清单默认与投标正本正文分离（受 Skill `layout_spec.append_checklist_at_end` 控制）。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path
import re
from typing import Any, Callable, Literal, Optional, Protocol, runtime_checkable

from docx import Document
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_COLOR_INDEX
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from core.composer_skills import (
    DEFAULT_LAYOUT_SPEC,
    normalize_layout_spec,
)
from core.hybrid_retriever import _tokenize_for_bm25, hybrid_retrieve_windows
from core.parser import parse_document
from core.price_extractor import extract_price_cap
from core.qualification_scanner import match_required_certificates_with_ledger
from core.quote_verifier import normalize_zh, verify_quote
from core.response_checker import (
    _filter_valid_requirements,
    build_unified_page_context,
    resolve_unified_page,
)
from core.tender_extractor import (
    build_review_checklist,
    classify_invalid_clause_role,
    extract_chapter4_procurement_requirements,
    extract_required_certificates,
)

ComposeMode = Literal['skeleton_v1', 'knowledge_draft_v2']

_COLOR_NAVY = RGBColor(0x0F, 0x17, 0x2A)
_COLOR_RED = RGBColor(0xDC, 0x26, 0x26)
_COLOR_GREEN = RGBColor(0x16, 0xA3, 0x4A)
_COLOR_AMBER = RGBColor(0xB4, 0x53, 0x09)
_COLOR_MUTED = RGBColor(0x47, 0x55, 0x69)

_PROJECT_NAME_RES = (
    re.compile(r'(?:采购|招标)?项目名称\s*[：:]\s*([^\n\r]{3,100})'),
    re.compile(r'工程名称\s*[：:]\s*([^\n\r]{3,100})'),
)
_PROJECT_CODE_RES = (
    re.compile(r'(?:采购|招标|项目|文件)编号\s*[：:]\s*([A-Za-z0-9\-_/（）()—]{4,64})'),
)
_BUYER_NAME_RES = (
    re.compile(r'(?:采购人|招标人)(?:名称)?\s*[：:]\s*([^\n\r]{2,80})'),
    re.compile(r'采购单位\s*[：:]\s*([^\n\r]{2,80})'),
)
_EVAL_SUBITEM_RE = re.compile(
    r'(?:^|\n)\s*(?:\d+[.、）)]|（[一二三四五六七八九十\d]+）)\s*([^\n]{6,180})'
)
_SCORING_BLEED_RE = re.compile(r'评分细则\s*[：:]?\s*(?:序号|评分因素|评价指标).*$', flags=re.DOTALL)
_UNGROUNDED_MONEY_RE = re.compile(
    r'(?:人民币\s*)?(?:¥|￥|RMB)?\s*\d[\d,]*(?:\.\d+)?\s*(?:万元|元整?)'
)
_UNGROUNDED_CONTRACT_RE = re.compile(
    r'(?:合同编号|证书编号|业绩编号)\s*[：:]\s*([A-Za-z0-9\-_/]{4,40})'
)
_TOC_DOT_LEADER_RE = re.compile(r'\.{4,}|…{2,}')
_PROCEDURAL_TOPIC_RE = re.compile(
    r'(?:'
    r'认真阅读招标文件|招标文件的澄清|招标文件的修改|潜在投标人|购买招标文件|'
    r'获取招标文件|投标文件编制的原则|投标截止期|采购代理机构|'
    r'评标委员会根据评分标准|得分合计后取平均值|计分方法|排名第一的中标候选人|'
    r'综合评分法|评审结果按评审|不接受联合体|特定资格要求|'
    r'政府采购法.{0,12}第二十二条|信用中国|失信被执行人|重大税收违法|'
    r'密封和标记|开标时间|开标地点|答疑会|踏勘现场|保证金退还|中标通知书'
    r')'
)
_PDF_SCORE_LINE_RE = re.compile(r'^(?:\d+(?:\.\d+)?\s*[-~—至]\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?)\s*分$')
_CERT_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.bmp', '.webp'}


@dataclass
class BlueprintSection:
    section_id: str
    title: str
    slot_type: str
    fill_mode: str
    tender_requirements: list[dict[str, Any]] = field(default_factory=list)
    content_blocks: list[dict[str, Any]] = field(default_factory=list)
    needs_manual_items: list[dict[str, Any]] = field(default_factory=list)
    source_refs: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BidBlueprint:
    project_name: str
    project_code: str
    buyer_name: str
    open_date: Optional[str]
    open_date_source: Optional[str]
    price_cap: Optional[str]
    package_no: Optional[int]
    compose_mode: ComposeMode
    tender_filename: str
    skill_id: Optional[int] = None
    skill_name: str = '标准骨架默认风'
    skill_layout: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_LAYOUT_SPEC))
    ai_status: str = 'skipped'
    planned_chapters: list[dict[str, Any]] = field(default_factory=list)
    sections: list[BlueprintSection] = field(default_factory=list)
    manual_checklist: list[dict[str, Any]] = field(default_factory=list)
    cert_match_summary: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            'project_name': self.project_name,
            'project_code': self.project_code,
            'buyer_name': self.buyer_name,
            'open_date': self.open_date,
            'open_date_source': self.open_date_source,
            'price_cap': self.price_cap,
            'package_no': self.package_no,
            'compose_mode': self.compose_mode,
            'tender_filename': self.tender_filename,
            'skill_id': self.skill_id,
            'skill_name': self.skill_name,
            'skill_layout': dict(self.skill_layout),
            'ai_status': self.ai_status,
            'planned_chapters': list(self.planned_chapters),
            'sections': [s.to_dict() for s in self.sections],
            'manual_checklist': list(self.manual_checklist),
            'cert_match_summary': dict(self.cert_match_summary),
            'stats': dict(self.stats),
        }


@runtime_checkable
class KnowledgeSnippetProvider(Protocol):
    """V2 可插拔企业历史方案/知识片段检索协议。"""

    @property
    def has_documents(self) -> bool:
        ...

    def retrieve(self, query: str, *, top_k: int = 2, min_score: float = 0.35) -> list[dict[str, Any]]:
        ...


def _extract_scoped_snippet(doc_text: str, hit: dict[str, Any]) -> str:
    """基于命中块截取同小节紧邻正文，避免 window_pad 误吸无关章节段落。"""
    quote = str(hit.get('quote') or '').strip()
    if not quote:
        return ''
    start = int(hit.get('start') or 0)
    end = int(hit.get('end') or (start + len(quote)))
    if len(quote) >= 60 or end >= len(doc_text):
        return quote

    tail = doc_text[end:min(len(doc_text), end + 360)]
    extra_lines: list[str] = []
    for raw_line in tail.split('\n'):
        s = raw_line.strip()
        if not s:
            if extra_lines:
                break
            continue
        if re.match(r'^(?:[一二三四五六七八九十]+、|第[一二三四五六七八九十]+章|\d+[.、]\s*\S{2,20}$)', s):
            break
        extra_lines.append(s)
        if sum(len(x) for x in extra_lines) >= 220:
            break
    combined = '\n'.join([quote] + extra_lines) if extra_lines else quote
    return combined if verify_quote(combined, doc_text) else quote


class LocalKnowledgeSnippetProvider:
    """V2 默认本地参考标书检索适配器（复用 hybrid_retrieve_windows + verify_quote 溯源）。"""

    def __init__(
        self,
        knowledge_paths: list[str | Path],
        *,
        client: Any = None,
    ) -> None:
        self._docs: list[tuple[str, str]] = []
        self._client = client
        for raw_p in knowledge_paths or []:
            p = Path(raw_p)
            if not p.is_file():
                continue
            try:
                parsed = parse_document(str(p))
                txt = (parsed.text or '').strip()
                if txt:
                    self._docs.append((p.name, txt))
            except Exception:
                continue

    @property
    def has_documents(self) -> bool:
        return bool(self._docs)

    def retrieve(self, query: str, *, top_k: int = 2, min_score: float = 0.35) -> list[dict[str, Any]]:
        if not self._docs or not (query or '').strip():
            return []

        q_tokens = set(_tokenize_for_bm25(query, expand_syn=True))
        candidates: list[dict[str, Any]] = []

        for filename, doc_text in self._docs:
            hits = hybrid_retrieve_windows(
                query,
                doc_text,
                client=self._client,
                top_k=max(top_k, 4),
                min_score=min_score,
                window_pad=0,
            )
            for h in hits:
                quote_txt = str(h.get('quote') or '').strip()
                if not quote_txt or not verify_quote(quote_txt, doc_text):
                    continue
                c_tokens = set(_tokenize_for_bm25(quote_txt, expand_syn=False))
                overlap = q_tokens & c_tokens
                has_syn = any(t.startswith('__syn_group_') for t in overlap)
                if not has_syn and len(overlap) < 2:
                    continue
                snippet = _extract_scoped_snippet(doc_text, h)
                candidates.append({
                    'source_filename': filename,
                    'snippet': snippet,
                    'quote': quote_txt,
                    'offset': int(h.get('offset') or 0),
                    'score': float(h.get('score') or 0.0),
                    'raw_bm25': float(h.get('raw_bm25') or 0.0),
                    'quote_verified': True,
                })

        candidates.sort(key=lambda x: (x['raw_bm25'], x['score']), reverse=True)
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for c in candidates:
            key = normalize_zh(c['snippet'])[:80]
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(c)
            if len(out) >= top_k:
                break
        return out


def _obj_field(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _merge_required_certificates_by_role(required_certs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按规范证书名称合并重复证照条目，保留全部角色（如 `必交资格 / 加分参考`）。"""
    merged: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}
    for item in required_certs or []:
        c_name = re.sub(r'\s+', ' ', str(item.get('cert_name') or '').strip())
        if not c_name:
            continue
        norm_k = normalize_zh(c_name)
        role = str(item.get('requirement_level') or 'bonus').strip()
        if role not in ('mandatory', 'bonus'):
            role = 'bonus'
        if norm_k not in by_key:
            entry = dict(item)
            entry['cert_name'] = c_name
            entry['requirement_level'] = role
            entry['requirement_roles'] = [role]
            by_key[norm_k] = entry
            merged.append(entry)
        else:
            existing = by_key[norm_k]
            roles = list(existing.get('requirement_roles') or [])
            if role not in roles:
                roles.append(role)
            roles.sort(key=lambda r: 0 if r == 'mandatory' else 1)
            existing['requirement_roles'] = roles
            if 'mandatory' in roles:
                existing['requirement_level'] = 'mandatory'
            q_new = str(item.get('quote') or '').strip()
            q_old = str(existing.get('quote') or '').strip()
            if q_new and q_new not in q_old:
                existing['quote'] = f"{q_old}；{q_new}".strip('；') if q_old else q_new
    return merged


def _format_cert_roles_label(roles: Optional[list[str]], level: str) -> str:
    role_set = set(roles or ())
    if 'mandatory' in role_set and 'bonus' in role_set:
        return '必交资格 / 加分参考'
    if level == 'mandatory' or 'mandatory' in role_set:
        return '必交资格项'
    if level == 'ledger_reference':
        return '台账备查项'
    return '加分/参考项'


def _lookup_cert_attachment_path(best_cert: dict[str, Any], ledger_certs: Optional[list[Any]]) -> Optional[str]:
    if not best_cert or not ledger_certs:
        return None
    target_id = best_cert.get('id')
    target_name = normalize_zh(str(best_cert.get('cert_name') or ''))
    for raw_c in ledger_certs:
        cid = _obj_field(raw_c, 'id')
        att_p = _obj_field(raw_c, 'attachment_path')
        if not att_p:
            continue
        if target_id and cid and int(cid) == int(target_id):
            return str(att_p)
        if target_name and normalize_zh(str(_obj_field(raw_c, 'cert_name') or '')) == target_name:
            return str(att_p)
    return None


def _resolve_embeddable_image_path(att_path: Optional[str]) -> Optional[Path]:
    raw = str(att_path or '').strip()
    if not raw:
        return None
    p = Path(raw)
    if '..' in p.parts:
        return None
    if p.suffix.lower() not in _CERT_IMAGE_EXTS:
        return None
    project_root = Path(__file__).resolve().parents[1]
    if p.is_absolute():
        resolved = p.resolve()
        return resolved if resolved.is_file() else None
    uploads_root = (project_root / 'data' / 'uploads').resolve()
    resolved = (project_root / p).resolve()
    if resolved.is_relative_to(uploads_root) and resolved.is_file():
        return resolved
    return None


def _format_decimal_yuan(raw_val: Any) -> Optional[str]:
    """用 Decimal（ROUND_HALF_UP）格式化金额字符串，禁用 float。"""
    if raw_val is None or isinstance(raw_val, bool):
        return None
    cleaned = re.sub(r'[,\s￥¥元]', '', str(raw_val).strip())
    if not cleaned:
        return None
    try:
        dec = Decimal(cleaned).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        return f"{dec:,.2f}"
    except (InvalidOperation, ValueError):
        return None


def _extract_first_match(text: str, patterns: tuple[re.Pattern, ...], max_window: int = 6000) -> Optional[str]:
    head = (text or '')[:max_window]
    for pat in patterns:
        m = pat.search(head)
        if m:
            val = re.sub(r'\s+', ' ', m.group(1)).strip(' ：:。；;,，')
            if val:
                return val
    return None


def _build_f4_lookup(f4_findings: Optional[list[Any]]) -> tuple[dict[str, Any], list[tuple[str, Any]]]:
    by_clause: dict[str, Any] = {}
    by_quote: list[tuple[str, Any]] = []
    for f in f4_findings or []:
        feedback = str(_obj_field(f, 'feedback', '') or '').lower()
        if feedback == 'rejected':
            continue
        cno = normalize_zh(str(_obj_field(f, 'clause_no', '') or ''))
        if cno and cno not in by_clause:
            by_clause[cno] = f
        q_norm = normalize_zh(str(_obj_field(f, 'quote', '') or ''))
        if q_norm:
            by_quote.append((q_norm, f))
    return by_clause, by_quote


def _match_f4_finding(
    req: dict[str, Any],
    by_clause: dict[str, Any],
    by_quote: list[tuple[str, Any]],
) -> Any:
    cno = normalize_zh(str(req.get('clause_no') or ''))
    if cno and cno in by_clause:
        return by_clause[cno]
    r_norm = normalize_zh(str(req.get('text') or req.get('full_text') or ''))
    if not r_norm:
        return None
    prefix = r_norm[:24]
    for q_norm, f in by_quote:
        if (prefix and prefix in q_norm) or (q_norm[:24] and q_norm[:24] in r_norm):
            return f
    return None


def _is_procedural_topic(text: str) -> bool:
    """判定条文是否属于投标人须知/目录存根/评标计分规则等非方案编写主题。"""
    s = re.sub(r'\s+', ' ', (text or '')).strip()
    if not s:
        return True
    if _TOC_DOT_LEADER_RE.search(s):
        return True
    if _PROCEDURAL_TOPIC_RE.search(s):
        return True
    if not re.search(r'(?:\d+\s*分|评分标准|技术要求|实施方案|售后|培训|验收|交付|质保)', s):
        if classify_invalid_clause_role(s, '') in ('submission_rule', 'eval_rule'):
            return True
    return False


def _extract_pdf_scoring_items(excerpts: list[str]) -> list[tuple[str, str, str]]:
    """从 PDF `评分细则` 文本块中抽取具体评分因素与分值（如 `实施方案 0-9 分`）。"""
    extracted: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for ex in excerpts:
        if _TOC_DOT_LEADER_RE.search(ex) and '评分细则' not in ex:
            continue
        lines = [ln.strip() for ln in ex.splitlines() if ln.strip()]
        for idx, ln in enumerate(lines):
            m_sc = _PDF_SCORE_LINE_RE.match(ln)
            if not m_sc:
                continue
            score_str = ln
            # 向上回溯 1~4 行拼合被 PDF 断行的评分因素标题
            title_parts: list[str] = []
            for back in range(idx - 1, max(-1, idx - 5), -1):
                cand = lines[back]
                if (
                    cand.isdigit()
                    or _PDF_SCORE_LINE_RE.match(cand)
                    or len(cand) > 20
                    or any(w in cand for w in ('序号', '评分因素', '评价指标', '分值', '评分细则', '。', '；', '：'))
                ):
                    break
                title_parts.insert(0, cand)
            factor_title = ''.join(title_parts).strip()
            if len(factor_title) < 2 or factor_title in ('合计', '总分', '小计'):
                continue
            if '价格' in factor_title:
                continue
            # 向下采集最多 6 行作为评分细节摘要
            desc_parts: list[str] = []
            for fwd in range(idx + 1, min(len(lines), idx + 7)):
                nxt = lines[fwd]
                if _PDF_SCORE_LINE_RE.match(nxt) or nxt.isdigit():
                    break
                desc_parts.append(nxt)
            desc = ' '.join(desc_parts).strip()
            if _is_procedural_topic(factor_title) or _is_procedural_topic(desc[:80]):
                continue
            k = normalize_zh(factor_title)
            if k and k not in seen:
                seen.add(k)
                extracted.append((factor_title, desc or factor_title, score_str))
    return extracted


def _collect_scoring_topics(checklist: dict[str, Any], text: str) -> list[dict[str, Any]]:
    """优先按评分细则提取方案主题（过滤目录与投标须知程序条文），并融合第四章全量采购需求。"""
    topics: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    def _add_topic(title: str, detail: str, score: str, source: str) -> None:
        clean_title = re.sub(r'\s+', ' ', (title or '').strip())
        clean_detail = re.sub(r'\s+', ' ', (detail or '').strip())
        if not clean_title and not clean_detail:
            return
        if not clean_title:
            clean_title = clean_detail[:36]
        if any(k in clean_title for k in ('合计', '总分', '总计', '小计')) and len(clean_detail) < 12:
            return
        dedup_k = normalize_zh(f"{clean_title[:24]}_{clean_detail[:24]}")
        if not dedup_k or dedup_k in seen_keys:
            return
        seen_keys.add(dedup_k)
        topics.append({
            'topic_id': f"topic_{len(topics) + 1}",
            'title': clean_title,
            'score_points': score or '以招标评分标准为准',
            'tender_quote': clean_detail[:280],
            'source': source,
        })

    for st in checklist.get('scoring_tables') or []:
        factor = str(st.get('factor') or '').strip()
        detail = str(st.get('detail') or '').strip()
        score = str(st.get('score') or '').strip()
        if _is_procedural_topic(factor):
            continue
        _add_topic(factor, detail, score, 'scoring_table')

    if len(topics) < 2:
        raw_hits = [str(sh.get('excerpt') or '') for sh in (checklist.get('scoring_hits') or []) if sh.get('excerpt')]
        # 优先处理明确含 `评分细则` / `评分因素` / 具体分值的正文窗，跳过前文目录引导页
        prioritized_hits = sorted(
            raw_hits,
            key=lambda ex: (
                0 if _TOC_DOT_LEADER_RE.search(ex[:180]) else 1,
                1 if ('评分细则' in ex or '评价指标' in ex or '评分因素' in ex) else 0,
                len(re.findall(r'\d+\s*分', ex)),
            ),
            reverse=True,
        )
        for f_title, f_desc, f_score in _extract_pdf_scoring_items(prioritized_hits):
            _add_topic(f_title, f_desc, f_score, 'scoring_pdf_block')
            if len(topics) >= 14:
                break

        if len(topics) < 4:
            for excerpt in prioritized_hits:
                if _TOC_DOT_LEADER_RE.search(excerpt[:180]) and '评分细则' not in excerpt:
                    continue
                for m in _EVAL_SUBITEM_RE.finditer(excerpt):
                    line = m.group(1).strip()
                    if len(line) < 8 or _is_procedural_topic(line):
                        continue
                    score_m = re.search(r'([（(]\s*\d+(?:\.\d+)?\s*分\s*[）)])', line)
                    _add_topic(
                        line[:40],
                        line,
                        score_m.group(1) if score_m else '评分要点',
                        'scoring_prose',
                    )
                    if len(topics) >= 14:
                        break
                if len(topics) >= 14:
                    break

    for ch4 in extract_chapter4_procurement_requirements(text):
        cno = str(ch4.get('clause_no') or '').strip()
        ctxt = str(ch4.get('text') or '').strip()
        first_line = ctxt.split('\n', 1)[0].strip()
        _add_topic(cno or first_line[:30], ctxt, '采购实质性需求', 'chapter4_procurement')
        if len(topics) >= 36:
            break

    if not topics:
        for fallback_title, fallback_desc in (
            ('项目理解与总体技术架构方案', '针对采购人核心业务需求与系统运行环境，明确总体架构与技术路线。'),
            ('项目实施组织、工期进度与验收方案', '明确实施阶段划分、人员配置、里程碑管控与验收交付标准。'),
            ('售后服务、应急响应与质保运维保障方案', '明确质保期限、故障响应时间、本地化运维与培训移交计划。'),
        ):
            _add_topic(fallback_title, fallback_desc, '待对照评分细则核对分值', 'default_outline')

    return topics


def _parse_json_tolerant(raw: str) -> Any:
    text = (raw or '').strip()
    if not text:
        return None
    fence = re.search(r'```(?:json)?\s*([\s\S]*?)```', text, flags=re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    for open_ch, close_ch in (('{', '}'), ('[', ']')):
        lo = text.find(open_ch)
        hi = text.rfind(close_ch)
        if lo >= 0 and hi > lo:
            try:
                return json.loads(text[lo:hi + 1])
            except Exception:
                continue
    return None


def _sanitize_ai_draft_text(draft_text: str, allowed_context: str) -> str:
    """E38 防幻觉确定性校验：禁止 AI 擅自捏造未在原文/参考库/台账中出现的金额与合同编号。"""
    if not draft_text:
        return ''
    norm_allowed = normalize_zh(allowed_context)

    def _clean_money(m: re.Match) -> str:
        raw_token = m.group(0)
        num_m = re.search(r'\d[\d,]*(?:\.\d+)?', raw_token)
        if num_m:
            digits = num_m.group(0).replace(',', '')
            if digits in norm_allowed:
                return raw_token
        return '[待人工核实金额]'

    def _clean_contract(m: re.Match) -> str:
        code_val = m.group(1)
        if normalize_zh(code_val) in norm_allowed:
            return m.group(0)
        return m.group(0).replace(code_val, '[待人工补充真实编号]')

    cleaned = _UNGROUNDED_MONEY_RE.sub(_clean_money, draft_text)
    cleaned = _UNGROUNDED_CONTRACT_RE.sub(_clean_contract, cleaned)
    return cleaned.strip()


def _resolve_skill_payload(skill: Any) -> tuple[Optional[int], str, str, dict[str, Any]]:
    if skill is None:
        spec = dict(DEFAULT_LAYOUT_SPEC)
        spec['append_checklist_at_end'] = True
        return None, '默认骨架（V1兼容）', '', spec
    sid = _obj_field(skill, 'id')
    sname = str(_obj_field(skill, 'name', '自定义写作Skill') or '自定义写作Skill').strip()
    s_inst = str(_obj_field(skill, 'writing_instructions', '') or '').strip()
    raw_layout = _obj_field(skill, 'layout_spec_json', None)
    if raw_layout is None:
        raw_layout = _obj_field(skill, 'layout_spec', {})
    return (
        int(sid) if sid is not None else None,
        sname,
        s_inst,
        normalize_layout_spec(raw_layout),
    )


def _plan_chapters_with_llm(
    client: Any,
    *,
    project_name: str,
    buyer_name: str,
    chapters_meta: list[dict[str, Any]],
    topics: list[dict[str, Any]],
    deviation_reqs: list[dict[str, Any]],
    skill_name: str,
    skill_instructions: str,
    skill_layout: dict[str, Any],
) -> Optional[list[dict[str, Any]]]:
    """调用大模型结合选定 Skill + 招标结构动态规划投标文件技术与服务章节树。"""
    if client is None or not topics:
        return None

    ch_titles = [str(c.get('title') or '').strip() for c in (chapters_meta or []) if c.get('title')][:30]
    topic_lines = [
        f"- [{t['topic_id']}] {t['title']}（{t['score_points']}）：{str(t.get('tender_quote') or '')[:90]}"
        for t in topics[:40]
    ]
    req_lines = [
        f"- {r.get('mark', '')}{r.get('clause_no', '')}：{str(r.get('text') or '')[:80]}"
        for r in deviation_reqs[:30]
    ]

    prompt = (
        "你是资深招投标方案架构师。请依据【选定写作 Skill】与本次招标信息，规划 3 至 6 个高度凝练、贴合招标评分架构的《技术与商务实施方案》动态章节（不得机械照抄或拆成十几个碎片小段）：\n"
        f"【项目名称】：{project_name}（采购人：{buyer_name}）\n"
        f"【招标原章节目录参考】：{', '.join(ch_titles) or '常规采购项目'}\n"
        f"【选定写作 Skill】：{skill_name}（语气：{skill_layout.get('tone')}，单章目标字数：{skill_layout.get('target_section_words')}）\n"
        f"【Skill 编写约束】：\n{skill_instructions[:1200] or '专业严谨点对点响应'}\n"
        f"【招标评分与需求要点】：\n" + '\n'.join(topic_lines) + "\n"
        f"【主要强制条款（供挂接检查）】：\n" + ('\n'.join(req_lines) or '无额外强制标记') + "\n"
        "请严格返回纯 JSON 对象：\n"
        '{"chapters": [{"chapter_no": "第一章", "title": "章节标题", "chapter_kind": "technical|implementation|service|security", "focus_topic_ids": ["topic_1"], "writing_brief": "本章撰写侧重点", "recommended_table": "可选建议表格名或空字符串"}]}'
    )
    valid_tids = {t['topic_id'] for t in topics}
    for _attempt in range(2):
        try:
            resp = client.chat([{'role': 'user', 'content': prompt}], model_tier='flash')
            parsed = _parse_json_tolerant(str(getattr(resp, 'content', '') or ''))
            raw_chs = parsed.get('chapters') if isinstance(parsed, dict) else parsed
            if not isinstance(raw_chs, list) or not raw_chs:
                continue

            covered_tids: set[str] = set()
            planned: list[dict[str, Any]] = []
            for idx, ch in enumerate(raw_chs[:8], start=1):
                if not isinstance(ch, dict):
                    continue
                title = str(ch.get('title') or '').strip()
                if not title:
                    continue
                f_ids = [str(x) for x in (ch.get('focus_topic_ids') or []) if str(x) in valid_tids]
                covered_tids.update(f_ids)
                planned.append({
                    'chapter_id': f"dyn_ch_{idx}",
                    'chapter_no': str(ch.get('chapter_no') or f"第{idx}部分").strip(),
                    'title': title[:80],
                    'chapter_kind': str(ch.get('chapter_kind') or 'technical'),
                    'focus_topic_ids': f_ids,
                    'writing_brief': str(ch.get('writing_brief') or '').strip()[:300],
                    'recommended_table': str(ch.get('recommended_table') or '').strip()[:60],
                })

            if not planned:
                continue

            # ★/评分点覆盖率硬校验：遗漏的 topic_id 自动挂接至最后一章或补充保障章，绝不漏分
            missing_tids = [t['topic_id'] for t in topics if t['topic_id'] not in covered_tids]
            if missing_tids:
                planned[-1]['focus_topic_ids'].extend(missing_tids)
            return planned
        except Exception:
            continue
    return None


def _strip_duplicate_leading_title(
    paragraphs: list[str],
    chapter_no: str,
    chapter_title: str,
) -> list[str]:
    """剔除 AI 在正文首段重复输出的章节号或本章标题，避免与 Word Heading 2 叠印。"""
    if not paragraphs:
        return []
    out = [str(p or '').strip() for p in paragraphs if str(p or '').strip()]
    if not out:
        return []

    title_norm = normalize_zh(chapter_title)
    full_norm = normalize_zh(f"{chapter_no}{chapter_title}")
    no_norm = normalize_zh(chapter_no)
    head_candidates = {x for x in (title_norm, full_norm) if len(x) >= 2}

    first = out[0]
    first_norm = normalize_zh(first)
    if first_norm in head_candidates:
        return out[1:] if len(out) > 1 else []

    if '\n' in first:
        first_line, rest = first.split('\n', 1)
        fl_norm = normalize_zh(first_line)
        if fl_norm in head_candidates or (no_norm and fl_norm.startswith(no_norm) and title_norm and title_norm in fl_norm and len(fl_norm) <= len(full_norm) + 4):
            rest_clean = rest.strip()
            if rest_clean:
                out[0] = rest_clean
            else:
                out = out[1:]
    return out


def _draft_chapter_with_llm(
    client: Any,
    *,
    chapter: dict[str, Any],
    topic_map: dict[str, dict[str, Any]],
    ref_snippets: list[dict[str, Any]],
    project_name: str,
    skill_name: str,
    skill_instructions: str,
    skill_layout: dict[str, Any],
    allowed_context_for_guard: str,
    cancel_check: Optional[Callable[[], None]] = None,
) -> dict[str, Any]:
    """为单个动态章节并发撰写正本草稿（支持单章瞬时失败最多 2 次自动重试与防造假校验）。"""
    if cancel_check is not None:
        cancel_check()

    mapped_topics = [topic_map[tid] for tid in (chapter.get('focus_topic_ids') or []) if tid in topic_map]
    req_desc = '\n'.join(
        f"- {t['title']}（{t['score_points']}）：{t.get('tender_quote', '')}"
        for t in mapped_topics
    ) or chapter.get('writing_brief', '')

    snip_desc = '\n'.join(
        f"[参考《{sn['source_filename']}》]：{sn['snippet'][:750]}"
        for sn in ref_snippets[:3]
    ) or '（无额外历史参考片段，请仅紧扣招标需求与规范方法论撰写）'

    target_words = int(skill_layout.get('target_section_words') or 500)
    prompt = (
        f"你是投标方案专家，正在为《{project_name}》撰写标书章节：{chapter['chapter_no']} {chapter['title']}。\n"
        f"【选定 Skill】：{skill_name}｜行文语气：{skill_layout.get('tone')}｜目标字数约 {target_words} 字。\n"
        f"【Skill 规则与格式限制】：\n{skill_instructions[:1200] or '正式专业、逐点响应'}\n"
        f"【本章对应招标需求与评分细则】：\n{req_desc[:2200]}\n"
        f"【可借鉴企业参考片段（如有）】：\n{snip_desc[:2400]}\n"
        f"【建议附带表格（可选）】：{chapter.get('recommended_table') or '如适合则给出实施分工/SLA承诺表'}\n"
        "严格约束：\n"
        "1. 严禁虚构任何投标报价金额、过往合同编号或未授权业绩年份；\n"
        "2. 用实实在在的可执行方案语言撰写正文，少喊口号，需企业人工核实的具体配置填 `[待人工确认：...]`；\n"
        "3. `paragraphs` 首段直接写正文内容，严禁在首行重复输出章节编号或本章标题；\n"
        "4. 返回纯 JSON：{\"paragraphs\": [\"分段正文1\", \"分段正文2\"], \"table\": {\"title\": \"表名\", \"headers\": [\"列1\", \"列2\", \"列3\"], \"rows\": [[\"a\", \"b\", \"c\"]]} | null, \"needs_manual\": [\"待确认项1\"]}"
    )

    guard_pool = f"{allowed_context_for_guard}\n{req_desc}\n{snip_desc}"
    for _attempt in range(3):
        if cancel_check is not None:
            cancel_check()
        try:
            resp = client.chat([{'role': 'user', 'content': prompt}], model_tier='flash')
            parsed = _parse_json_tolerant(str(getattr(resp, 'content', '') or ''))
            if isinstance(parsed, dict) and isinstance(parsed.get('paragraphs'), list) and parsed['paragraphs']:
                sanitized_paras = [
                    _sanitize_ai_draft_text(str(p or ''), guard_pool)
                    for p in parsed['paragraphs']
                    if str(p or '').strip()
                ]
                clean_paras = _strip_duplicate_leading_title(
                    sanitized_paras,
                    str(chapter.get('chapter_no') or ''),
                    str(chapter.get('title') or ''),
                )
                if not clean_paras:
                    continue
                clean_table = None
                raw_tbl = parsed.get('table')
                if isinstance(raw_tbl, dict) and isinstance(raw_tbl.get('headers'), list) and isinstance(raw_tbl.get('rows'), list):
                    hdrs = [str(h or '').strip() for h in raw_tbl['headers'] if str(h or '').strip()]
                    if 2 <= len(hdrs) <= 6:
                        rows = [
                            [_sanitize_ai_draft_text(str(c or ''), guard_pool) for c in r[:len(hdrs)]]
                            for r in raw_tbl['rows'][:36]
                            if isinstance(r, list) and r
                        ]
                        if rows:
                            clean_table = {
                                'title': str(raw_tbl.get('title') or chapter.get('recommended_table') or '方案明细表').strip(),
                                'headers': hdrs,
                                'rows': [r + [''] * (len(hdrs) - len(r)) for r in rows],
                            }
                manual_notes = [str(m or '').strip() for m in (parsed.get('needs_manual') or []) if str(m or '').strip()][:5]
                return {
                    'ai_generated': True,
                    'paragraphs': clean_paras,
                    'table': clean_table,
                    'needs_manual': manual_notes,
                }
        except Exception:
            continue

    return {
        'ai_generated': False,
        'paragraphs': [
            f"围绕「{chapter['title']}」，对照招标文件要求（{chapter.get('writing_brief') or '见对应评分要点'}）开展总体设计、分阶段实施路径及质量控制措施 [待人工补充：结合公司核心产品截图及详尽实施细则展开]。"
        ],
        'table': None,
        'needs_manual': [],
    }


def _draft_deviation_with_llm(
    client: Any,
    dev_rows: list[dict[str, Any]],
    *,
    skill_name: str,
    skill_instructions: str,
    allowed_context: str,
    cancel_check: Optional[Callable[[], None]] = None,
) -> dict[int, str]:
    """为尚未完成 F4 体检的偏离表行分批（每批 12 条）全量拟定正面技术/商务响应草稿。"""
    targets = [
        r for r in dev_rows
        if r.get('deviation') == '[待人工核对]'
    ]
    if client is None or not targets:
        return {}

    out: dict[int, str] = {}
    batch_size = 12
    for offset in range(0, len(targets), batch_size):
        if cancel_check is not None:
            cancel_check()
        batch = targets[offset:offset + batch_size]
        items_txt = '\n'.join(
            f"seq={r['seq']}｜条款={r['clause_no']}｜要求={str(r['tender_requirement'])[:160]}"
            for r in batch
        )
        prompt = (
            f"你是资深投标文件撰写助手（风格：{skill_name}）。请为以下招标实质条款逐条撰写简洁明确的“我方响应草稿”（每条 20~60 字，明确肯定完全满足并说明响应点；若必须附第三方证明请附 `[待人工核附：证明页码]`；严禁编造报价或证书编号）：\n"
            f"Skill要求：{skill_instructions[:600]}\n"
            f"{items_txt}\n"
            '返回纯 JSON：{"responses": [{"seq": 1, "response": "完全响应：..."}]}'
        )
        try:
            resp = client.chat([{'role': 'user', 'content': prompt}], model_tier='flash')
            parsed = _parse_json_tolerant(str(getattr(resp, 'content', '') or ''))
            items = parsed.get('responses') if isinstance(parsed, dict) else parsed
            if isinstance(items, list):
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    try:
                        seq = int(it.get('seq'))
                    except (TypeError, ValueError):
                        continue
                    txt = _sanitize_ai_draft_text(str(it.get('response') or '').strip(), allowed_context)
                    if txt:
                        out[seq] = txt
        except Exception:
            continue
    return out


def _align_snippet_with_llm_optional(
    client: Any,
    topic: dict[str, Any],
    snippet: str,
    tender_text: str,
) -> Optional[str]:
    """可选 LLM 术语对齐改写（仅吃局部窗口，禁引入未在原文/片段出现的数字或年号）。"""
    if client is None or not snippet:
        return None
    prompt = (
        "你是投标技术编制助手。请仅基于给定【招标要求】与【企业参考片段】做术语对齐整理：\n"
        "1. 严禁编造新的合同金额、业绩年份、认证名称或具体指标数值；\n"
        "2. 缺口部分必须保留 `[待人工补充：...]`；\n"
        "3. 直接输出整理后的 1 段技术应答草稿，不超过 300 字。\n"
        f"【招标要求】：{topic.get('title')} — {topic.get('tender_quote')}\n"
        f"【企业参考片段】：{snippet}"
    )
    try:
        resp = client.chat([{'role': 'user', 'content': prompt}], model_tier='flash')
        content = str(getattr(resp, 'content', '') or '').strip()
        if not content or len(content) < 12:
            return None
        allowed_nums = set(re.findall(r'\d{2,}', f"{snippet} {topic.get('tender_quote') or ''}"))
        new_nums = set(re.findall(r'\d{2,}', content))
        if new_nums - allowed_nums:
            return None
        return content
    except Exception:
        return None


def compose_bid_blueprint(
    tender_path: str | Path,
    *,
    ledger_certs: Optional[list[Any]] = None,
    rule_config: Optional[Any] = None,
    f4_findings: Optional[list[Any]] = None,
    task_title: Optional[str] = None,
    package_no: Optional[int] = None,
    compose_mode: str = 'skeleton_v1',
    knowledge_provider: Optional[KnowledgeSnippetProvider] = None,
    knowledge_paths: Optional[list[str | Path]] = None,
    client: Any = None,
    checklist: Optional[dict[str, Any]] = None,
    skill: Any = None,
    use_ai: bool = True,
    progress_cb: Optional[Callable[[int, str], None]] = None,
    cancel_check: Optional[Callable[[], None]] = None,
) -> BidBlueprint:
    """E37 + E38：生成受写作 Skill 与可选 AI 驱动的统一投标蓝图 IR（严格守住报价/证照/覆盖三铁律）。"""
    if compose_mode not in ('skeleton_v1', 'knowledge_draft_v2'):
        raise ValueError(f"不支持的编制模式 compose_mode={compose_mode!r}")

    t_path = Path(tender_path)
    if not t_path.is_file():
        raise FileNotFoundError(f"招标文件不存在：{tender_path}")

    def _emit_progress(pct: int, detail: str) -> None:
        if cancel_check is not None:
            cancel_check()
        if progress_cb is not None:
            progress_cb(pct, detail)

    skill_id, skill_name, skill_instructions, skill_layout = _resolve_skill_payload(skill)

    _emit_progress(12, '阶段 1/4：解析招标文件元信息与企业证照台账')
    doc = parse_document(str(t_path))
    full_text = doc.text or ''
    page_ctx = build_unified_page_context(str(t_path))

    if checklist is None:
        checklist = build_review_checklist(str(t_path))

    extracted_name = _extract_first_match(full_text, _PROJECT_NAME_RES)
    project_name = extracted_name or (task_title or '').strip() or t_path.stem
    extracted_code = _extract_first_match(full_text, _PROJECT_CODE_RES)
    project_code = extracted_code or '[待人工填报：招标项目编号]'
    buyer_name = (
        _extract_first_match(full_text, _BUYER_NAME_RES)
        or str(_obj_field(rule_config, 'buyer_name', '') or '').strip()
        or '[待人工填报：采购人名称]'
    )

    open_date = checklist.get('open_date') or _obj_field(rule_config, 'open_date')
    if open_date:
        open_date = str(open_date).strip()
        open_date_source = checklist.get('open_date_source') or 'rule_config'
    else:
        open_date = None
        open_date_source = 'missing'

    raw_rule_cap = _obj_field(rule_config, 'price_cap')
    formatted_cap = _format_decimal_yuan(raw_rule_cap) if raw_rule_cap else None
    if formatted_cap is None:
        extracted_cap = extract_price_cap(full_text, package_no=package_no)
        formatted_cap = _format_decimal_yuan(extracted_cap) if extracted_cap else None

    manual_checklist: list[dict[str, Any]] = []

    def _add_manual(section_id: str, field_name: str, reason: str, hint: str) -> dict[str, Any]:
        item = {
            'section_id': section_id,
            'field': field_name,
            'reason': reason,
            'hint': hint,
        }
        manual_checklist.append(item)
        return item

    # ── Section 1: 封面与投标函骨架 ──────────────────────────────────────────
    s1_manual = [
        _add_manual('sec_cover', '投标人全称与公章', '需按正式参选主体填写并加盖公章', '请填写入围投标单位法定全称'),
        _add_manual('sec_cover', '法定代表人/授权代表签章', '封标前须完成法人签章及授权委托书签署', '核对授权书有效期'),
        _add_manual('sec_cover', '投标有效期承诺天数', '需满足招标文件前附表规定最低天数', '例如：自开标之日起不少于 90 日历天'),
    ]
    if not extracted_code:
        s1_manual.append(_add_manual('sec_cover', '招标项目编号', '未在招标前文正则命中明确项目编号', '请对照招标公告人工补填'))
    if open_date:
        pass
    else:
        s1_manual.append(_add_manual('sec_cover', '开标日期', '未提取到明确开标日期', '请对照招标公告人工补填'))

    sec_cover = BlueprintSection(
        section_id='sec_cover',
        title='第一部分 投标函与项目基本响应信息',
        slot_type='cover_and_letter',
        fill_mode='deterministic',
        tender_requirements=[],
        content_blocks=[
            {'kind': 'meta', 'label': '项目名称', 'value': project_name, 'highlight': False},
            {'kind': 'meta', 'label': '项目编号', 'value': project_code, 'highlight': extracted_code is None},
            {'kind': 'meta', 'label': '采购人/招标人', 'value': buyer_name, 'highlight': buyer_name.startswith('[待人工')},
            {'kind': 'meta', 'label': '分包信息', 'value': f"第 {package_no} 包" if package_no else '整包/默认包', 'highlight': False},
            {'kind': 'meta', 'label': '开标日期', 'value': open_date or '[待人工核对：开标日期]', 'highlight': open_date is None},
            {'kind': 'meta', 'label': '最高限价参考', 'value': f"{formatted_cap} 元" if formatted_cap else '[待人工核对：招标控制价]', 'highlight': formatted_cap is None},
            {'kind': 'placeholder', 'label': '投标人名称（盖章）', 'value': '[待人工填报：投标人法定全称（加盖公章）]', 'highlight': True},
            {'kind': 'placeholder', 'label': '法定代表人或委托代理人', 'value': '[待人工填报：签字或盖章]', 'highlight': True},
            {'kind': 'paragraph', 'text': f"致 {buyer_name}：我方已全面研读《{project_name}》招标文件及全部补充澄清通知，充分理解并承诺遵守招标文件全部实质性条款要求。本项目投标有效期为：[待人工填报，须不少于招标文件规定天数]。", 'highlight': True},
        ],
        needs_manual_items=s1_manual,
        source_refs=[{'file': t_path.name, 'open_date_source': open_date_source}],
    )

    # ── Section 2: 资格审查与企业证照核对清单（带角色去重合并 + 原件附图索引）────
    required_certs = checklist.get('required_certificates') or []
    if not required_certs and full_text:
        required_certs = extract_required_certificates(full_text, page_ctx=page_ctx)

    merged_required_certs = _merge_required_certificates_by_role(required_certs)
    roles_by_name = {
        normalize_zh(it['cert_name']): list(it.get('requirement_roles') or [])
        for it in merged_required_certs
    }

    matched_certs, cert_summary = match_required_certificates_with_ledger(
        merged_required_certs,
        ledger_certs or [],
        reference_date=open_date,
    )

    s2_blocks: list[dict[str, Any]] = []
    s2_manual: list[dict[str, Any]] = []
    for mc in matched_certs:
        c_name = str(mc.get('cert_name') or '')
        status = str(mc.get('ledger_status') or 'not_in_ledger')
        best = mc.get('matched_cert') or {}
        req_roles = roles_by_name.get(normalize_zh(c_name)) or [mc.get('requirement_level', 'bonus')]
        role_label = _format_cert_roles_label(req_roles, str(mc.get('requirement_level') or 'bonus'))
        att_path = _lookup_cert_attachment_path(best, ledger_certs)
        has_img = _resolve_embeddable_image_path(att_path) is not None

        if status == 'valid':
            validity = '长期有效' if best.get('is_long_term') else f"有效期至 {best.get('valid_until')}"
            cert_no = best.get('cert_no') or '见原件'
            if has_img:
                att_tip = f"已关联台账原件扫描图《{best.get('attachment_name')}》（导出时已自动嵌入附页）"
            elif best.get('has_attachment'):
                att_tip = f"已关联台账原件《{best.get('attachment_name')}》（可从规则库下载插入）"
            else:
                att_tip = '[待人工补充：台账未附扫描件，请线下插入原件]'
            status_text = f"台账有效（证号：{cert_no}，{validity}）· {att_tip}"
            highlight = not bool(best.get('has_attachment'))
        elif status == 'expiring_soon':
            status_text = f"[预警：证照将于 {best.get('valid_until')} 临期（距开标基准剩余 {best.get('days_remaining')} 天），请确认续期]"
            highlight = True
            s2_manual.append(_add_manual('sec_qual', f"临期证照：{c_name}", status_text, '请核对开标日是否在有效期内或换发新证'))
        elif status == 'expired':
            status_text = f"[红线预警：台账证照已于 {best.get('valid_until')} 过期，请立即更新]"
            highlight = True
            s2_manual.append(_add_manual('sec_qual', f"过期证照：{c_name}", status_text, '过期证照将直接触发资格废标，请立即补传新证'))
        else:
            status_text = '[台账缺失，请补充上传：未在企业证照台账匹配到该项]'
            highlight = True
            s2_manual.append(_add_manual('sec_qual', f"缺失证照：{c_name}", '企业台账暂无此证照记录', '请前往「废标规则与证照底表」补录并粘贴原件扫描件'))

        s2_blocks.append({
            'kind': 'cert_row',
            'cert_name': c_name,
            'requirement_level': mc.get('requirement_level', 'bonus'),
            'requirement_roles': req_roles,
            'role_label': role_label,
            'matched_cert_id': best.get('id'),
            'ledger_status': status,
            'cert_no': best.get('cert_no'),
            'valid_until': '长期有效' if best.get('is_long_term') else best.get('valid_until'),
            'is_long_term': bool(best.get('is_long_term', False)),
            'attachment_name': best.get('attachment_name'),
            'attachment_path': att_path,
            'has_attachment': bool(best.get('has_attachment', False)),
            'has_embedded_image': has_img,
            'display_status': status_text,
            'quote': mc.get('quote', ''),
            'page': mc.get('page'),
            'highlight': highlight,
        })

    if not matched_certs:
        s2_manual.append(_add_manual('sec_qual', '资格文件核对', '未自动识别出细分证照清单，请人工核对招标书资格审查表', '对照招标文件第三章资格审查要求逐项附证'))
        for raw_c in ledger_certs or []:
            c_name = str(_obj_field(raw_c, 'cert_name', '') or '').strip()
            if not c_name:
                continue
            is_long = bool(_obj_field(raw_c, 'is_long_term', False))
            vu = _obj_field(raw_c, 'valid_until')
            vu_str = '长期有效' if is_long else (vu.isoformat() if hasattr(vu, 'isoformat') else str(vu or ''))
            att_p = str(_obj_field(raw_c, 'attachment_path') or '').strip() or None
            has_img = _resolve_embeddable_image_path(att_p) is not None
            s2_blocks.append({
                'kind': 'cert_row',
                'cert_name': c_name,
                'requirement_level': 'ledger_reference',
                'requirement_roles': ['ledger_reference'],
                'role_label': '台账备查项',
                'matched_cert_id': _obj_field(raw_c, 'id'),
                'ledger_status': 'valid' if is_long else 'in_ledger',
                'cert_no': _obj_field(raw_c, 'cert_no'),
                'valid_until': vu_str,
                'is_long_term': is_long,
                'attachment_name': _obj_field(raw_c, 'attachment_name'),
                'attachment_path': att_p,
                'has_attachment': bool(att_p or _obj_field(raw_c, 'has_attachment')),
                'has_embedded_image': has_img,
                'display_status': f"企业台账备查（{vu_str}）",
                'highlight': False,
            })

    sec_qual = BlueprintSection(
        section_id='sec_qual',
        title='第二部分 资格审查与企业资质证明文件',
        slot_type='qualification_checklist',
        fill_mode='cert_ledger',
        tender_requirements=merged_required_certs,
        content_blocks=s2_blocks,
        needs_manual_items=s2_manual,
        source_refs=[{'cert_summary': cert_summary}],
    )

    # ── Section 3: 开标一览表与报价锁定区（严禁 AI 代填报价）──────────────────
    total_placeholder = (
        f"[待人工填报金额，当前参考限价：{formatted_cap} 元]"
        if formatted_cap
        else "[待人工填报金额，当前未自动识别最高限价请人工对照招标公告核填]"
    )
    upper_placeholder = "[待人工填报人民币大写金额，须与小写总价及分项汇总表 100% 一致]"
    s3_manual = [
        _add_manual('sec_price', '开标一览表投标总价（小写/大写）', '合规红线禁止 AI 自动填报报价数字，须商务负责人亲填并做算术核验', f"注意不可超过参考限价：{formatted_cap} 元" if formatted_cap else '请人工确认是否设最高限价'),
        _add_manual('sec_price', '分项报价明细表', '须逐项填写单价×数量小计并保证全表加和等于开标总价', '定稿后可上传体检跑 F2 算术沙盒复核'),
    ]
    sec_price = BlueprintSection(
        section_id='sec_price',
        title='第三部分 开标一览表与分项报价占位（合规锁账）',
        slot_type='price_placeholder',
        fill_mode='deterministic',
        tender_requirements=[{'price_cap': formatted_cap, 'package_no': package_no}],
        content_blocks=[
            {'kind': 'price_row', 'label': '项目名称', 'value': project_name, 'highlight': False},
            {'kind': 'price_row', 'label': '投标总报价（人民币小写）', 'value': total_placeholder, 'highlight': True},
            {'kind': 'price_row', 'label': '投标总报价（人民币大写）', 'value': upper_placeholder, 'highlight': True},
            {'kind': 'price_row', 'label': '质保期与交付期', 'value': '[待人工填报：与技术商务响应及投标函保持完全一致]', 'highlight': True},
            {'kind': 'paragraph', 'text': '合规提示：本部分强制屏蔽 AI 测算与示例数字，请商务人员按真实成本填入报价并利用 F2 算术沙盒复验。', 'highlight': False},
        ],
        needs_manual_items=s3_manual,
        source_refs=[{'price_cap_yuan': formatted_cap}],
    )

    # ── Section 4: ★/▲ 实质性条款响应与偏离表（修复 E37 eval_rule/评分细则串行 bug）──
    raw_marked = _filter_valid_requirements(list(checklist.get('marked_clauses') or []))
    # 严禁将 submission_rule（递交纪律）或 eval_rule（评标委员会审查细则，如"6、报价过低"）误混入偏离表
    raw_invalid = [
        r for r in (checklist.get('invalid_bid_clauses') or [])
        if classify_invalid_clause_role(str(r.get('text') or ''), str(r.get('clause_no') or ''))
        not in ('submission_rule', 'eval_rule')
    ]
    combined_reqs: list[dict[str, Any]] = []
    seen_req_norm: set[str] = set()
    for req in raw_marked + raw_invalid:
        txt = _SCORING_BLEED_RE.sub('', str(req.get('full_text') or req.get('text') or '')).strip()
        if len(txt) < 6:
            continue
        k = normalize_zh(txt)[:60]
        if k in seen_req_norm:
            continue
        seen_req_norm.add(k)
        req_copy = dict(req)
        req_copy['text'] = txt
        combined_reqs.append(req_copy)

    by_clause, by_quote = _build_f4_lookup(f4_findings)
    s4_blocks: list[dict[str, Any]] = []
    s4_manual: list[dict[str, Any]] = []
    for idx, req in enumerate(combined_reqs, start=1):
        cno = str(req.get('clause_no') or f"条款{idx}").strip()
        mark = str(req.get('mark') or '').strip()
        req_text = str(req.get('text') or '').strip()
        page_num = req.get('page')
        if page_num is None and req.get('start') is not None:
            page_num = resolve_unified_page(int(req['start']), page_ctx)[0]

        matched_f = _match_f4_finding(req, by_clause, by_quote)
        if matched_f is not None:
            f_sev = str(_obj_field(matched_f, 'severity', '') or '').lower()
            f_code = str(_obj_field(matched_f, 'code', '') or '').upper()
            f_sug = str(_obj_field(matched_f, 'suggestion', '') or _obj_field(matched_f, 'message', '') or '').strip()
            f_bid_q = str(_obj_field(matched_f, 'bid_quote', '') or '').strip()
            if f_sev in ('error', 'warning') or f_code in ('MISSING_RESPONSE', 'CONTRADICTION'):
                resp_text = f"[待人工整改（关联F4体检 {f_code}）：{f_sug[:100]}]"
                dev_label = '需人工补齐/整改'
                highlight = True
                s4_manual.append(_add_manual('sec_deviation', f"偏离项 {mark}{cno}", f_code or 'F4风险', f_sug[:120]))
            else:
                resp_text = f_bid_q or '完全响应招标文件该条款要求。'
                dev_label = '无偏离（已引用体检记录）'
                highlight = False
        else:
            resp_text = '[待人工核对应答：须逐项填写实质性响应承诺并附证明索引]'
            dev_label = '[待人工核对]'
            highlight = True
            s4_manual.append(_add_manual('sec_deviation', f"实质条款 {mark}{cno}", '需填写具体投标响应', req_text[:60]))

        s4_blocks.append({
            'kind': 'deviation_row',
            'seq': idx,
            'clause_no': f"{mark}{cno}" if (mark and mark not in cno) else cno,
            'tender_requirement': req_text[:300],
            'bid_response': resp_text,
            'deviation': dev_label,
            'page': page_num,
            'highlight': highlight,
        })

    if not s4_blocks:
        s4_manual.append(_add_manual('sec_deviation', '实质性条款核对', '未提取到带 ★/▲ 或模式 A/B 强制条款，请人工核对招标要求后填表', '核对第四章采购需求'))

    # ── 准备共享可信数值池（用于 AI 防造假数字守卫）──────────────────────────
    allowed_guard_parts = [full_text[:15000]]
    for c in ledger_certs or []:
        allowed_guard_parts.append(str(_obj_field(c, 'cert_name', '') or ''))
        allowed_guard_parts.append(str(_obj_field(c, 'cert_no', '') or ''))
    allowed_guard_text = '\n'.join(allowed_guard_parts)

    ai_active = bool(use_ai and client is not None)
    ai_status = 'skipped'

    if ai_active and s4_blocks:
        _emit_progress(28, '阶段 2/4：AI 按选定 Skill 拟定实质条款响应草稿')
        drafted_dev = _draft_deviation_with_llm(
            client,
            s4_blocks,
            skill_name=skill_name,
            skill_instructions=skill_instructions,
            allowed_context=allowed_guard_text,
            cancel_check=cancel_check,
        )
        for r in s4_blocks:
            seq_no = r['seq']
            if seq_no in drafted_dev:
                r['bid_response'] = f"{drafted_dev[seq_no]} [AI草稿·待确认]"
                r['deviation'] = '无偏离（AI草稿·待核对）'

    sec_deviation = BlueprintSection(
        section_id='sec_deviation',
        title='第四部分 实质性条款（★/▲）响应与技术/商务偏离表',
        slot_type='deviation_table',
        fill_mode='ai_skill_draft' if (ai_active and s4_blocks) else 'deviation_table',
        tender_requirements=combined_reqs,
        content_blocks=s4_blocks,
        needs_manual_items=s4_manual,
        source_refs=[{'total_requirements': len(s4_blocks)}],
    )

    # ── Section 5: 技术与商务实施方案（AI Skill 动态规划 + V2 参考方案库）──────
    topics = _collect_scoring_topics(checklist, full_text)
    provider = knowledge_provider
    if provider is None and compose_mode == 'knowledge_draft_v2' and knowledge_paths:
        provider = LocalKnowledgeSnippetProvider(list(knowledge_paths), client=client)

    use_rag = (
        provider is not None
        and bool(getattr(provider, 'has_documents', False))
    )

    topic_map = {t['topic_id']: t for t in topics}
    topic_snippets: dict[str, list[dict[str, Any]]] = {}
    s5_refs: list[dict[str, Any]] = []
    matched_rag_count = 0
    matched_indicator_count = 0

    for t in topics:
        query_text = f"{t['title']} {t['tender_quote']}".strip()
        ref_snippets: list[dict[str, Any]] = []
        if use_rag:
            raw_hits = provider.retrieve(query_text, top_k=2, min_score=0.35)
            has_ind_hit = False
            for h in raw_hits:
                snip = str(h.get('snippet') or '').strip()
                if not snip:
                    continue
                is_ind = bool(h.get('is_indicator_ref', False))
                if is_ind:
                    has_ind_hit = True
                aligned = (
                    _align_snippet_with_llm_optional(client, t, snip, full_text)
                    if (not ai_active and not is_ind)
                    else None
                )
                src_fn = str(h.get('source_filename') or '参考方案库')
                custom_notice = str(h.get('notice') or '').strip()
                ref_snippets.append({
                    'source_filename': src_fn,
                    'snippet': aligned or snip,
                    'raw_quote': snip,
                    'score': float(h.get('score') or 0.0),
                    'quote_verified': bool(h.get('quote_verified', True)),
                    'llm_aligned': aligned is not None,
                    'is_indicator_ref': is_ind,
                    'indicator_id': h.get('indicator_id'),
                    'scoring_ref': h.get('scoring_ref'),
                    'notice': custom_notice or f"[参考片段 · 来源《{src_fn}》 · 待人工确认改写，严禁直接照搬年份与金额]",
                })
                s5_refs.append({
                    'topic_id': t['topic_id'],
                    'source_filename': src_fn,
                    'score': float(h.get('score') or 0.0),
                    'is_indicator_ref': is_ind,
                    'indicator_id': h.get('indicator_id'),
                })
            if has_ind_hit:
                matched_indicator_count += 1
        if ref_snippets:
            matched_rag_count += 1
        topic_snippets[t['topic_id']] = ref_snippets

    s5_blocks: list[dict[str, Any]] = []
    s5_manual: list[dict[str, Any]] = []
    planned_chapters: list[dict[str, Any]] = []

    if ai_active:
        _emit_progress(40, f"阶段 3/4：AI 依据「{skill_name}」动态规划标书技术章节")
        planned = _plan_chapters_with_llm(
            client,
            project_name=project_name,
            buyer_name=buyer_name,
            chapters_meta=list(checklist.get('chapters') or []),
            topics=topics,
            deviation_reqs=combined_reqs,
            skill_name=skill_name,
            skill_instructions=skill_instructions,
            skill_layout=skill_layout,
        )
        if planned:
            planned_chapters = planned
            draft_results: dict[str, dict[str, Any]] = {}
            total_ch = len(planned_chapters)
            done_cnt = 0

            def _run_one(ch_item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
                ch_refs: list[dict[str, Any]] = []
                seen_sn: set[str] = set()
                for tid in ch_item.get('focus_topic_ids') or []:
                    for sn in topic_snippets.get(tid, []):
                        k = normalize_zh(sn.get('snippet', ''))[:50]
                        if k and k not in seen_sn:
                            seen_sn.add(k)
                            ch_refs.append(sn)
                res_d = _draft_chapter_with_llm(
                    client,
                    chapter=ch_item,
                    topic_map=topic_map,
                    ref_snippets=ch_refs,
                    project_name=project_name,
                    skill_name=skill_name,
                    skill_instructions=skill_instructions,
                    skill_layout=skill_layout,
                    allowed_context_for_guard=allowed_guard_text,
                    cancel_check=cancel_check,
                )
                return ch_item['chapter_id'], res_d

            with ThreadPoolExecutor(max_workers=min(4, max(1, total_ch))) as pool:
                futs = [pool.submit(_run_one, ch) for ch in planned_chapters]
                try:
                    for fut in futs:
                        cid, d_res = fut.result()
                        draft_results[cid] = d_res
                        done_cnt += 1
                        pct = 45 + int((done_cnt / max(1, total_ch)) * 40)
                        _emit_progress(pct, f"阶段 3/4：AI 正在撰写方案章节（{done_cnt}/{total_ch}）")
                except BaseException:
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise

            ai_ok_cnt = 0
            for ch_item in planned_chapters:
                cid = ch_item['chapter_id']
                d_res = draft_results.get(cid) or {'ai_generated': False, 'paragraphs': [], 'table': None, 'needs_manual': []}
                if d_res.get('ai_generated'):
                    ai_ok_cnt += 1
                ch_refs: list[dict[str, Any]] = []
                for tid in ch_item.get('focus_topic_ids') or []:
                    ch_refs.extend(topic_snippets.get(tid, [])[:1])
                mapped_titles = [topic_map[tid]['title'] for tid in ch_item.get('focus_topic_ids') or [] if tid in topic_map]
                for mn in d_res.get('needs_manual') or []:
                    s5_manual.append(_add_manual('sec_scoring', f"{ch_item['chapter_no']} {ch_item['title'][:20]}", mn, 'AI提示需人工补齐或核验'))
                if not d_res.get('needs_manual'):
                    s5_manual.append(_add_manual('sec_scoring', f"审校 AI 章节：{ch_item['title'][:24]}", 'AI 辅助撰写正本草稿需技术负责人结合项目实际参数终核', skill_name))

                s5_blocks.append({
                    'kind': 'ai_chapter',
                    'chapter_id': cid,
                    'chapter_no': ch_item['chapter_no'],
                    'title': ch_item['title'],
                    'chapter_kind': ch_item.get('chapter_kind', 'technical'),
                    'covered_topics': mapped_titles,
                    'paragraphs': d_res.get('paragraphs') or [],
                    'table': d_res.get('table'),
                    'reference_snippets': ch_refs[:2],
                    'ai_generated': bool(d_res.get('ai_generated')),
                    'highlight': not bool(d_res.get('ai_generated')),
                })
            ai_status = 'done' if ai_ok_cnt == len(planned_chapters) else ('partial' if ai_ok_cnt > 0 else 'fallback_error')
        else:
            ai_status = 'fallback_error'

    if not s5_blocks:
        # 确定性兜底大纲与 V2 参考片段（保 E37 原有结构与测试断言 100% 兼容）
        for t in topics:
            ref_snippets = topic_snippets.get(t['topic_id'], [])
            sub_outline = [
                f"1. 需求响应与建设目标理解（对应得分点：{t['score_points']}）",
                "2. 具体技术路线、架构设计与实施细则 [待人工补充：结合本项目参数展开]",
                "3. 交付验收标准、风险应急预案与佐证截图索引 [待人工补充：附系统截图/检测报告]",
            ]
            if ref_snippets:
                s5_manual.append(_add_manual('sec_scoring', f"确认改写参考片段：{t['title'][:28]}", 'V2 方案库检索片段须经技术负责人去重改写与参数校准', ref_snippets[0]['source_filename']))
            else:
                s5_manual.append(_add_manual('sec_scoring', f"编制方案正文：{t['title'][:28]}", 'V1 评分要点大纲需人工填充具体技术措施与证明材料', t['score_points']))
            s5_blocks.append({
                'kind': 'scoring_topic',
                'topic_id': t['topic_id'],
                'title': t['title'],
                'score_points': t['score_points'],
                'tender_quote': t['tender_quote'],
                'sub_outline': sub_outline,
                'reference_snippets': ref_snippets,
                'highlight': True,
            })

    if ai_status in ('done', 'partial'):
        sec_scoring_mode = 'ai_skill_rag' if (use_rag and matched_rag_count > 0) else 'ai_skill_draft'
    else:
        sec_scoring_mode = 'rag_knowledge' if (use_rag and matched_rag_count > 0) else 'outline_stub'

    sec_scoring = BlueprintSection(
        section_id='sec_scoring',
        title='第五部分 技术与商务实施方案',
        slot_type='scoring_response_outline',
        fill_mode=sec_scoring_mode,
        tender_requirements=topics,
        content_blocks=s5_blocks,
        needs_manual_items=s5_manual,
        source_refs=s5_refs,
    )

    # ── Section 6: 人工待填项汇总与台账备查附录 ──────────────────────────────
    ledger_refs: list[dict[str, Any]] = []
    for raw_c in ledger_certs or []:
        c_name = str(_obj_field(raw_c, 'cert_name', '') or '').strip()
        if not c_name:
            continue
        is_long = bool(_obj_field(raw_c, 'is_long_term', False))
        vu = _obj_field(raw_c, 'valid_until')
        vu_str = '长期有效' if is_long else (vu.isoformat() if hasattr(vu, 'isoformat') else str(vu or ''))
        ledger_refs.append({
            'kind': 'ledger_appendix_row',
            'cert_name': c_name,
            'cert_no': _obj_field(raw_c, 'cert_no') or '—',
            'valid_until': vu_str,
            'is_long_term': is_long,
            'has_attachment': bool(_obj_field(raw_c, 'attachment_path') or _obj_field(raw_c, 'has_attachment')),
            'attachment_name': _obj_field(raw_c, 'attachment_name') or '—',
        })

    sec_appendix = BlueprintSection(
        section_id='sec_appendix',
        title='第六部分 人工待填报清单总览与企业台账备查附录',
        slot_type='cert_ledger_appendix',
        fill_mode='cert_ledger',
        tender_requirements=[],
        content_blocks=[
            {'kind': 'manual_summary', 'count': len(manual_checklist), 'items': list(manual_checklist)},
            *ledger_refs,
        ],
        needs_manual_items=[],
        source_refs=[{'ledger_count': len(ledger_refs)}],
    )

    _emit_progress(90, '阶段 4/4：正在封装蓝图结构与按 Skill 版式渲染 Word')
    sections = [sec_cover, sec_qual, sec_price, sec_deviation, sec_scoring, sec_appendix]
    skill_biz_line = str(_obj_field(skill, 'biz_line', 'general') or 'general').strip() or 'general'
    stats = {
        'section_count': len(sections),
        'required_cert_count': len(matched_certs),
        'deviation_row_count': len(s4_blocks),
        'scoring_topic_count': len(topics),
        'ai_chapter_count': len(planned_chapters),
        'rag_matched_topics': matched_rag_count,
        'indicator_matched_topics': matched_indicator_count,
        'manual_item_count': len(manual_checklist),
        'scoring_fill_mode': sec_scoring_mode,
        'ai_status': ai_status,
        'skill_name': skill_name,
        'biz_line': skill_biz_line,
        'use_indicator_library': bool(
            matched_indicator_count > 0
            or (provider is not None and (hasattr(provider, 'target_lines') or hasattr(provider, '_providers')))
        ),
    }
    return BidBlueprint(
        project_name=project_name,
        project_code=project_code,
        buyer_name=buyer_name,
        open_date=open_date,
        open_date_source=open_date_source,
        price_cap=formatted_cap,
        package_no=package_no,
        compose_mode=compose_mode,
        tender_filename=t_path.name,
        skill_id=skill_id,
        skill_name=skill_name,
        skill_layout=skill_layout,
        ai_status=ai_status,
        planned_chapters=planned_chapters,
        sections=sections,
        manual_checklist=manual_checklist,
        cert_match_summary=cert_summary,
        stats=stats,
    )


def _hex_to_rgb(hex_str: str, fallback: RGBColor = _COLOR_NAVY) -> RGBColor:
    cleaned = (hex_str or '').strip().lstrip('#')
    if len(cleaned) != 6:
        return fallback
    try:
        return RGBColor(int(cleaned[0:2], 16), int(cleaned[2:4], 16), int(cleaned[4:6], 16))
    except ValueError:
        return fallback


def _set_run_font(
    run,
    *,
    name_zh: str = '仿宋',
    size_pt: float = 11.5,
    bold: bool = False,
    color: Optional[RGBColor] = None,
) -> None:
    run.font.name = name_zh
    rPr = run._r.get_or_add_rPr()
    rFonts = rPr.find(qn('w:rFonts'))
    if rFonts is None:
        rFonts = OxmlElement('w:rFonts')
        rPr.append(rFonts)
    rFonts.set(qn('w:eastAsia'), name_zh)
    run.font.size = Pt(size_pt)
    run.bold = bold
    if color is not None:
        run.font.color.rgb = color


def _apply_para_spacing(p, *, line_spacing: float = 1.35, space_before: int = 2, space_after: int = 5) -> None:
    pf = p.paragraph_format
    pf.line_spacing = line_spacing
    pf.space_before = Pt(space_before)
    pf.space_after = Pt(space_after)


def _shade_cell(cell, fill_hex: str) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    shd = tcPr.find(qn('w:shd'))
    if shd is not None:
        tcPr.remove(shd)
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), fill_hex.lstrip('#'))
    tcPr.append(shd)


def _style_table_cell(cell, *, pad_v_dxa: int = 90, pad_h_dxa: int = 120, width_in: Optional[float] = None) -> None:
    cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
    if width_in is not None:
        cell.width = Inches(width_in)
    tcPr = cell._tc.get_or_add_tcPr()
    tcMar = tcPr.find(qn('w:tcMar'))
    if tcMar is None:
        tcMar = OxmlElement('w:tcMar')
        tcPr.append(tcMar)
    for side, val in (('top', pad_v_dxa), ('bottom', pad_v_dxa), ('left', pad_h_dxa), ('right', pad_h_dxa)):
        el = tcMar.find(qn(f'w:{side}'))
        if el is None:
            el = OxmlElement(f'w:{side}')
            tcMar.append(el)
        el.set(qn('w:w'), str(val))
        el.set(qn('w:type'), 'dxa')


def _insert_toc_field(paragraph) -> None:
    run = paragraph.add_run()
    fldChar1 = OxmlElement('w:fldChar')
    fldChar1.set(qn('w:fldCharType'), 'begin')
    instrText = OxmlElement('w:instrText')
    instrText.set(qn('xml:space'), 'preserve')
    instrText.text = 'TOC \\o "1-3" \\h \\z \\u'
    fldChar2 = OxmlElement('w:fldChar')
    fldChar2.set(qn('w:fldCharType'), 'separate')
    fldChar3 = OxmlElement('w:fldChar')
    fldChar3.set(qn('w:fldCharType'), 'end')
    r_el = run._r
    r_el.append(fldChar1)
    r_el.append(instrText)
    r_el.append(fldChar2)
    r_el.append(fldChar3)


def _add_styled_paragraph(
    doc: Document,
    text: str,
    *,
    font_zh: str = '仿宋',
    bold: bool = False,
    color: Optional[RGBColor] = None,
    highlight_manual: bool = False,
    font_size: float = 11.0,
    line_spacing: float = 1.35,
    first_line_indent_pt: float = 0.0,
) -> None:
    p = doc.add_paragraph()
    _apply_para_spacing(p, line_spacing=line_spacing)
    if first_line_indent_pt > 0:
        p.paragraph_format.first_line_indent = Pt(first_line_indent_pt)
    run = p.add_run(text)
    _set_run_font(run, name_zh=font_zh, size_pt=font_size, bold=bold, color=color)
    if highlight_manual:
        run.font.highlight_color = WD_COLOR_INDEX.YELLOW


def _add_doc_heading(
    doc: Document,
    text: str,
    *,
    level: int,
    heading_font_zh: str,
    accent_rgb: RGBColor,
) -> None:
    h = doc.add_heading('', level=level)
    _apply_para_spacing(h, line_spacing=1.2, space_before=10 if level == 1 else 6, space_after=4)
    r = h.add_run(text)
    sz = 15.0 if level == 1 else (12.5 if level == 2 else 11.5)
    _set_run_font(r, name_zh=heading_font_zh, size_pt=sz, bold=True, color=accent_rgb)


def export_composed_bid_docx(
    blueprint: BidBlueprint | dict[str, Any],
    output_path: str | Path,
) -> Path:
    """按 Skill `skill_layout` 规范渲染独立封面 + 原生目录/标题 + 洁净标书正文 `.docx`。"""
    bp = blueprint.to_dict() if isinstance(blueprint, BidBlueprint) else dict(blueprint)
    project_name = str(bp.get('project_name') or '拟投标项目')
    project_code = str(bp.get('project_code') or '—')
    buyer_name = str(bp.get('buyer_name') or '—')
    open_date = str(bp.get('open_date') or '[待人工核对]')
    layout = normalize_layout_spec(bp.get('skill_layout') or {})
    # 若来自无 Skill 的旧调用且明确传了 append_checklist_at_end，尊重蓝图自身设置
    if bp.get('skill_id') is None and isinstance(bp.get('skill_layout'), dict) and 'append_checklist_at_end' in bp['skill_layout']:
        layout['append_checklist_at_end'] = bool(bp['skill_layout']['append_checklist_at_end'])

    font_zh = str(layout['font_zh'])
    heading_font = str(layout['heading_font_zh'])
    font_size = float(layout['font_size_pt'])
    line_spacing = float(layout['line_spacing'])
    accent_rgb = _hex_to_rgb(str(layout['accent_color_hex']))
    hdr_bg = str(layout['table_header_bg'])
    cover_style = str(layout['cover_style'])

    doc = Document()
    normal_style = doc.styles['Normal']
    normal_style.font.name = font_zh
    normal_style.font.size = Pt(font_size)

    # 页边距与不同首页设置（封面不显示页眉页脚）
    for sec in doc.sections:
        sec.top_margin = Inches(0.95)
        sec.bottom_margin = Inches(0.9)
        sec.left_margin = Inches(1.0)
        sec.right_margin = Inches(1.0)
        sec.different_first_page_header_footer = True

        hdr = sec.header.paragraphs[0] if sec.header.paragraphs else sec.header.add_paragraph()
        hdr.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        r_hdr = hdr.add_run(f"{project_name} · 投标文件（初稿）")
        _set_run_font(r_hdr, name_zh=font_zh, size_pt=8.5, color=_COLOR_MUTED)

        ftr = sec.footer.paragraphs[0] if sec.footer.paragraphs else sec.footer.add_paragraph()
        ftr.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r_ftr = ftr.add_run("请结合招标文件终核报价、业绩证明及签章后正式封标提交")
        _set_run_font(r_ftr, name_zh=font_zh, size_pt=8.5, color=_COLOR_MUTED)

    # ── 1. 独立居中封面页（按 cover_style 动态控制视觉）─────────────────────
    top_spacer = doc.add_paragraph()
    _apply_para_spacing(top_spacer, space_before=18, space_after=12)
    top_spacer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    badge_txt = {
        'tech_modern': 'TECHNICAL & COMMERCIAL PROPOSAL',
        'navy_business': '正 式 投 标 文 件（商 务 及 技 术 稿）',
        'gov_clean': '政 府 采 购 投 标 文 件（初 稿）',
    }.get(cover_style, '投 标 文 件（初 稿）')
    r_badge = top_spacer.add_run(badge_txt)
    _set_run_font(r_badge, name_zh=heading_font, size_pt=11, bold=True, color=accent_rgb)

    p_proj = doc.add_paragraph()
    p_proj.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _apply_para_spacing(p_proj, line_spacing=1.35, space_before=16, space_after=8)
    r_proj = p_proj.add_run(project_name)
    _set_run_font(r_proj, name_zh=heading_font, size_pt=22, bold=True, color=accent_rgb)

    p_sub = doc.add_paragraph()
    p_sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _apply_para_spacing(p_sub, space_before=4, space_after=24)
    r_sub_title = p_sub.add_run('投 标 文 件')
    _set_run_font(r_sub_title, name_zh=heading_font, size_pt=26, bold=True, color=_COLOR_NAVY)

    cover_tbl = doc.add_table(rows=5, cols=2)
    cover_tbl.style = 'Table Grid'
    cover_tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    cover_pairs = (
        ('项目编号', project_code),
        ('采购人 / 招标人', buyer_name),
        ('开标日期', open_date),
        ('投标人名称（盖章）', '[待人工填报：投标单位法定全称（加盖公章）]'),
        ('法定代表人或授权代表', '[待人工填报：签字或盖章]'),
    )
    for idx, (k, v) in enumerate(cover_pairs):
        c0, c1 = cover_tbl.rows[idx].cells[0], cover_tbl.rows[idx].cells[1]
        _style_table_cell(c0, width_in=2.1)
        _style_table_cell(c1, width_in=4.2)
        if cover_style != 'gov_clean':
            _shade_cell(c0, hdr_bg)
        r0 = c0.paragraphs[0].add_run(k)
        _set_run_font(r0, name_zh=heading_font, size_pt=10.5, bold=True, color=_COLOR_NAVY)
        r1 = c1.paragraphs[0].add_run(v)
        is_ph = v.startswith('[待人工')
        _set_run_font(r1, name_zh=font_zh, size_pt=10.5, bold=is_ph, color=_COLOR_RED if is_ph else _COLOR_NAVY)

    p_foot = doc.add_paragraph()
    p_foot.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _apply_para_spacing(p_foot, space_before=30, space_after=4)
    r_pf = p_foot.add_run(
        f"编写风格规范：{bp.get('skill_name', '标准')} ｜ 依据：{bp.get('tender_filename', '')} ｜ {datetime.datetime.now().strftime('%Y-%m-%d')}"
    )
    _set_run_font(r_pf, name_zh=font_zh, size_pt=9.5, color=_COLOR_MUTED)

    doc.add_page_break()

    # ── 2. 目录索引页（include_toc=True）────────────────────────────────────
    if layout.get('include_toc', True):
        _add_doc_heading(doc, '目  录', level=1, heading_font_zh=heading_font, accent_rgb=accent_rgb)
        p_toc_tip = doc.add_paragraph()
        _insert_toc_field(p_toc_tip)
        _add_styled_paragraph(
            doc,
            '（在 Word 中右键“更新域”即可刷新真实页码；以下为本章结构总览）',
            font_zh=font_zh,
            color=_COLOR_MUTED,
            font_size=9.0,
        )
        for sec_data in bp.get('sections') or []:
            if sec_data.get('slot_type') == 'cert_ledger_appendix' and not layout.get('append_checklist_at_end'):
                continue
            _add_styled_paragraph(
                doc,
                f"● {sec_data.get('title')}",
                font_zh=heading_font,
                bold=True,
                color=_COLOR_NAVY,
                font_size=10.5,
            )
            if sec_data.get('slot_type') == 'scoring_response_outline':
                for b in sec_data.get('content_blocks') or []:
                    if b.get('kind') == 'ai_chapter':
                        _add_styled_paragraph(
                            doc,
                            f"    - {b.get('chapter_no')} {b.get('title')}",
                            font_zh=font_zh,
                            color=_COLOR_MUTED,
                            font_size=9.5,
                        )
        doc.add_page_break()

    # ── 3. 主体章节内容渲染 ─────────────────────────────────────────────────
    for sec_data in bp.get('sections') or []:
        slot_type = sec_data.get('slot_type')
        if slot_type == 'cert_ledger_appendix' and not layout.get('append_checklist_at_end'):
            continue

        _add_doc_heading(
            doc,
            str(sec_data.get('title') or ''),
            level=1,
            heading_font_zh=heading_font,
            accent_rgb=accent_rgb,
        )
        blocks = sec_data.get('content_blocks') or []

        if slot_type in ('cover_and_letter', 'price_placeholder'):
            kv_rows = [b for b in blocks if b.get('kind') in ('meta', 'placeholder', 'price_row')]
            if kv_rows:
                tbl = doc.add_table(rows=1, cols=2)
                tbl.style = 'Table Grid'
                tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
                headers_kv = ('响应项目 / 条款', '拟响应内容与核对占位')
                for i, h_txt in enumerate(headers_kv):
                    c = tbl.rows[0].cells[i]
                    _style_table_cell(c, width_in=2.1 if i == 0 else 4.2)
                    _shade_cell(c, hdr_bg)
                    r = c.paragraphs[0].add_run(h_txt)
                    _set_run_font(r, name_zh=heading_font, size_pt=10, bold=True, color=_COLOR_NAVY)
                for item in kv_rows:
                    rc = tbl.add_row().cells
                    _style_table_cell(rc[0], width_in=2.1)
                    _style_table_cell(rc[1], width_in=4.2)
                    r0 = rc[0].paragraphs[0].add_run(str(item.get('label') or ''))
                    _set_run_font(r0, name_zh=font_zh, size_pt=10, bold=True, color=_COLOR_NAVY)
                    r1 = rc[1].paragraphs[0].add_run(str(item.get('value') or ''))
                    is_hl = bool(item.get('highlight'))
                    _set_run_font(r1, name_zh=font_zh, size_pt=10, bold=is_hl, color=_COLOR_RED if is_hl else _COLOR_NAVY)
                    if is_hl:
                        r1.font.highlight_color = WD_COLOR_INDEX.YELLOW
            for b in blocks:
                if b.get('kind') == 'paragraph':
                    _add_styled_paragraph(
                        doc,
                        str(b.get('text') or ''),
                        font_zh=font_zh,
                        color=_COLOR_AMBER if b.get('highlight') else _COLOR_NAVY,
                        highlight_manual=bool(b.get('highlight')),
                        font_size=font_size,
                        line_spacing=line_spacing,
                        first_line_indent_pt=22.0,
                    )

        elif slot_type == 'qualification_checklist':
            cert_rows = [b for b in blocks if b.get('kind') == 'cert_row']
            if cert_rows:
                _add_styled_paragraph(
                    doc,
                    '以下为本项目所需资格与资质证明总览索引；每项证明材料的独立附页（原件扫描图或标准粘贴装订框）紧随索引表按序号逐一单列，确保满足 A4 全幅清晰审阅要求。',
                    font_zh=font_zh,
                    color=_COLOR_MUTED,
                    font_size=9.5,
                )
                _add_doc_heading(
                    doc,
                    '2.0 资格与资质证明文件总览索引表',
                    level=2,
                    heading_font_zh=heading_font,
                    accent_rgb=accent_rgb,
                )
                tbl = doc.add_table(rows=1, cols=6)
                tbl.style = 'Table Grid'
                tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
                col_w = (0.45, 1.4, 0.95, 1.65, 0.95, 0.9)
                headers = ('序号', '招标所需证照/资质', '要求角色', '台账核对结果', '有效期/证号', '附页 / 原件')
                for i, h in enumerate(headers):
                    c = tbl.rows[0].cells[i]
                    _style_table_cell(c, width_in=col_w[i])
                    _shade_cell(c, hdr_bg)
                    r = c.paragraphs[0].add_run(h)
                    _set_run_font(r, name_zh=heading_font, size_pt=9.0, bold=True, color=_COLOR_NAVY)

                resolved_imgs: dict[int, Optional[Path]] = {}
                for idx, b in enumerate(cert_rows, start=1):
                    img_p = _resolve_embeddable_image_path(b.get('attachment_path'))
                    resolved_imgs[idx] = img_p
                    role_txt = str(
                        b.get('role_label')
                        or _format_cert_roles_label(b.get('requirement_roles'), str(b.get('requirement_level') or 'bonus'))
                    )
                    rc = tbl.add_row().cells
                    for i in range(6):
                        _style_table_cell(rc[i], width_in=col_w[i])
                    _set_run_font(rc[0].paragraphs[0].add_run(str(idx)), name_zh=font_zh, size_pt=9.0)
                    _set_run_font(rc[1].paragraphs[0].add_run(str(b.get('cert_name') or '')), name_zh=font_zh, size_pt=9.0)
                    _set_run_font(rc[2].paragraphs[0].add_run(role_txt), name_zh=font_zh, size_pt=9.0)
                    st = str(b.get('ledger_status') or '')
                    r_st = rc[3].paragraphs[0].add_run(str(b.get('display_status') or ''))
                    col_st = _COLOR_GREEN if st == 'valid' else (_COLOR_RED if st in ('expired', 'not_in_ledger') else _COLOR_AMBER)
                    _set_run_font(r_st, name_zh=font_zh, size_pt=8.8, bold=(st != 'valid'), color=col_st)
                    _set_run_font(rc[4].paragraphs[0].add_run(f"{b.get('valid_until') or '—'}\n{b.get('cert_no') or ''}".strip()), name_zh=font_zh, size_pt=8.8)
                    att_flag = '已嵌图' if img_p is not None else ('PDF原件' if b.get('has_attachment') else '待粘贴')
                    _set_run_font(rc[5].paragraphs[0].add_run(f"见 2.{idx}\n({att_flag})"), name_zh=font_zh, size_pt=8.8)

                for idx, b in enumerate(cert_rows, start=1):
                    c_title = str(b.get('cert_name') or '证明文件')
                    role_txt = str(
                        b.get('role_label')
                        or _format_cert_roles_label(b.get('requirement_roles'), str(b.get('requirement_level') or 'bonus'))
                    )
                    _add_doc_heading(
                        doc,
                        f"2.{idx} {c_title}",
                        level=2,
                        heading_font_zh=heading_font,
                        accent_rgb=accent_rgb,
                    )
                    _add_styled_paragraph(
                        doc,
                        f"要求角色：{role_txt} ｜ 台账核对：{b.get('display_status') or '待核对'}",
                        font_zh=font_zh,
                        color=_COLOR_MUTED,
                        font_size=9.5,
                    )
                    img_p = resolved_imgs.get(idx)
                    inserted_pic = False
                    if img_p is not None:
                        try:
                            p_pic = doc.add_paragraph()
                            p_pic.alignment = WD_ALIGN_PARAGRAPH.CENTER
                            _apply_para_spacing(p_pic, space_before=6, space_after=4)
                            p_pic.add_run().add_picture(str(img_p), width=Inches(5.4))
                            _add_styled_paragraph(
                                doc,
                                f"【台账自动关联原件扫描图：{b.get('attachment_name') or img_p.name}】",
                                font_zh=font_zh,
                                color=_COLOR_GREEN,
                                font_size=9.0,
                            )
                            inserted_pic = True
                        except Exception:
                            inserted_pic = False

                    if not inserted_pic:
                        box_tbl = doc.add_table(rows=1, cols=1)
                        box_tbl.style = 'Table Grid'
                        box_tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
                        cell = box_tbl.rows[0].cells[0]
                        if b.get('has_attachment'):
                            _style_table_cell(cell, pad_v_dxa=260, pad_h_dxa=180, width_in=6.2)
                            _shade_cell(cell, 'F8FAFC')
                            p_box = cell.paragraphs[0]
                            p_box.alignment = WD_ALIGN_PARAGRAPH.CENTER
                            _set_run_font(
                                p_box.add_run(
                                    f"【已关联企业台账原件：{b.get('attachment_name') or '已上传附件'}】\n"
                                    f"本证照非图片格式（如 PDF 原件），请打印导出后按 2.{idx} 位置随正本加盖公章装订。"
                                ),
                                name_zh=font_zh,
                                size_pt=9.5,
                                color=_COLOR_NAVY,
                            )
                        else:
                            _style_table_cell(cell, pad_v_dxa=380, pad_h_dxa=180, width_in=6.2)
                            p_box = cell.paragraphs[0]
                            p_box.alignment = WD_ALIGN_PARAGRAPH.CENTER
                            _set_run_font(
                                p_box.add_run(
                                    f"【{c_title} 原件扫描件 / 复印件粘贴区（附页 2.{idx}）】\n\n"
                                    f"[待人工补充：请在此处整页粘贴有效期内清晰彩色扫描件或复印件并加盖投标人公章]"
                                ),
                                name_zh=font_zh,
                                size_pt=10.0,
                                bold=True,
                                color=_COLOR_AMBER,
                            )
            else:
                _add_styled_paragraph(doc, '[待人工补充：未提取到明确资格证照，请核对招标文件原文]', font_zh=font_zh, highlight_manual=True)

        elif slot_type == 'deviation_table':
            dev_rows = [b for b in blocks if b.get('kind') == 'deviation_row']
            if dev_rows:
                tbl = doc.add_table(rows=1, cols=5)
                tbl.style = 'Table Grid'
                tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
                col_w = (0.55, 0.95, 2.2, 1.75, 0.85)
                for i, h in enumerate(('序号', '条款号', '招标实质性要求', '我方响应（初稿）', '偏离状态')):
                    c = tbl.rows[0].cells[i]
                    _style_table_cell(c, width_in=col_w[i])
                    _shade_cell(c, hdr_bg)
                    _set_run_font(c.paragraphs[0].add_run(h), name_zh=heading_font, size_pt=9.5, bold=True, color=_COLOR_NAVY)
                for b in dev_rows:
                    rc = tbl.add_row().cells
                    for i in range(5):
                        _style_table_cell(rc[i], width_in=col_w[i])
                    _set_run_font(rc[0].paragraphs[0].add_run(str(b.get('seq') or '')), name_zh=font_zh, size_pt=9.0)
                    _set_run_font(rc[1].paragraphs[0].add_run(str(b.get('clause_no') or '')), name_zh=font_zh, size_pt=9.0)
                    _set_run_font(rc[2].paragraphs[0].add_run(str(b.get('tender_requirement') or '')), name_zh=font_zh, size_pt=9.0)
                    r_resp = rc[3].paragraphs[0].add_run(str(b.get('bid_response') or ''))
                    is_hl = bool(b.get('highlight')) and '[AI草稿' not in str(b.get('bid_response') or '')
                    _set_run_font(r_resp, name_zh=font_zh, size_pt=9.0, bold=is_hl, color=_COLOR_AMBER if is_hl else _COLOR_NAVY)
                    if is_hl:
                        r_resp.font.highlight_color = WD_COLOR_INDEX.YELLOW
                    _set_run_font(rc[4].paragraphs[0].add_run(str(b.get('deviation') or '')), name_zh=font_zh, size_pt=9.0)
            else:
                _add_styled_paragraph(doc, '[待人工补充：请结合第四章技术与商务要求核填偏离表]', font_zh=font_zh, highlight_manual=True)

        elif slot_type == 'scoring_response_outline':
            for b in blocks:
                if b.get('kind') == 'ai_chapter':
                    _add_doc_heading(
                        doc,
                        f"{b.get('chapter_no', '')} {b.get('title', '')}".strip(),
                        level=2,
                        heading_font_zh=heading_font,
                        accent_rgb=accent_rgb,
                    )
                    covered = b.get('covered_topics') or []
                    if covered:
                        _add_styled_paragraph(
                            doc,
                            f"【评分点映射】{'；'.join(str(x) for x in covered[:5])}",
                            font_zh=font_zh,
                            color=_COLOR_MUTED,
                            font_size=9.0,
                        )
                    for para_txt in _strip_duplicate_leading_title(
                        list(b.get('paragraphs') or []),
                        str(b.get('chapter_no') or ''),
                        str(b.get('title') or ''),
                    ):
                        _add_styled_paragraph(
                            doc,
                            str(para_txt),
                            font_zh=font_zh,
                            font_size=font_size,
                            line_spacing=line_spacing,
                            first_line_indent_pt=22.0,
                            color=_COLOR_NAVY,
                        )
                    ai_tbl = b.get('table')
                    if isinstance(ai_tbl, dict) and ai_tbl.get('headers') and ai_tbl.get('rows'):
                        if ai_tbl.get('title'):
                            _add_styled_paragraph(doc, f"表：{ai_tbl['title']}", font_zh=heading_font, bold=True, font_size=10.0, color=accent_rgb)
                        hdrs = list(ai_tbl['headers'])
                        t_doc = doc.add_table(rows=1, cols=len(hdrs))
                        t_doc.style = 'Table Grid'
                        t_doc.alignment = WD_TABLE_ALIGNMENT.CENTER
                        col_unit = round(6.2 / max(1, len(hdrs)), 2)
                        for idx, ht in enumerate(hdrs):
                            c = t_doc.rows[0].cells[idx]
                            _style_table_cell(c, width_in=col_unit)
                            _shade_cell(c, hdr_bg)
                            _set_run_font(c.paragraphs[0].add_run(str(ht)), name_zh=heading_font, size_pt=9.5, bold=True, color=_COLOR_NAVY)
                        for r_vals in ai_tbl['rows']:
                            rc = t_doc.add_row().cells
                            for idx, val in enumerate(r_vals[:len(hdrs)]):
                                _style_table_cell(rc[idx], width_in=col_unit)
                                _set_run_font(rc[idx].paragraphs[0].add_run(str(val)), name_zh=font_zh, size_pt=9.5, color=_COLOR_NAVY)
                    for sn in b.get('reference_snippets') or []:
                        tbl = doc.add_table(rows=1, cols=1)
                        tbl.style = 'Table Grid'
                        cell = tbl.rows[0].cells[0]
                        _style_table_cell(cell, width_in=6.2)
                        _shade_cell(cell, 'F8FAFC')
                        _set_run_font(cell.paragraphs[0].add_run(str(sn.get('notice') or '') + '\n'), name_zh=font_zh, size_pt=9.0, bold=True, color=_COLOR_AMBER)
                        _set_run_font(cell.add_paragraph().add_run(str(sn.get('snippet') or '')), name_zh=font_zh, size_pt=9.0, color=_COLOR_MUTED)
                    continue

                if b.get('kind') != 'scoring_topic':
                    continue
                _add_doc_heading(
                    doc,
                    f"{b.get('title')}（{b.get('score_points')}）",
                    level=2,
                    heading_font_zh=heading_font,
                    accent_rgb=accent_rgb,
                )
                if b.get('tender_quote'):
                    _add_styled_paragraph(
                        doc,
                        f"招标依据：{b.get('tender_quote')}",
                        font_zh=font_zh,
                        color=_COLOR_MUTED,
                        font_size=9.5,
                    )
                for line in b.get('sub_outline') or []:
                    _add_styled_paragraph(
                        doc,
                        f"  {line}",
                        font_zh=font_zh,
                        highlight_manual='[待人工补充' in line,
                        font_size=font_size,
                        line_spacing=line_spacing,
                    )
                for sn in b.get('reference_snippets') or []:
                    tbl = doc.add_table(rows=1, cols=1)
                    tbl.style = 'Table Grid'
                    cell = tbl.rows[0].cells[0]
                    _style_table_cell(cell, width_in=6.2)
                    _shade_cell(cell, 'F8FAFC')
                    p_notice = cell.paragraphs[0]
                    _set_run_font(p_notice.add_run(str(sn.get('notice') or '') + '\n'), name_zh=font_zh, size_pt=9.0, bold=True, color=_COLOR_AMBER)
                    p_snip = cell.add_paragraph()
                    _set_run_font(p_snip.add_run(str(sn.get('snippet') or '')), name_zh=font_zh, size_pt=9.0, color=_COLOR_MUTED)

        elif slot_type == 'cert_ledger_appendix':
            for b in blocks:
                if b.get('kind') == 'manual_summary':
                    items = b.get('items') or []
                    _add_styled_paragraph(doc, f"共 {len(items)} 项待人工补充/核对确认：", font_zh=heading_font, bold=True, color=_COLOR_RED if items else _COLOR_GREEN)
                    for idx, mi in enumerate(items, start=1):
                        _add_styled_paragraph(
                            doc,
                            f"{idx}. [{mi.get('field')}] {mi.get('reason')}（建议：{mi.get('hint')}）",
                            font_zh=font_zh,
                            highlight_manual=True,
                            font_size=9.0,
                        )
            ledger_rows = [b for b in blocks if b.get('kind') == 'ledger_appendix_row']
            if ledger_rows:
                tbl = doc.add_table(rows=1, cols=4)
                tbl.style = 'Table Grid'
                tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
                for i, h in enumerate(('企业台账证照', '证号', '效期类型', '原件附件')):
                    c = tbl.rows[0].cells[i]
                    _style_table_cell(c, width_in=1.55)
                    _shade_cell(c, hdr_bg)
                    _set_run_font(c.paragraphs[0].add_run(h), name_zh=heading_font, size_pt=9.5, bold=True)
                for lr in ledger_rows:
                    rc = tbl.add_row().cells
                    _set_run_font(rc[0].paragraphs[0].add_run(str(lr.get('cert_name') or '')), name_zh=font_zh, size_pt=9.0)
                    _set_run_font(rc[1].paragraphs[0].add_run(str(lr.get('cert_no') or '')), name_zh=font_zh, size_pt=9.0)
                    _set_run_font(rc[2].paragraphs[0].add_run(str(lr.get('valid_until') or '')), name_zh=font_zh, size_pt=9.0)
                    _set_run_font(rc[3].paragraphs[0].add_run(str(lr.get('attachment_name') or '')), name_zh=font_zh, size_pt=9.0)

    out_p = Path(output_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_p))
    return out_p
