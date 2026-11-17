# -*- coding: utf-8 -*-
"""E37 / F8 · 标书编制输出与蓝图装配引擎（V1 正向骨架 + V2 方案库插槽）。

模块职责（对应 E37 任务卡 §3.1）：
1. 统一章节蓝图 IR（BidBlueprint / BlueprintSection）：
   - V1 确定性正向装配（0 Token 零幻觉）：基于招标文件 F1 抽取结果（项目/开标日/限价/★▲/
     资格证照清单/采购需求/评分细则表）与租户企业证照台账（含 E35 is_long_term 长期有效证照）
     + 可选复用体检任务 F4 结论，生成结构完整、证照与偏离表对齐、防串名的投标文件初稿；
   - 报价铁律：总报价/单价栏强制填充 `[待人工填报金额...]` 占位符，严禁 AI 编造任何报价数字；
     限价核算全程使用 Decimal（ROUND_HALF_UP），禁用 float；
2. V2 可插拔方案库接口（KnowledgeSnippetProvider / LocalKnowledgeSnippetProvider）：
   - `compose_mode='knowledge_draft_v2'` 且传入 `knowledge_paths` 或自定义 Provider 时，
     按评分细则小节通过 `core/hybrid_retriever.py` 做局部检索对齐并过 `verify_quote` 溯源，
     附注来源文件名与 `[参考片段·待人工确认改写]` 标灰提示；
   - 未传参考资料时优雅降级回 `outline_stub` 大纲，绝不报错、绝不混入无关文本；
3. Word 渲染导出（export_composed_bid_docx）：
   - 输出带目录层级、自定义页眉/页脚防串名、Table Grid 规范边框、待填项醒目黄色高亮的
     《投标文件初稿骨架.docx》。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import re
from typing import Any, Literal, Optional, Protocol, runtime_checkable

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_COLOR_INDEX
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

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

    # 若命中句较短（如小节标题），仅吸收同一段内直到空行/下个标题之前的后续行
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
                # 严格相关性门禁：防无关段落因单字偶合混入技术章节
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
        # 按文本去重后取 Top-k
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


def _collect_scoring_topics(checklist: dict[str, Any], text: str) -> list[dict[str, Any]]:
    """从评分细则表、第四章采购需求与评分命中段落确定性提取技术/商务方案主题大纲。"""
    topics: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    def _add_topic(title: str, detail: str, score: str, source: str) -> None:
        clean_title = re.sub(r'\s+', ' ', (title or '').strip())
        clean_detail = re.sub(r'\s+', ' ', (detail or '').strip())
        if not clean_title and not clean_detail:
            return
        if not clean_title:
            clean_title = clean_detail[:36]
        if any(k in clean_title for k in ('合计', '总分', '总计', '小计')) and not clean_detail:
            return
        dedup_k = normalize_zh(f"{clean_title[:24]}_{clean_detail[:24]}")
        if not dedup_k or dedup_k in seen_keys:
            return
        seen_keys.add(dedup_k)
        topics.append({
            'topic_id': f"topic_{len(topics) + 1}",
            'title': clean_title,
            'score_points': score or '以招标评分标准为准',
            'tender_quote': clean_detail[:240],
            'source': source,
        })

    for st in checklist.get('scoring_tables') or []:
        factor = str(st.get('factor') or '').strip()
        detail = str(st.get('detail') or '').strip()
        score = str(st.get('score') or '').strip()
        _add_topic(factor, detail, score, 'scoring_table')

    for ch4 in extract_chapter4_procurement_requirements(text):
        cno = str(ch4.get('clause_no') or '').strip()
        ctxt = str(ch4.get('text') or '').strip()
        first_line = ctxt.split('\n', 1)[0].strip()
        _add_topic(cno or first_line[:30], ctxt, '实质性需求点', 'chapter4_procurement')
        if len(topics) >= 16:
            break

    if len(topics) < 2:
        for sh in checklist.get('scoring_hits') or []:
            excerpt = str(sh.get('excerpt') or '')
            for m in _EVAL_SUBITEM_RE.finditer(excerpt):
                line = m.group(1).strip()
                if len(line) < 8:
                    continue
                score_m = re.search(r'([（(]\s*\d+(?:\.\d+)?\s*分\s*[）)])', line)
                _add_topic(
                    line[:40],
                    line,
                    score_m.group(1) if score_m else '评分要点',
                    'scoring_prose',
                )
                if len(topics) >= 10:
                    break

    if not topics:
        for fallback_title, fallback_desc in (
            ('项目理解与总体技术架构方案', '针对采购人核心业务需求与系统运行环境，明确总体架构与技术路线。'),
            ('项目实施组织、工期进度与验收方案', '明确实施阶段划分、人员配置、里程碑管控与验收交付标准。'),
            ('售后服务、应急响应与质保运维保障方案', '明确质保期限、故障响应时间、本地化运维与培训移交计划。'),
        ):
            _add_topic(fallback_title, fallback_desc, '待对照评分细则核对分值', 'default_outline')

    return topics


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
        # 确定性数字保真校验：禁止 LLM 凭空捏造参考片段和招标文均未出现的多位年份/金额数字
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
) -> BidBlueprint:
    """E37 §3.1：生成统一投标章节蓝图 IR（V1 确定性骨架+台账+偏离表 / V2 方案库槽位）。"""
    if compose_mode not in ('skeleton_v1', 'knowledge_draft_v2'):
        raise ValueError(f"不支持的编制模式 compose_mode={compose_mode!r}")

    t_path = Path(tender_path)
    if not t_path.is_file():
        raise FileNotFoundError(f"招标文件不存在：{tender_path}")

    doc = parse_document(str(t_path))
    full_text = doc.text or ''
    page_ctx = build_unified_page_context(str(t_path))

    if checklist is None:
        checklist = build_review_checklist(str(t_path))

    # 1. 基础项目元信息提取（绝不串用旧模板名）
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
    if not open_date:
        s1_manual.append(_add_manual('sec_cover', '开标日期', '未提取到明确开标日期', '请对照招标公告人工补填'))

    sec_cover = BlueprintSection(
        section_id='sec_cover',
        title='第一部分 投标文件封面与投标函骨架',
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
            {'kind': 'paragraph', 'text': '致采购人及招标代理机构：我方已仔细研究上述项目招标文件及其全部补充通知，愿按本投标文件所列实质性条款及正式核报价格参与投标。投标有效期：[待人工填报，须不少于招标文件规定天数]。', 'highlight': True},
        ],
        needs_manual_items=s1_manual,
        source_refs=[{'file': t_path.name, 'open_date_source': open_date_source}],
    )

    # ── Section 2: 资格审查与企业证照核对清单 ────────────────────────────────
    required_certs = checklist.get('required_certificates') or []
    if not required_certs and full_text:
        required_certs = extract_required_certificates(full_text, page_ctx=page_ctx)

    matched_certs, cert_summary = match_required_certificates_with_ledger(
        required_certs,
        ledger_certs or [],
        reference_date=open_date,
    )

    s2_blocks: list[dict[str, Any]] = []
    s2_manual: list[dict[str, Any]] = []
    for mc in matched_certs:
        c_name = str(mc.get('cert_name') or '')
        status = str(mc.get('ledger_status') or 'not_in_ledger')
        best = mc.get('matched_cert') or {}
        if status == 'valid':
            validity = '长期有效' if best.get('is_long_term') else f"有效期至 {best.get('valid_until')}"
            cert_no = best.get('cert_no') or '见原件'
            att_tip = f"已关联台账原件《{best.get('attachment_name')}》（可从规则库下载插入）" if best.get('has_attachment') else '[待人工补充：台账未附扫描件，请线下插入原件]'
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
            'ledger_status': status,
            'cert_no': best.get('cert_no'),
            'valid_until': '长期有效' if best.get('is_long_term') else best.get('valid_until'),
            'is_long_term': bool(best.get('is_long_term', False)),
            'attachment_name': best.get('attachment_name'),
            'has_attachment': bool(best.get('has_attachment', False)),
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
            s2_blocks.append({
                'kind': 'cert_row',
                'cert_name': c_name,
                'requirement_level': 'ledger_reference',
                'ledger_status': 'valid' if is_long else 'in_ledger',
                'cert_no': _obj_field(raw_c, 'cert_no'),
                'valid_until': vu_str,
                'is_long_term': is_long,
                'attachment_name': _obj_field(raw_c, 'attachment_name'),
                'has_attachment': bool(_obj_field(raw_c, 'attachment_path') or _obj_field(raw_c, 'has_attachment')),
                'display_status': f"企业台账备查（{vu_str}）",
                'highlight': False,
            })

    sec_qual = BlueprintSection(
        section_id='sec_qual',
        title='第二部分 资格审查与企业证照核对清单',
        slot_type='qualification_checklist',
        fill_mode='cert_ledger',
        tender_requirements=required_certs,
        content_blocks=s2_blocks,
        needs_manual_items=s2_manual,
        source_refs=[{'cert_summary': cert_summary}],
    )

    # ── Section 3: 开标一览表与分项报价表占位（严禁 AI 代填报价）──────────────────
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
        title='第三部分 开标一览表与分项报价表占位（严禁 AI 代填数字）',
        slot_type='price_placeholder',
        fill_mode='deterministic',
        tender_requirements=[{'price_cap': formatted_cap, 'package_no': package_no}],
        content_blocks=[
            {'kind': 'price_row', 'label': '项目名称', 'value': project_name, 'highlight': False},
            {'kind': 'price_row', 'label': '投标总报价（人民币小写）', 'value': total_placeholder, 'highlight': True},
            {'kind': 'price_row', 'label': '投标总报价（人民币大写）', 'value': upper_placeholder, 'highlight': True},
            {'kind': 'price_row', 'label': '质保期与交付期', 'value': '[待人工填报：与技术商务响应及投标函保持完全一致]', 'highlight': True},
            {'kind': 'paragraph', 'text': '合规提示：本部分故意屏蔽任何自动测算或示例报价数字，请商务预算人员根据成本测算自行填入真实报价后提交 F2 算术核验。', 'highlight': False},
        ],
        needs_manual_items=s3_manual,
        source_refs=[{'price_cap_yuan': formatted_cap}],
    )

    # ── Section 4: ★/▲ 实质性条款响应与偏离表初稿 ─────────────────────────────
    raw_marked = _filter_valid_requirements(list(checklist.get('marked_clauses') or []))
    raw_invalid = [
        r for r in (checklist.get('invalid_bid_clauses') or [])
        if classify_invalid_clause_role(str(r.get('text') or ''), str(r.get('clause_no') or '')) != 'submission_rule'
    ]
    combined_reqs: list[dict[str, Any]] = []
    seen_req_norm: set[str] = set()
    for req in raw_marked + raw_invalid:
        txt = str(req.get('full_text') or req.get('text') or '').strip()
        if not txt:
            continue
        k = normalize_zh(txt)[:60]
        if k in seen_req_norm:
            continue
        seen_req_norm.add(k)
        combined_reqs.append(req)

    by_clause, by_quote = _build_f4_lookup(f4_findings)
    s4_blocks: list[dict[str, Any]] = []
    s4_manual: list[dict[str, Any]] = []
    for idx, req in enumerate(combined_reqs, start=1):
        cno = str(req.get('clause_no') or f"条款{idx}").strip()
        mark = str(req.get('mark') or '').strip()
        req_text = str(req.get('full_text') or req.get('text') or '').strip()
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

    sec_deviation = BlueprintSection(
        section_id='sec_deviation',
        title='第四部分 实质性条款（★/▲）响应与技术/商务偏离表初稿',
        slot_type='deviation_table',
        fill_mode='deviation_table',
        tender_requirements=combined_reqs,
        content_blocks=s4_blocks,
        needs_manual_items=s4_manual,
        source_refs=[{'total_requirements': len(s4_blocks)}],
    )

    # ── Section 5: 技术与商务实施方案大纲（V1 outline_stub / V2 rag_knowledge）───
    topics = _collect_scoring_topics(checklist, full_text)
    provider = knowledge_provider
    if provider is None and compose_mode == 'knowledge_draft_v2' and knowledge_paths:
        provider = LocalKnowledgeSnippetProvider(list(knowledge_paths), client=client)

    use_rag = (
        compose_mode == 'knowledge_draft_v2'
        and provider is not None
        and bool(getattr(provider, 'has_documents', False))
    )

    s5_blocks: list[dict[str, Any]] = []
    s5_manual: list[dict[str, Any]] = []
    s5_refs: list[dict[str, Any]] = []
    matched_rag_count = 0

    for t in topics:
        query_text = f"{t['title']} {t['tender_quote']}".strip()
        sub_outline = [
            f"1. 需求响应与建设目标理解（对应得分点：{t['score_points']}）",
            "2. 具体技术路线、架构设计与实施细则 [待人工补充：结合本项目参数展开]",
            "3. 交付验收标准、风险应急预案与佐证截图索引 [待人工补充：附系统截图/检测报告]",
        ]
        ref_snippets: list[dict[str, Any]] = []
        if use_rag:
            raw_hits = provider.retrieve(query_text, top_k=2, min_score=0.35)
            for h in raw_hits:
                snip = str(h.get('snippet') or '').strip()
                if not snip:
                    continue
                aligned = _align_snippet_with_llm_optional(client, t, snip, full_text)
                ref_snippets.append({
                    'source_filename': str(h.get('source_filename') or '参考方案库'),
                    'snippet': aligned or snip,
                    'raw_quote': snip,
                    'score': float(h.get('score') or 0.0),
                    'quote_verified': bool(h.get('quote_verified', True)),
                    'llm_aligned': aligned is not None,
                    'notice': f"[参考片段 · 来源《{h.get('source_filename') or '参考方案库'}》 · 待人工确认改写，严禁直接照搬年份与金额]",
                })
                s5_refs.append({
                    'topic_id': t['topic_id'],
                    'source_filename': str(h.get('source_filename') or '参考方案库'),
                    'score': float(h.get('score') or 0.0),
                })

        if ref_snippets:
            matched_rag_count += 1
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

    sec_scoring_mode = 'rag_knowledge' if (use_rag and matched_rag_count > 0) else 'outline_stub'
    sec_scoring = BlueprintSection(
        section_id='sec_scoring',
        title='第五部分 技术与商务实施方案大纲（按评分细则拆解）',
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

    sections = [sec_cover, sec_qual, sec_price, sec_deviation, sec_scoring, sec_appendix]
    stats = {
        'section_count': len(sections),
        'required_cert_count': len(matched_certs),
        'deviation_row_count': len(s4_blocks),
        'scoring_topic_count': len(s5_blocks),
        'rag_matched_topics': matched_rag_count,
        'manual_item_count': len(manual_checklist),
        'scoring_fill_mode': sec_scoring_mode,
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
        sections=sections,
        manual_checklist=manual_checklist,
        cert_match_summary=cert_summary,
        stats=stats,
    )


def _set_run_font(run, *, name_zh: str = '宋体', size_pt: int = 10, bold: bool = False, color: Optional[RGBColor] = None) -> None:
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


def _shade_cell(cell, fill_hex: str) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    shd = tcPr.find(qn('w:shd'))
    if shd is not None:
        tcPr.remove(shd)
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), fill_hex)
    tcPr.append(shd)


def _add_styled_paragraph(
    doc: Document,
    text: str,
    *,
    bold: bool = False,
    color: Optional[RGBColor] = None,
    highlight_manual: bool = False,
    font_size: int = 10,
) -> None:
    p = doc.add_paragraph()
    run = p.add_run(text)
    _set_run_font(run, name_zh='宋体', size_pt=font_size, bold=bold, color=color)
    if highlight_manual:
        run.font.highlight_color = WD_COLOR_INDEX.YELLOW


def export_composed_bid_docx(
    blueprint: BidBlueprint | dict[str, Any],
    output_path: str | Path,
) -> Path:
    """将 BidBlueprint IR 渲染导出为结构化《投标文件初稿骨架.docx》。"""
    bp = blueprint.to_dict() if isinstance(blueprint, BidBlueprint) else dict(blueprint)
    project_name = str(bp.get('project_name') or '拟投标项目')

    doc = Document()
    # 页眉页脚强制使用本次招标项目名，绝不残留历史项目名称
    for sec in doc.sections:
        hdr = sec.header.paragraphs[0] if sec.header.paragraphs else sec.header.add_paragraph()
        hdr.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        r_hdr = hdr.add_run(f"《{project_name}》投标文件初稿骨架（系统辅助编制·待人工终核）")
        _set_run_font(r_hdr, size_pt=8, color=_COLOR_MUTED)

        ftr = sec.footer.paragraphs[0] if sec.footer.paragraphs else sec.footer.add_paragraph()
        ftr.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r_ftr = ftr.add_run("免责声明：本初稿依据招标文件与企业台账自动装配，报价/业绩/签章与技术参数须人工逐项核实定稿。")
        _set_run_font(r_ftr, size_pt=8, color=_COLOR_MUTED)

    title_p = doc.add_paragraph()
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r_title = title_p.add_run(f"{project_name}\n投标文件（初稿骨架）")
    _set_run_font(r_title, name_zh='黑体', size_pt=18, bold=True, color=_COLOR_NAVY)

    sub_p = doc.add_paragraph()
    sub_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    mode_label = 'V2 方案库参考装配' if bp.get('compose_mode') == 'knowledge_draft_v2' else 'V1 零幻觉骨架装配'
    r_sub = sub_p.add_run(
        f"编制模式：{mode_label} ｜ 依据招标文件：{bp.get('tender_filename', '')} ｜ "
        f"生成时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    _set_run_font(r_sub, size_pt=9, color=_COLOR_MUTED)

    for sec_data in bp.get('sections') or []:
        h_p = doc.add_paragraph()
        r_h = h_p.add_run(str(sec_data.get('title') or ''))
        _set_run_font(r_h, name_zh='黑体', size_pt=13, bold=True, color=_COLOR_NAVY)

        slot_type = sec_data.get('slot_type')
        blocks = sec_data.get('content_blocks') or []

        if slot_type in ('cover_and_letter', 'price_placeholder'):
            kv_rows = [b for b in blocks if b.get('kind') in ('meta', 'placeholder', 'price_row')]
            if kv_rows:
                tbl = doc.add_table(rows=1, cols=2)
                tbl.style = 'Table Grid'
                tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
                tbl.rows[0].cells[0].text = '项目信息项'
                tbl.rows[0].cells[1].text = '拟响应内容 / 核对占位'
                for c in tbl.rows[0].cells:
                    _shade_cell(c, 'F1F5F9')
                for item in kv_rows:
                    row_cells = tbl.add_row().cells
                    row_cells[0].text = str(item.get('label') or '')
                    p_val = row_cells[1].paragraphs[0]
                    r_val = p_val.add_run(str(item.get('value') or ''))
                    is_hl = bool(item.get('highlight'))
                    _set_run_font(r_val, size_pt=10, bold=is_hl, color=_COLOR_RED if is_hl else _COLOR_NAVY)
                    if is_hl:
                        r_val.font.highlight_color = WD_COLOR_INDEX.YELLOW
            for b in blocks:
                if b.get('kind') == 'paragraph':
                    _add_styled_paragraph(
                        doc,
                        str(b.get('text') or ''),
                        color=_COLOR_AMBER if b.get('highlight') else _COLOR_NAVY,
                        highlight_manual=bool(b.get('highlight')),
                    )

        elif slot_type == 'qualification_checklist':
            cert_rows = [b for b in blocks if b.get('kind') == 'cert_row']
            if cert_rows:
                tbl = doc.add_table(rows=1, cols=5)
                tbl.style = 'Table Grid'
                tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
                headers = ('招标所需证照/资质', '要求级别', '台账核对结果', '有效期/证号', '原件关联')
                for i, h in enumerate(headers):
                    tbl.rows[0].cells[i].text = h
                    _shade_cell(tbl.rows[0].cells[i], 'F1F5F9')
                for b in cert_rows:
                    rc = tbl.add_row().cells
                    rc[0].text = str(b.get('cert_name') or '')
                    rc[1].text = '必交资格项' if b.get('requirement_level') == 'mandatory' else '加分/参考项'
                    p_st = rc[2].paragraphs[0]
                    st = str(b.get('ledger_status') or '')
                    r_st = p_st.add_run(str(b.get('display_status') or ''))
                    col_st = _COLOR_GREEN if st == 'valid' else (_COLOR_RED if st in ('expired', 'not_in_ledger') else _COLOR_AMBER)
                    _set_run_font(r_st, size_pt=9, bold=(st != 'valid'), color=col_st)
                    rc[3].text = f"{b.get('valid_until') or '—'}\n{b.get('cert_no') or ''}".strip()
                    rc[4].text = str(b.get('attachment_name') or '未上传原件')
            else:
                _add_styled_paragraph(doc, '[待人工补充：未提取到明确资格证照，请核对招标文件原文]', highlight_manual=True)

        elif slot_type == 'deviation_table':
            dev_rows = [b for b in blocks if b.get('kind') == 'deviation_row']
            if dev_rows:
                tbl = doc.add_table(rows=1, cols=5)
                tbl.style = 'Table Grid'
                tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
                for i, h in enumerate(('序号', '招标条款号', '招标实质性要求', '我方响应（初稿占位）', '核对状态')):
                    tbl.rows[0].cells[i].text = h
                    _shade_cell(tbl.rows[0].cells[i], 'F1F5F9')
                for b in dev_rows:
                    rc = tbl.add_row().cells
                    rc[0].text = str(b.get('seq') or '')
                    rc[1].text = str(b.get('clause_no') or '')
                    rc[2].text = str(b.get('tender_requirement') or '')
                    p_resp = rc[3].paragraphs[0]
                    r_resp = p_resp.add_run(str(b.get('bid_response') or ''))
                    is_hl = bool(b.get('highlight'))
                    _set_run_font(r_resp, size_pt=9, bold=is_hl, color=_COLOR_AMBER if is_hl else _COLOR_NAVY)
                    if is_hl:
                        r_resp.font.highlight_color = WD_COLOR_INDEX.YELLOW
                    rc[4].text = str(b.get('deviation') or '')
            else:
                _add_styled_paragraph(doc, '[待人工补充：请结合第四章技术与商务要求核填偏离表]', highlight_manual=True)

        elif slot_type == 'scoring_response_outline':
            for b in blocks:
                if b.get('kind') != 'scoring_topic':
                    continue
                _add_styled_paragraph(
                    doc,
                    f"● {b.get('title')}（{b.get('score_points')}）",
                    bold=True,
                    color=_COLOR_NAVY,
                    font_size=11,
                )
                if b.get('tender_quote'):
                    _add_styled_paragraph(
                        doc,
                        f"  招标依据：{b.get('tender_quote')}",
                        color=_COLOR_MUTED,
                        font_size=9,
                    )
                for line in b.get('sub_outline') or []:
                    _add_styled_paragraph(
                        doc,
                        f"  {line}",
                        highlight_manual='[待人工补充' in line,
                        font_size=10,
                    )
                for sn in b.get('reference_snippets') or []:
                    tbl = doc.add_table(rows=1, cols=1)
                    tbl.style = 'Table Grid'
                    cell = tbl.rows[0].cells[0]
                    _shade_cell(cell, 'F8FAFC')
                    p_notice = cell.paragraphs[0]
                    r_n = p_notice.add_run(str(sn.get('notice') or '') + '\n')
                    _set_run_font(r_n, size_pt=9, bold=True, color=_COLOR_AMBER)
                    p_snip = cell.add_paragraph()
                    r_s = p_snip.add_run(str(sn.get('snippet') or ''))
                    _set_run_font(r_s, size_pt=9, color=_COLOR_MUTED)

        elif slot_type == 'cert_ledger_appendix':
            for b in blocks:
                if b.get('kind') == 'manual_summary':
                    items = b.get('items') or []
                    _add_styled_paragraph(doc, f"共 {len(items)} 项待人工补充/核对确认：", bold=True, color=_COLOR_RED if items else _COLOR_GREEN)
                    for idx, mi in enumerate(items, start=1):
                        _add_styled_paragraph(
                            doc,
                            f"{idx}. [{mi.get('field')}] {mi.get('reason')}（建议：{mi.get('hint')}）",
                            highlight_manual=True,
                            font_size=9,
                        )
            ledger_rows = [b for b in blocks if b.get('kind') == 'ledger_appendix_row']
            if ledger_rows:
                tbl = doc.add_table(rows=1, cols=4)
                tbl.style = 'Table Grid'
                tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
                for i, h in enumerate(('企业台账证照', '证号', '效期类型', '原件附件')):
                    tbl.rows[0].cells[i].text = h
                    _shade_cell(tbl.rows[0].cells[i], 'F1F5F9')
                for lr in ledger_rows:
                    rc = tbl.add_row().cells
                    rc[0].text = str(lr.get('cert_name') or '')
                    rc[1].text = str(lr.get('cert_no') or '')
                    rc[2].text = str(lr.get('valid_until') or '')
                    rc[3].text = str(lr.get('attachment_name') or '')

    out_p = Path(output_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_p))
    return out_p
