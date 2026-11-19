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

import datetime
import hashlib
import json
import math
import os
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

_LINE_KEY_RE = re.compile(r'^[a-z][a-z0-9_]{1,23}$')
_PENDING_SLOT_RE = re.compile(r'\[待人工(?:确认|填报)[^\]]*\]')


def validate_indicator_line_key(raw: Any) -> str:
    """校验并规范化指标库业务线标识（仅允许 `^[a-z][a-z0-9_]{1,23}$`，严防路径穿越）。"""
    val = str(raw or '').strip().lower()
    if val == 'general':
        val = 'common'
    if not _LINE_KEY_RE.match(val):
        raise ValueError(
            f"业务线标识 {raw!r} 格式非法：须以小写字母开头，仅含小写字母、数字或下划线，长度 2~24 位"
        )
    return val


def _resolve_line_filepath(line_key: str, lib_dir: Path) -> Path:
    clean_key = validate_indicator_line_key(line_key)
    base_dir = lib_dir.resolve()
    fname = _LINE_FILENAME_MAP.get(clean_key, f"{clean_key}_indicators.json")
    fpath = (base_dir / fname).resolve()
    if fpath.parent != base_dir:
        raise ValueError(f"检测到非法路径穿越：{line_key!r}")
    return fpath


def _atomic_write_json(fpath: Path, payload: dict[str, Any]) -> None:
    """UTF-8 + LF + 临时文件 os.replace 原子落盘。"""
    fpath.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = fpath.with_name(f".{fpath.name}.{os.getpid()}.tmp")
    content = json.dumps(payload, ensure_ascii=False, indent=2) + '\n'
    try:
        tmp_path.write_text(content, encoding='utf-8', newline='\n')
        os.replace(str(tmp_path), str(fpath))
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def count_pending_slots_in_entry(entry: dict[str, Any]) -> int:
    """统计单条指标正文中仍保留的 `[待人工确认：...]` / `[待人工填报：...]` 槽位数。"""
    resp = str(entry.get('standard_response') or '')
    return len(_PENDING_SLOT_RE.findall(resp))


def normalize_skill_biz_line(raw: Any) -> str:
    """规范化 Skill 的业务线标识（兼容内置 general/security/maas 与自定义业务线，非法值回退 'general'）。"""
    val = str(raw or '').strip().lower()
    if val in VALID_SKILL_BIZ_LINES:
        return val
    if val == 'common':
        return 'general'
    if _LINE_KEY_RE.match(val):
        return val
    return 'general'


def resolve_target_entry_lines(biz_line: Any) -> tuple[str, ...]:
    """根据 Skill 或请求选择的业务线，解析需加载的指标库层级（业务线专精 + common 底座）。"""
    cleaned = str(biz_line or '').strip().lower()
    if cleaned in ('', 'general', 'common'):
        return ('common',)
    if _LINE_KEY_RE.match(cleaned):
        return (cleaned, 'common')
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
    pts = [
        sanitize_indicator_text(str(p).strip())
        for p in (raw.get('typical_points') or [])
        if str(p).strip()
    ]
    return {
        'typical_points': [p for p in pts if p][:8],
        'weight_hint': sanitize_indicator_text(str(raw.get('weight_hint') or '').strip()),
        'high_score_tips': sanitize_indicator_text(str(raw.get('high_score_tips') or '').strip()),
    }


def _normalize_entry(raw: Any, *, fallback_line: str) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    fb_clean = fallback_line if _LINE_KEY_RE.match(str(fallback_line or '')) else 'common'
    line = str(raw.get('line') or fb_clean).strip().lower()
    if not _LINE_KEY_RE.match(line):
        line = fb_clean
    name = sanitize_indicator_text(str(raw.get('indicator_name') or '').strip())
    std_resp = sanitize_indicator_text(str(raw.get('standard_response') or ''))
    if not name or not std_resp:
        return None

    iid = str(raw.get('indicator_id') or '').strip()
    if not iid:
        iid = f"{line}_{hashlib.sha256(name.encode('utf-8')).hexdigest()[:8]}"

    keywords = [
        sanitize_indicator_text(str(k).strip())
        for k in (raw.get('keywords') or [])
        if str(k).strip()
    ]
    keywords = [k for k in keywords if k]
    if name not in keywords:
        keywords.insert(0, name)

    slots = [
        sanitize_indicator_text(str(s).strip())
        for s in (raw.get('param_slots') or [])
        if str(s).strip()
    ]
    slots = [s for s in slots if s]
    return {
        'indicator_id': iid,
        'line': line,
        'category': sanitize_indicator_text(str(raw.get('category') or '技术方案').strip()) or '技术方案',
        'indicator_name': name,
        'standard_response': std_resp,
        'scoring_ref': _normalize_scoring_ref(raw.get('scoring_ref')),
        'param_slots': slots,
        'keywords': keywords,
        'source_ref': sanitize_indicator_text(str(raw.get('source_ref') or '').strip()),
    }


def _load_single_line_payload(
    line: str,
    lib_dir: Path,
) -> dict[str, Any]:
    try:
        fpath = _resolve_line_filepath(line, lib_dir)
    except ValueError:
        return {}
    if not fpath.is_file():
        return {}
    try:
        raw_obj = json.loads(fpath.read_text(encoding='utf-8'))
    except Exception:
        return {}
    if not isinstance(raw_obj, dict):
        return {}

    clean_line = validate_indicator_line_key(line)
    raw_entries = raw_obj.get('entries')
    if not isinstance(raw_entries, list):
        raw_entries = []
    norm_entries: list[dict[str, Any]] = []
    for raw_e in raw_entries:
        norm_e = _normalize_entry(raw_e, fallback_line=clean_line)
        if norm_e is not None and norm_e['line'] == clean_line:
            norm_entries.append(norm_e)

    disp_name = str(raw_obj.get('display_name') or _BIZ_LINE_LABELS.get(clean_line, clean_line))
    if clean_line not in _BIZ_LINE_LABELS and disp_name:
        _BIZ_LINE_LABELS[clean_line] = disp_name

    return {
        'schema_version': str(raw_obj.get('schema_version') or '1.0'),
        'line': clean_line,
        'display_name': disp_name,
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

    if review_statuses and all(s in ('approved', 'reviewed') for s in review_statuses):
        overall_review = 'approved' if 'approved' in review_statuses else 'reviewed'
    else:
        overall_review = 'pending_human_review'
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


def _normalize_line_scoring_summary(raw_list: Any) -> list[dict[str, str]]:
    if not isinstance(raw_list, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw_list[:16]:
        if not isinstance(item, dict):
            continue
        dim = sanitize_indicator_text(str(item.get('dimension') or '').strip())
        if not dim:
            continue
        out.append({
            'dimension': dim,
            'typical_weight': sanitize_indicator_text(str(item.get('typical_weight') or '').strip()),
            'key_focus': sanitize_indicator_text(str(item.get('key_focus') or '').strip()),
        })
    return out


def _format_line_record(payload: dict[str, Any]) -> dict[str, Any]:
    ln = str(payload.get('line') or 'common')
    entries = list(payload.get('entries') or [])
    return {
        'line': ln,
        'display_name': str(payload.get('display_name') or _BIZ_LINE_LABELS.get(ln, ln)),
        'description': str(payload.get('description') or ''),
        'is_builtin': ln in VALID_ENTRY_LINES,
        'review_status': str(payload.get('review_status') or 'pending_human_review'),
        'reviewed_at': payload.get('reviewed_at'),
        'reviewed_by': payload.get('reviewed_by'),
        'content_hash': str(payload.get('content_hash') or compute_library_content_hash(entries)),
        'total_indicators': len(entries),
        'pending_slot_count': sum(count_pending_slots_in_entry(e) for e in entries),
        'line_scoring_summary': list(payload.get('line_scoring_summary') or []),
        'indicators': entries,
    }


def list_indicator_lines(
    *,
    library_dir: Optional[str | Path] = None,
) -> list[dict[str, Any]]:
    """E45 §2.1：列出全部可用指标库业务线（内置三线 + 同目录下合法自定义 `{line}_indicators.json`）。"""
    lib_dir = Path(library_dir) if library_dir else DEFAULT_INDICATOR_LIBRARY_DIR
    discovered_keys: list[str] = list(VALID_ENTRY_LINES)
    if lib_dir.is_dir():
        for fp in sorted(lib_dir.glob('*_indicators.json')):
            stem = fp.name[:-len('_indicators.json')]
            if stem not in discovered_keys and _LINE_KEY_RE.match(stem):
                discovered_keys.append(stem)

    lines_out: list[dict[str, Any]] = []
    for lk in discovered_keys:
        payload = _load_single_line_payload(lk, lib_dir)
        if not payload:
            continue
        lines_out.append(_format_line_record(payload))
    return lines_out


def create_indicator_line(
    line_key: Optional[str] = None,
    display_name: str = '',
    description: str = '',
    line_scoring_summary: Optional[list[dict[str, Any]]] = None,
    *,
    line: Optional[str] = None,
    library_dir: Optional[str | Path] = None,
) -> dict[str, Any]:
    """E45 §2.1：创建新的自定义业务线指标库文件（原子落盘，默认 `pending_human_review`）。"""
    eff_key = line_key if line_key is not None else line
    raw_key = str(eff_key or '').strip().lower()
    if raw_key == 'general' or not _LINE_KEY_RE.match(raw_key):
        raise ValueError(
            f"业务线标识 {eff_key!r} 非法：须以小写字母开头，仅含小写字母/数字/下划线，长度 2~24 位，且不可为 'general'"
        )
    disp = sanitize_indicator_text(str(display_name or '').strip())
    if not disp:
        raise ValueError('业务线显示名称不能为空')

    lib_dir = Path(library_dir) if library_dir else DEFAULT_INDICATOR_LIBRARY_DIR
    fpath = _resolve_line_filepath(raw_key, lib_dir)
    if fpath.exists():
        raise FileExistsError(f"业务线 {raw_key!r} 已存在，请勿重复创建")

    norm_summary = _normalize_line_scoring_summary(line_scoring_summary)
    payload: dict[str, Any] = {
        'schema_version': '1.0',
        'line': raw_key,
        'display_name': disp,
        'description': sanitize_indicator_text(str(description or '').strip()),
        'review_status': 'pending_human_review',
        'reviewed_at': None,
        'reviewed_by': None,
        'content_hash': compute_library_content_hash([]),
        'line_scoring_summary': norm_summary,
        'entries': [],
    }
    _atomic_write_json(fpath, payload)
    _BIZ_LINE_LABELS[raw_key] = disp
    return _format_line_record(payload)


def _contains_raw_bid_money(raw_obj: Any) -> bool:
    if isinstance(raw_obj, str):
        return bool(_BID_MONEY_RE.search(raw_obj) or _PRICE_UNIT_LINE_HINT_RE.search(raw_obj))
    if isinstance(raw_obj, dict):
        return any(_contains_raw_bid_money(v) for v in raw_obj.values())
    if isinstance(raw_obj, (list, tuple)):
        return any(_contains_raw_bid_money(v) for v in raw_obj)
    return False


def upsert_indicator_entry(
    line_key: Optional[str] = None,
    entry_payload: Optional[dict[str, Any]] = None,
    *,
    line: Optional[str] = None,
    indicator_id: Optional[str] = None,
    library_dir: Optional[str | Path] = None,
) -> dict[str, Any]:
    """E45 §2.1：新增或更新单条指标标准应答（强制执行 `_BID_MONEY_RE` 防造假锁与原子写回，禁止绕过）。"""
    eff_key = line_key if line_key is not None else line
    clean_line = validate_indicator_line_key(str(eff_key or ''))
    lib_dir = Path(library_dir) if library_dir else DEFAULT_INDICATOR_LIBRARY_DIR
    fpath = _resolve_line_filepath(clean_line, lib_dir)
    payload = _load_single_line_payload(clean_line, lib_dir)
    if not payload or not fpath.is_file():
        raise FileNotFoundError(f"未找到业务线指标库：{clean_line}")

    if not isinstance(entry_payload, dict):
        raise ValueError('指标条目载荷必须为对象')

    merged_payload = {**entry_payload, 'line': clean_line}
    if indicator_id and str(indicator_id).strip():
        merged_payload['indicator_id'] = str(indicator_id).strip()

    had_raw_money = _contains_raw_bid_money(merged_payload)
    norm_entry = _normalize_entry(merged_payload, fallback_line=clean_line)
    if norm_entry is None:
        raise ValueError('指标名称（indicator_name）与标准应答正文（standard_response）均不能为空')

    # 防造假硬锁二次校验：确保落盘字段绝不残留任何裸人民币报价数字
    if _BID_MONEY_RE.search(norm_entry['standard_response']) or _PRICE_UNIT_LINE_HINT_RE.search(norm_entry['standard_response']):
        norm_entry['standard_response'] = sanitize_indicator_text(norm_entry['standard_response'])
    if had_raw_money:
        slot_note = '[待人工填报：具体商务报价金额（系统已自动拦截裸报价数字并转为槽位）]'
        if not any('报价' in s or '金额' in s or '单价' in s or '待人工填报' in s for s in norm_entry['param_slots']):
            norm_entry['param_slots'].append(slot_note)

    entries: list[dict[str, Any]] = list(payload.get('entries') or [])
    target_id = norm_entry['indicator_id']
    replaced = False
    for idx, existing in enumerate(entries):
        if existing.get('indicator_id') == target_id:
            entries[idx] = norm_entry
            replaced = True
            break
    if not replaced:
        entries.append(norm_entry)

    old_hash = str(payload.get('content_hash') or '')
    new_hash = compute_library_content_hash(entries)
    payload['entries'] = entries
    payload['content_hash'] = new_hash
    if payload.get('review_status') in ('approved', 'reviewed') and new_hash != old_hash:
        payload['review_status'] = 'pending_human_review'
        payload['reviewed_at'] = None
        payload['reviewed_by'] = None

    _atomic_write_json(fpath, payload)
    return {
        'line': clean_line,
        'entry': norm_entry,
        'created': not replaced,
        'money_sanitized': had_raw_money,
        'review_status': payload['review_status'],
        'line_review_status': payload['review_status'],
        'pending_slot_count': sum(count_pending_slots_in_entry(e) for e in entries),
        'content_hash': new_hash,
    }


def delete_indicator_entry(
    line_key: Optional[str] = None,
    indicator_id: str = '',
    *,
    line: Optional[str] = None,
    library_dir: Optional[str | Path] = None,
) -> dict[str, Any]:
    """E45 §2.1：从指定业务线删除单条指标并重算哈希。"""
    eff_key = line_key if line_key is not None else line
    clean_line = validate_indicator_line_key(str(eff_key or ''))
    clean_id = str(indicator_id or '').strip()
    if not clean_id:
        raise ValueError('indicator_id 不能为空')

    lib_dir = Path(library_dir) if library_dir else DEFAULT_INDICATOR_LIBRARY_DIR
    fpath = _resolve_line_filepath(clean_line, lib_dir)
    payload = _load_single_line_payload(clean_line, lib_dir)
    if not payload or not fpath.is_file():
        raise FileNotFoundError(f"未找到业务线指标库：{clean_line}")

    entries: list[dict[str, Any]] = list(payload.get('entries') or [])
    new_entries = [e for e in entries if e.get('indicator_id') != clean_id]
    if len(new_entries) == len(entries):
        raise KeyError(f"未在业务线 {clean_line} 中找到指标 {clean_id}")

    new_hash = compute_library_content_hash(new_entries)
    payload['entries'] = new_entries
    payload['content_hash'] = new_hash
    if payload.get('review_status') in ('approved', 'reviewed'):
        payload['review_status'] = 'pending_human_review'
        payload['reviewed_at'] = None
        payload['reviewed_by'] = None

    _atomic_write_json(fpath, payload)
    return {
        'deleted_id': clean_id,
        'line': clean_line,
        'remaining_count': len(new_entries),
        'review_status': payload['review_status'],
        'line_review_status': payload['review_status'],
        'content_hash': new_hash,
    }


def approve_indicator_line(
    line_key: Optional[str] = None,
    reviewed_by: str = 'admin',
    *,
    line: Optional[str] = None,
    review_note: Optional[str] = None,
    library_dir: Optional[str | Path] = None,
) -> dict[str, Any]:
    """E45 §2.1：管理员对指定业务线签署「人工终审通过（`approved`）」，写回审核人与 UTC 时间戳。"""
    eff_key = line_key if line_key is not None else line
    clean_line = validate_indicator_line_key(str(eff_key or ''))
    lib_dir = Path(library_dir) if library_dir else DEFAULT_INDICATOR_LIBRARY_DIR
    fpath = _resolve_line_filepath(clean_line, lib_dir)
    payload = _load_single_line_payload(clean_line, lib_dir)
    if not payload or not fpath.is_file():
        raise FileNotFoundError(f"未找到业务线指标库：{clean_line}")

    entries: list[dict[str, Any]] = []
    for raw_e in payload.get('entries') or []:
        ne = _normalize_entry(raw_e, fallback_line=clean_line)
        if ne is not None:
            if _BID_MONEY_RE.search(ne['standard_response']) or _PRICE_UNIT_LINE_HINT_RE.search(ne['standard_response']):
                ne['standard_response'] = sanitize_indicator_text(ne['standard_response'])
            entries.append(ne)

    reviewer = str(reviewed_by or 'admin').strip() or 'admin'
    if review_note and str(review_note).strip():
        payload['review_note'] = sanitize_indicator_text(str(review_note).strip())
    now_iso = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    payload['entries'] = entries
    payload['review_status'] = 'approved'
    payload['reviewed_by'] = reviewer
    payload['reviewed_at'] = now_iso
    payload['content_hash'] = compute_library_content_hash(entries)
    _atomic_write_json(fpath, payload)
    return _format_line_record(payload)


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
            line_bonus = 0.15 if (self.biz_line not in ('general', 'common') and entry['line'] == self.biz_line) else 0.0
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
