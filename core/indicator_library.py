# -*- coding: utf-8 -*-
"""E41 · 公司常见指标标准应答库与双业务线（网络安全 / 并行 MaaS）0-Token 检索引擎。

设计原则：
1. 三层业务线叠加：`common`（通用政采必考售后 SLA / 实施 / 培训 / 保密 / 常规商务）为共享底座，
   `security`（网络安全与数据安全）与 `maas`（并行科技 MaaS 大模型算力与 Token 服务）在选中时叠加 `common`，
   `general`（通用写作 Skill）仅消费 `common` 层；`security` 与 `maas` 严格跨线隔离；
2. 实现 `KnowledgeSnippetProvider` 协议（`has_documents` / `retrieve`），复用 `bid_composer.py` 现有插槽，
   并与用户上传的历史标书 `LocalKnowledgeSnippetProvider` 通过 `CompositeKnowledgeSnippetProvider` 叠加（指标优先）；
3. 0-Token 检索：纯 Python BM25 + 招投标领域同义词 + `keywords`/`category`/`indicator_name` 加权门控，绝不调 LLM；
4. 防废标硬锁：条目严格禁止包含可直接报价的金额数字，商务报价数字一律为 `[待人工填报：...]` 槽位，
   未经公司确认的 SLA 硬数字一律保留 `[待人工确认：...]` 槽位。
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Optional, Sequence

from core.hybrid_retriever import _tokenize_for_bm25, expand_query_synonyms
from core.quote_verifier import normalize_zh

DEFAULT_INDICATOR_LIBRARY_DIR = (
    Path(__file__).resolve().parents[1] / 'data' / 'rules' / 'indicator_library'
)

VALID_ENTRY_LINES = ('common', 'security', 'maas')
VALID_SKILL_BIZ_LINES = ('general', 'security', 'maas')

_BIZ_LINE_LABELS: dict[str, str] = {
    'general': '通用政采常见指标',
    'common': '通用政采常见指标',
    'security': '网络安全与数据安全',
    'maas': '并行科技 MaaS 平台（Token/算力服务）',
}

# 检测疑似具体人民币报价金额（例如 ￥46000、46000元、12.5万元），用于指标入库/检索安全拦截
_BID_MONEY_RE = re.compile(
    r'(?:￥|¥|人民币)\s*\d[\d,]*(?:\.\d+)?(?:\s*万?元)?'
    r'|\d[\d,]*(?:\.\d+)?\s*(?:万元|亿元|元(?![/／]?(?:M|百万)\s*Tokens))'
)

# 允许出现的非报价类明确法律/行业口径词上下文（如 12个月、100% 等不含币种单位的本来就不命中）
_PRICE_UNIT_LINE_HINT_RE = re.compile(r'(?:\d+(?:\.\d+)?)\s*(?:元|￥|¥)\s*/\s*(?:M|百万|千)\s*Tokens?', re.IGNORECASE)

_LINE_FILENAME_MAP: dict[str, str] = {
    'common': 'common_indicators.json',
    'security': 'security_indicators.json',
    'maas': 'maas_indicators.json',
}


def normalize_skill_biz_line(raw: Any) -> str:
    """规范化 Skill 的业务线标识，非法值保守回退为 'general'。"""
    val = str(raw or '').strip().lower()
    if val in VALID_SKILL_BIZ_LINES:
        return val
    if val == 'common':
        return 'general'
    return 'general'


def resolve_target_entry_lines(biz_line: Any) -> tuple[str, ...]:
    """根据 Skill 或请求选择的业务线，解析需加载的指标库层级（业务线专精 + common 底座）。"""
    cleaned = str(biz_line or '').strip().lower()
    if cleaned == 'security':
        return ('security', 'common')
    if cleaned == 'maas':
        return ('maas', 'common')
    return ('common',)


def sanitize_indicator_text(text: str) -> str:
    """防废标硬锁：屏蔽指标正文中的具体投标报价金额数字，替换为待人工填报占位槽。"""
    raw = str(text or '').strip()
    if not raw:
        return ''
    cleaned = _PRICE_UNIT_LINE_HINT_RE.sub('[待人工填报：Token 单价 ___ ￥/M Tokens]', raw)
    cleaned = _BID_MONEY_RE.sub('[待人工填报：具体金额]', cleaned)
    return cleaned


def compute_library_content_hash(entries: Sequence[dict[str, Any]]) -> str:
    """基于指标核心字段计算确定性 SHA256 哈希（剔除审核时间戳，保障重跑幂等）。"""
    canonical: list[dict[str, Any]] = []
    for item in entries:
        canonical.append({
            'indicator_id': str(item.get('indicator_id') or ''),
            'line': str(item.get('line') or ''),
            'category': str(item.get('category') or ''),
            'indicator_name': str(item.get('indicator_name') or ''),
            'standard_response': str(item.get('standard_response') or ''),
            'scoring_ref': item.get('scoring_ref') or {},
            'param_slots': list(item.get('param_slots') or []),
            'keywords': list(item.get('keywords') or []),
            'source_ref': str(item.get('source_ref') or ''),
        })
    canonical.sort(key=lambda x: (x['line'], x['indicator_id'], x['indicator_name']))
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _normalize_scoring_ref(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {'typical_points': [], 'weight_hint': '', 'high_score_tips': ''}
    pts = [str(p).strip() for p in (raw.get('typical_points') or []) if str(p).strip()]
    return {
        'typical_points': pts[:8],
        'weight_hint': str(raw.get('weight_hint') or '').strip(),
        'high_score_tips': str(raw.get('high_score_tips') or '').strip(),
    }


def _normalize_entry(raw: Any, *, fallback_line: str) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    line = str(raw.get('line') or fallback_line).strip().lower()
    if line not in VALID_ENTRY_LINES:
        line = fallback_line if fallback_line in VALID_ENTRY_LINES else 'common'
    name = str(raw.get('indicator_name') or '').strip()
    std_resp = sanitize_indicator_text(str(raw.get('standard_response') or ''))
    if not name or not std_resp:
        return None

    iid = str(raw.get('indicator_id') or '').strip()
    if not iid:
        iid = f"{line}_{hashlib.sha256(name.encode('utf-8')).hexdigest()[:8]}"

    keywords = [str(k).strip() for k in (raw.get('keywords') or []) if str(k).strip()]
    if name not in keywords:
        keywords.insert(0, name)

    slots = [str(s).strip() for s in (raw.get('param_slots') or []) if str(s).strip()]
    return {
        'indicator_id': iid,
        'line': line,
        'category': str(raw.get('category') or '技术方案').strip(),
        'indicator_name': name,
        'standard_response': std_resp,
        'scoring_ref': _normalize_scoring_ref(raw.get('scoring_ref')),
        'param_slots': slots,
        'keywords': keywords,
        'source_ref': str(raw.get('source_ref') or '').strip(),
    }


def _load_single_line_payload(
    line: str,
    lib_dir: Path,
) -> dict[str, Any]:
    fname = _LINE_FILENAME_MAP.get(line)
    if not fname:
        return {}
    fpath = lib_dir / fname
    if not fpath.is_file():
        return {}
    try:
        raw_obj = json.loads(fpath.read_text(encoding='utf-8'))
    except Exception:
        return {}
    if not isinstance(raw_obj, dict):
        return {}

    raw_entries = raw_obj.get('entries')
    if not isinstance(raw_entries, list):
        raw_entries = []
    norm_entries: list[dict[str, Any]] = []
    for raw_e in raw_entries:
        norm_e = _normalize_entry(raw_e, fallback_line=line)
        if norm_e is not None and norm_e['line'] == line:
            norm_entries.append(norm_e)

    return {
        'schema_version': str(raw_obj.get('schema_version') or '1.0'),
        'line': line,
        'display_name': str(raw_obj.get('display_name') or _BIZ_LINE_LABELS.get(line, line)),
        'description': str(raw_obj.get('description') or ''),
        'review_status': str(raw_obj.get('review_status') or 'pending_human_review'),
        'reviewed_at': raw_obj.get('reviewed_at'),
        'reviewed_by': raw_obj.get('reviewed_by'),
        'content_hash': str(raw_obj.get('content_hash') or compute_library_content_hash(norm_entries)),
        'line_scoring_summary': list(raw_obj.get('line_scoring_summary') or []),
        'entries': norm_entries,
    }


def load_indicator_library(
    biz_line: str = 'general',
    *,
    library_dir: Optional[str | Path] = None,
) -> list[dict[str, Any]]:
    """加载指定业务线可用指标列表（自动叠加 `common` 通用指标底座，专精线条目优先）。"""
    lib_dir = Path(library_dir) if library_dir else DEFAULT_INDICATOR_LIBRARY_DIR
    target_lines = resolve_target_entry_lines(biz_line)
    entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for ln in target_lines:
        payload = _load_single_line_payload(ln, lib_dir)
        for item in payload.get('entries') or []:
            eid = item['indicator_id']
            if eid in seen_ids:
                continue
            seen_ids.add(eid)
            entries.append(item)
    return entries


def get_biz_line_indicator_overview(
    biz_line: str = 'general',
    *,
    library_dir: Optional[str | Path] = None,
) -> dict[str, Any]:
    """返回选定业务线的核心指标清单与典型评分标准参考（供前端预览卡片及接口直接透出）。"""
    norm_line = normalize_skill_biz_line(biz_line)
    lib_dir = Path(library_dir) if library_dir else DEFAULT_INDICATOR_LIBRARY_DIR
    target_lines = resolve_target_entry_lines(norm_line)

    entries: list[dict[str, Any]] = []
    scoring_summary: list[dict[str, Any]] = []
    review_statuses: list[str] = []
    reviewed_notes: list[str] = []

    for ln in target_lines:
        payload = _load_single_line_payload(ln, lib_dir)
        if not payload:
            continue
        review_statuses.append(str(payload.get('review_status') or 'pending_human_review'))
        if payload.get('reviewed_at'):
            reviewed_notes.append(f"{ln}:{payload.get('reviewed_by') or '已复核'}@{payload['reviewed_at']}")
        if ln == norm_line or (norm_line == 'general' and ln == 'common'):
            scoring_summary.extend(payload.get('line_scoring_summary') or [])
        entries.extend(payload.get('entries') or [])

    overall_review = 'reviewed' if (review_statuses and all(s == 'reviewed' for s in review_statuses)) else 'pending_human_review'
    return {
        'biz_line': norm_line,
        'biz_line_label': _BIZ_LINE_LABELS.get(norm_line, norm_line),
        'included_lines': list(target_lines),
        'review_status': overall_review,
        'reviewed_notes': reviewed_notes,
        'total_indicators': len(entries),
        'line_scoring_summary': scoring_summary,
        'indicators': entries,
    }


def _build_entry_searchable_text(entry: dict[str, Any]) -> str:
    sr = entry.get('scoring_ref') or {}
    pts = ' '.join(sr.get('typical_points') or [])
    kws = ' '.join(entry.get('keywords') or [])
    return (
        f"{entry.get('indicator_name', '')} {entry.get('category', '')} "
        f"{kws} {pts} {entry.get('standard_response', '')}"
    ).strip()


def _format_entry_snippet(entry: dict[str, Any]) -> str:
    """将指标条目格式化为含 [公司标准应答参考] 前缀与 [评分要点参考] 的参考片段。"""
    sr = entry.get('scoring_ref') or {}
    tips = str(sr.get('high_score_tips') or '').strip()
    weight = str(sr.get('weight_hint') or '').strip()
    pts = [str(p).strip() for p in (sr.get('typical_points') or []) if str(p).strip()]

    lines = [
        f"[公司标准应答参考｜{entry['indicator_name']}]（分类：{entry['category']}）",
        entry['standard_response'],
    ]
    if weight or pts or tips:
        score_parts: list[str] = []
        if weight:
            score_parts.append(f"常见分值权重：{weight}")
        if pts:
            score_parts.append(f"常见评分点：{'；'.join(pts[:3])}")
        if tips:
            score_parts.append(f"高分应答提示：{tips}")
        lines.append(f"[评分标准参考] {'｜'.join(score_parts)}")
    if entry.get('param_slots'):
        lines.append(f"[需企业确认参数槽] {'；'.join(entry['param_slots'][:4])}")
    return '\n'.join(lines)


class IndicatorLibraryProvider:
    """E41 0-Token 指标标准应答库检索适配器（实现 `KnowledgeSnippetProvider` 协议）。"""

    def __init__(
        self,
        biz_line: str = 'general',
        *,
        library_dir: Optional[str | Path] = None,
    ) -> None:
        self.biz_line = normalize_skill_biz_line(biz_line)
        self.target_lines = resolve_target_entry_lines(self.biz_line)
        self._entries = load_indicator_library(self.biz_line, library_dir=library_dir)
        self._doc_tokens: list[list[str]] = [
            _tokenize_for_bm25(_build_entry_searchable_text(e), expand_syn=True)
            for e in self._entries
        ]
        self._doc_token_sets: list[set[str]] = [set(toks) for toks in self._doc_tokens]
        self._idf: dict[str, float] = {}
        self._avg_len: float = 1.0
        self._build_bm25_index()

    @property
    def has_documents(self) -> bool:
        return bool(self._entries)

    def _build_bm25_index(self) -> None:
        n_docs = len(self._doc_tokens)
        if n_docs == 0:
            self._idf = {}
            self._avg_len = 1.0
            return
        total_len = sum(len(t) for t in self._doc_tokens)
        self._avg_len = max(1.0, float(total_len) / float(n_docs))
        df: dict[str, int] = {}
        for tset in self._doc_token_sets:
            for tok in tset:
                df[tok] = df.get(tok, 0) + 1
        self._idf = {
            tok: math.log((n_docs - freq + 0.5) / (freq + 0.5) + 1.0)
            for tok, freq in df.items()
        }

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 2,
        min_score: float = 0.35,
    ) -> list[dict[str, Any]]:
        """三级门控 0-Token 检索：词面关键词精确加权 + BM25 同义词扩展 + 跨业务线严格隔离。"""
        if not self._entries or not (query or '').strip():
            return []

        q_clean = query.strip()
        q_low = q_clean.lower()
        q_norm = normalize_zh(q_clean)
        q_syns = expand_query_synonyms(q_clean)
        q_tokens = _tokenize_for_bm25(q_clean, expand_syn=True)
        q_tok_set = set(q_tokens)

        k1 = 1.5
        b = 0.75
        scored: list[tuple[float, dict[str, Any]]] = []

        for idx, entry in enumerate(self._entries):
            doc_toks = self._doc_tokens[idx]
            doc_tset = self._doc_token_sets[idx]

            # 1) 关键词 / 指标名 / 分类精确子串门控（高置信直通）
            kw_hits = 0
            for kw in entry.get('keywords') or []:
                kw_l = kw.lower()
                if len(kw_l) >= 2 and (kw_l in q_low or normalize_zh(kw_l) in q_norm):
                    kw_hits += 1
            for syn in q_syns:
                syn_l = syn.lower()
                if len(syn_l) >= 2 and any(syn_l in str(ek).lower() for ek in (entry.get('keywords') or [])):
                    kw_hits += 1

            cat_hit = 1 if (entry.get('category', '').lower() and entry['category'].lower() in q_low) else 0

            # 2) BM25 分数计算
            bm25_score = 0.0
            if doc_toks and q_tok_set:
                tf_map: dict[str, int] = {}
                for t in doc_toks:
                    tf_map[t] = tf_map.get(t, 0) + 1
                dl = float(len(doc_toks))
                for qt in q_tok_set:
                    tf = tf_map.get(qt, 0)
                    if tf <= 0:
                        continue
                    idf = self._idf.get(qt, 0.25)
                    bm25_score += idf * (tf * (k1 + 1.0)) / (tf + k1 * (1.0 - b + b * dl / self._avg_len))

            overlap = q_tok_set & doc_tset
            has_syn_overlap = any(t.startswith('__syn_group_') for t in overlap)
            raw_token_overlap = len(overlap)

            # 严苛门控：既无关键词/同义词组精确命中，又仅有零散单字 2-gram 巧合，则拒检防噪声污染
            if kw_hits == 0 and cat_hit == 0 and not has_syn_overlap and raw_token_overlap < 4:
                continue

            # 3) 综合归一化分值（专精线微幅优先于通用底座）
            line_bonus = 0.15 if (self.biz_line in ('security', 'maas') and entry['line'] == self.biz_line) else 0.0
            norm_score = min(
                1.0,
                0.25 * min(kw_hits, 3) + 0.12 * cat_hit + 0.08 * min(bm25_score, 5.0) + line_bonus,
            )
            if norm_score < min_score:
                continue

            scored.append((norm_score, entry))

        scored.sort(
            key=lambda pair: (
                pair[0],
                1 if pair[1]['line'] == self.biz_line else 0,
            ),
            reverse=True,
        )

        out: list[dict[str, Any]] = []
        for score_val, entry in scored[:max(1, top_k)]:
            src_label = f"公司指标标准库（{_BIZ_LINE_LABELS.get(entry['line'], entry['line'])}）"
            out.append({
                'source_filename': src_label,
                'snippet': _format_entry_snippet(entry),
                'quote': entry['indicator_name'],
                'offset': 0,
                'score': round(float(score_val), 4),
                'quote_verified': True,
                'is_indicator_ref': True,
                'indicator_id': entry['indicator_id'],
                'line': entry['line'],
                'category': entry['category'],
                'indicator_name': entry['indicator_name'],
                'scoring_ref': entry.get('scoring_ref') or {},
                'param_slots': list(entry.get('param_slots') or []),
                'notice': (
                    f"[公司标准指标应答参考｜{entry['indicator_name']}｜"
                    f"来源《{src_label}》｜仅供参考，须结合本项目实际参数人工核定]"
                ),
            })
        return out


class CompositeKnowledgeSnippetProvider:
    """将多个 `KnowledgeSnippetProvider` 组合检索（默认公司指标库片段优先、历史方案库片段互补去重）。"""

    def __init__(self, providers: Sequence[Any]) -> None:
        self._providers = [p for p in (providers or []) if p is not None and bool(getattr(p, 'has_documents', False))]

    @property
    def has_documents(self) -> bool:
        return bool(self._providers)

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 2,
        min_score: float = 0.35,
    ) -> list[dict[str, Any]]:
        if not self._providers:
            return []
        merged: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        limit = max(1, int(top_k))
        # 先从排在首位的 provider（指标库）取优先条目，再用后续 provider（V2 历史标书库）补足配额或补充 1 条对照
        max_collect = max(limit, 3) if len(self._providers) > 1 else limit
        for prov in self._providers:
            try:
                hits = prov.retrieve(query, top_k=limit, min_score=min_score)
            except Exception:
                hits = []
            for h in hits or []:
                snip = str(h.get('snippet') or '').strip()
                k = normalize_zh(snip)[:60]
                if not k or k in seen_keys:
                    continue
                seen_keys.add(k)
                merged.append(h)
                if len(merged) >= max_collect:
                    return merged
        return merged
