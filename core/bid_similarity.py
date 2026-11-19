# -*- coding: utf-8 -*-
"""E36 · 多标书交叉比对与相似度风险分析引擎（0-Token 确定性 5 维排查）。

合规边界铁律：
    本模块仅对 2~6 份投标文件执行客观文本/元数据/图片指纹/实体/报价关联比对，
    输出证据明细与双栏差异对比，严禁提供任何自动改写洗稿或伪造文件指纹功能。

五大确定性维度：
    1. 文件底层元数据与内嵌图片 SHA256 碰撞（docProps core/app.xml / PDF metadata / media hash）；
    2. 剥离《招标文件》公共条款及制式套话后的“净正文 8-gram Jaccard 相似度”与 ≥25 字连续雷同长句定位；
    3. 非标“指纹错误”与外部第三方串名/项目编号碰撞；
    4. 跨标书关键人员/手机号/身份证号/合同编号碰撞（手机号与身份证号强制前 3 后 4 位脱敏）；
    5. 报价规律性关联沙盒（100% Decimal 禁 float：对齐分项恒定比值/等差间距或总价等差数列）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import itertools
import json
from pathlib import Path
import re
from typing import Any, Callable, Optional
import zipfile

from docx import Document
from docx.shared import Pt, RGBColor
import lxml.etree as ET
import pymupdf

from core.parser import parse_document
from core.price_extractor import extract_price_data
from core.quote_verifier import normalize_zh, verify_quote
from core.response_checker import build_unified_page_context, resolve_unified_page

__all__ = [
    'extract_file_fingerprint',
    'analyze_bid_similarity',
    'export_similarity_report_docx',
]

# 常见通用系统用户名/工具名（命中作者碰撞时降级 warning，真实个人名/专有账号报 error）
_GENERIC_AUTHOR_NAMES = frozenset({
    'admin', 'administrator', 'user', 'users', 'owner', 'default', 'system',
    'lenovo', 'huawei', 'dell', 'hp', 'asus', 'acer', 'apple', 'mac',
    'microsoft', 'wps', 'wps office', 'kingsoft', 'office', 'windows',
})
# 生成库固定占位作者名（如 python-docx 未传 author 时默认写死的占位值，不作为同源证据）
_LIBRARY_DEFAULT_AUTHORS = frozenset({'python-docx'})
_MIN_VALID_META_YEAR = 2020

# 标书通用制式套话片段（即使未传入招标书，也剥离极常见格式抬头减少噪声）
_COMMON_BOILERPLATE_PHRASES = (
    '投标人名称盖章',
    '法定代表人或其委托代理人签字',
    '法定代表人或委托代理人',
    '开标一览表',
    '投标函',
    '授权委托书',
    '资格审查资料',
    '技术偏差表',
    '商务偏差表',
    '投标文件正本',
    '投标文件副本',
)

# 维度 3/4 实体与异常片段正则
_MOBILE_RE = re.compile(r'(?<!\d)(1[3-9]\d{9})(?!\d)')
_ID_CARD_RE = re.compile(
    r'(?<![0-9X])([1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?![0-9Xx])'
)
_PERSON_ROLE_RE = re.compile(
    r'(?:项目经理|项目负责人|技术负责人|授权委托人|法定代表人)\s*[：:]\s*([\u4e00-\u9fa5]{2,4})(?=$|[\s，。,；;（(])'
)
_CONTRACT_NO_RE = re.compile(
    r'(?:合同编号|协议编号|项目编号|备案号)\s*[：:]\s*([A-Za-z0-9\-_/]{6,32})'
)
_EXTERNAL_UNIT_RE = re.compile(
    r'([\u4e00-\u9fa5]{2,18}(?:市|省|县|区)?[\u4e00-\u9fa5]{2,14}(?:大数据局|政务云中心|管理局|信息中心|委员会|医院|学院|大学|银行|有限公司|集团))'
)
_EXTERNAL_PROJ_CODE_RE = re.compile(r'\b([A-Z]{2,8}-\d{4}-[A-Z0-9\-]{2,20})\b')
_MOJIBAKE_ERR_RE = re.compile(
    r'([^\n。；;]{4,24}(?:锟斤拷|烫烫烫|\ufffd{2,}|\?\?\?[\u4e00-\u9fa5]|[\u4e00-\u9fa5]{2}\s+[\u4e00-\u9fa5]{2}\s+[\u4e00-\u9fa5]{2})[^\n。；;]{0,16})'
)

# 单字符/局部归一映射（与 core.quote_verifier.normalize_zh 保持完全一致语义）
_ZH_PUNCT_MAP = str.maketrans({
    '“': '"', '”': '"',
    '‘': "'", '’': "'",
    '，': ',',
    '。': '.',
    '；': ';',
    '？': '?',
    '！': '!',
    '…': '.',
    '·': '.',
})


def _mask_mobile(raw: str) -> str:
    s = raw.strip()
    if len(s) < 7:
        return '***'
    return f"{s[:3]}****{s[-4:]}"


def _mask_id_card(raw: str) -> str:
    s = raw.strip().upper()
    if len(s) != 18:
        return '***'
    return f"{s[:3]}{'*' * 11}{s[-4:]}"


def _mask_sensitive_text(text: str) -> str:
    """E36 合规与隐私铁律：对摘录引用文本中的 18 位身份证号与 11 位手机号强制执行前 3 后 4 位脱敏。"""
    if not text:
        return ''
    masked = _ID_CARD_RE.sub(lambda m: _mask_id_card(m.group(1)), text)
    masked = _MOBILE_RE.sub(lambda m: _mask_mobile(m.group(1)), masked)
    return masked


def _sha256_tag(raw: str) -> str:
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def _normalize_with_pos_map(raw_text: str) -> tuple[str, list[int]]:
    """逐字符复刻 core.quote_verifier.normalize_zh，并记录每个归一字符在原文的偏移索引。"""
    if not raw_text:
        return '', []
    out_chars: list[str] = []
    pos_map: list[int] = []
    n = len(raw_text)
    i = 0
    while i < n:
        ch = raw_text[i]
        if ch.isspace() or ch == '\u3000':
            i += 1
            continue
        if ch == '—' and i + 1 < n and raw_text[i + 1] == '—':
            out_chars.append('-')
            pos_map.append(i)
            i += 2
            continue
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            ch_norm = chr(code - 0xFEE0)
        else:
            ch_norm = ch
        ch_norm = ch_norm.translate(_ZH_PUNCT_MAP)
        out_chars.append(ch_norm)
        pos_map.append(i)
        i += 1
    return ''.join(out_chars), pos_map


def _parse_meta_datetime(raw: Optional[str]) -> Optional[datetime]:
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    # PDF 格式 D:YYYYMMDDHHmmSS...
    m_pdf = re.match(r'^D:(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})', s)
    if m_pdf:
        try:
            return datetime(*[int(x) for x in m_pdf.groups()], tzinfo=timezone.utc)
        except ValueError:
            return None
    # ISO 8601
    try:
        iso_s = s.replace('Z', '+00:00')
        dt = datetime.fromisoformat(iso_s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _xml_child_text(root: Optional[ET._Element], local_name: str) -> Optional[str]:
    if root is None:
        return None
    for el in root.iter():
        tag = el.tag.split('}')[-1] if isinstance(el.tag, str) else ''
        if tag == local_name and el.text and el.text.strip():
            return el.text.strip()
    return None


def extract_file_fingerprint(file_path: str | Path) -> dict[str, Any]:
    """提取 .docx / .pdf / .txt 底层元数据指纹与内嵌图片二进制 SHA256 集合。"""
    p = Path(file_path)
    ext = p.suffix.lower()
    metadata: dict[str, Any] = {
        'author': None,
        'last_modified_by': None,
        'creator': None,
        'producer': None,
        'company': None,
        'application': None,
        'created': None,
        'modified': None,
        'revision': None,
    }
    media_hashes: list[str] = []
    seen_hashes: set[str] = set()

    if ext == '.docx' and p.is_file():
        try:
            with zipfile.ZipFile(p, 'r') as zf:
                names = set(zf.namelist())
                if 'docProps/core.xml' in names:
                    try:
                        core_root = ET.fromstring(zf.read('docProps/core.xml'))
                        metadata['author'] = _xml_child_text(core_root, 'creator')
                        metadata['last_modified_by'] = _xml_child_text(core_root, 'lastModifiedBy')
                        metadata['created'] = _xml_child_text(core_root, 'created')
                        metadata['modified'] = _xml_child_text(core_root, 'modified')
                        metadata['revision'] = _xml_child_text(core_root, 'revision')
                    except Exception:
                        pass
                if 'docProps/app.xml' in names:
                    try:
                        app_root = ET.fromstring(zf.read('docProps/app.xml'))
                        metadata['company'] = _xml_child_text(app_root, 'Company')
                        metadata['application'] = _xml_child_text(app_root, 'Application')
                    except Exception:
                        pass
                for name in sorted(names):
                    if name.startswith('word/media/') and not name.endswith('/'):
                        try:
                            blob = zf.read(name)
                            if len(blob) >= 64:
                                digest = hashlib.sha256(blob).hexdigest()
                                if digest not in seen_hashes:
                                    seen_hashes.add(digest)
                                    media_hashes.append(digest)
                        except Exception:
                            continue
        except Exception:
            pass
    elif ext == '.pdf' and p.is_file():
        try:
            doc = pymupdf.open(str(p))
            try:
                raw_meta = doc.metadata or {}
                metadata['author'] = (raw_meta.get('author') or '').strip() or None
                metadata['creator'] = (raw_meta.get('creator') or '').strip() or None
                metadata['producer'] = (raw_meta.get('producer') or '').strip() or None
                metadata['created'] = (raw_meta.get('creationDate') or '').strip() or None
                metadata['modified'] = (raw_meta.get('modDate') or '').strip() or None

                seen_xrefs: set[int] = set()
                for page in doc:
                    for img_info in page.get_images(full=True):
                        xref = int(img_info[0])
                        if xref in seen_xrefs:
                            continue
                        seen_xrefs.add(xref)
                        try:
                            img_dict = doc.extract_image(xref)
                            blob = img_dict.get('image') if img_dict else None
                            if isinstance(blob, (bytes, bytearray)) and len(blob) >= 64:
                                digest = hashlib.sha256(blob).hexdigest()
                                if digest not in seen_hashes:
                                    seen_hashes.add(digest)
                                    media_hashes.append(digest)
                        except Exception:
                            continue
            finally:
                doc.close()
        except Exception:
            pass

    return {
        'metadata': metadata,
        'media_hashes': media_hashes,
        'media_count': len(media_hashes),
    }


def _build_shingle_set(norm_text: str, k: int) -> set[str]:
    if len(norm_text) < k:
        return {norm_text} if len(norm_text) >= 4 else set()
    return {norm_text[i:i + k] for i in range(len(norm_text) - k + 1)}


def _compute_net_norm_and_map(
    norm_text: str,
    pos_map: list[int],
    tender_shingles: set[str],
    tender_norm: str = '',
) -> tuple[str, list[int]]:
    """从投标书归一化文本中剥离招标公共片段及制式套话，保留净自编字符与原文偏移映射。"""
    n = len(norm_text)
    if n == 0:
        return '', []

    masked = [False] * n
    # 1. 剥离命中的招标书 12-gram 公共切片（≥12 连续字符匹配招标条款即整体掩码）
    if tender_shingles and n >= 12:
        k = 12
        for i in range(n - k + 1):
            if norm_text[i:i + k] in tender_shingles:
                for j in range(i, i + k):
                    masked[j] = True
    elif tender_norm and 6 <= n <= 100 and norm_text in tender_norm:
        return '', []

    # 2. 剥离极常见的标书制式套话短语
    for phrase in _COMMON_BOILERPLATE_PHRASES:
        p_norm = normalize_zh(phrase)
        if not p_norm:
            continue
        start_search = 0
        while True:
            idx = norm_text.find(p_norm, start_search)
            if idx < 0:
                break
            for j in range(idx, idx + len(p_norm)):
                masked[j] = True
            start_search = idx + len(p_norm)

    net_chars: list[str] = []
    net_map: list[int] = []
    for i, ch in enumerate(norm_text):
        if not masked[i]:
            net_chars.append(ch)
            net_map.append(pos_map[i])
    return ''.join(net_chars), net_map


def _jaccard_pct(set_a: set[str], set_b: set[str]) -> Decimal:
    if not set_a or not set_b:
        return Decimal('0.00')
    union_len = len(set_a | set_b)
    if union_len == 0:
        return Decimal('0.00')
    inter_len = len(set_a & set_b)
    ratio = (Decimal(inter_len) * Decimal('100')) / Decimal(union_len)
    return ratio.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _find_shared_passages(
    raw_a: str,
    net_norm_a: str,
    net_map_a: list[int],
    page_ctx_a: Any,
    raw_b: str,
    net_norm_b: str,
    net_map_b: list[int],
    page_ctx_b: Any,
    *,
    min_chars: int = 25,
    max_passages: int = 20,
) -> list[dict[str, Any]]:
    """精准抽取剔除招标公共文本后仍连续一致 ≥25 字的原文句段并溯源双端偏移及页码。"""
    anchor_len = 15
    if len(net_norm_a) < min_chars or len(net_norm_b) < min_chars:
        return []

    index_b: dict[str, list[int]] = {}
    for idx in range(0, len(net_norm_b) - anchor_len + 1):
        gram = net_norm_b[idx:idx + anchor_len]
        bucket = index_b.setdefault(gram, [])
        if len(bucket) < 16:
            bucket.append(idx)

    raw_spans: list[tuple[int, int, int, int]] = []
    covered_a: list[tuple[int, int]] = []
    i = 0
    limit_a = len(net_norm_a) - anchor_len + 1
    while i < limit_a:
        gram = net_norm_a[i:i + anchor_len]
        positions_b = index_b.get(gram)
        if not positions_b:
            i += 1
            continue

        best_span: Optional[tuple[int, int, int, int]] = None
        for b_start in positions_b:
            la = i
            lb = b_start
            while la > 0 and lb > 0 and net_norm_a[la - 1] == net_norm_b[lb - 1]:
                la -= 1
                lb -= 1
            ra = i + anchor_len
            rb = b_start + anchor_len
            while ra < len(net_norm_a) and rb < len(net_norm_b) and net_norm_a[ra] == net_norm_b[rb]:
                ra += 1
                rb += 1
            if ra - la >= min_chars:
                if best_span is None or (ra - la) > (best_span[1] - best_span[0]):
                    best_span = (la, ra, lb, rb)

        if best_span is not None:
            la, ra, lb, rb = best_span
            overlapped = any(not (ra <= ca or la >= cb) for ca, cb in covered_a)
            if not overlapped:
                raw_spans.append((la, ra, lb, rb))
                covered_a.append((la, ra))
            i = max(i + 1, ra)
        else:
            i += 1

    raw_spans.sort(key=lambda sp: sp[1] - sp[0], reverse=True)
    passages: list[dict[str, Any]] = []
    for la, ra, lb, rb in raw_spans[:max_passages]:
        orig_sa = net_map_a[la]
        orig_ea = net_map_a[ra - 1] + 1
        orig_sb = net_map_b[lb]
        orig_eb = net_map_b[rb - 1] + 1

        quote_a = _mask_sensitive_text(raw_a[orig_sa:orig_ea].strip())
        quote_b = _mask_sensitive_text(raw_b[orig_sb:orig_eb].strip())
        page_a = resolve_unified_page(orig_sa, page_ctx_a)[0] if page_ctx_a else None
        page_b = resolve_unified_page(orig_sb, page_ctx_b)[0] if page_ctx_b else None
        passages.append({
            'norm_chars': ra - la,
            'a_quote': quote_a[:400],
            'a_offset': orig_sa,
            'a_page': page_a,
            'b_quote': quote_b[:400],
            'b_offset': orig_sb,
            'b_page': page_b,
        })
    return passages


def _extract_entities_and_fingerprints(raw_text: str, tender_text: str = '') -> dict[str, Any]:
    """提取非招标原文内的实体（隐私号仅存 hash 键 + 前3后4掩码）与异常指纹片段。"""
    mobiles: dict[str, str] = {}
    for m in _MOBILE_RE.finditer(raw_text):
        num = m.group(1)
        if tender_text and num in tender_text:
            continue
        mobiles[_sha256_tag(num)] = _mask_mobile(num)

    id_cards: dict[str, str] = {}
    for m in _ID_CARD_RE.finditer(raw_text):
        cid = m.group(1).upper()
        if tender_text and cid in tender_text.upper():
            continue
        id_cards[_sha256_tag(cid)] = _mask_id_card(cid)

    persons: dict[str, str] = {}
    for m in _PERSON_ROLE_RE.finditer(raw_text):
        pname = m.group(1).strip()
        if pname and (not tender_text or pname not in tender_text):
            persons[pname] = pname

    contracts: dict[str, str] = {}
    for m in _CONTRACT_NO_RE.finditer(raw_text):
        cno = m.group(1).strip().upper()
        if len(cno) >= 6 and (not tender_text or cno not in tender_text.upper()):
            contracts[cno] = m.group(1).strip()

    external_refs: set[str] = set()
    tender_norm = normalize_zh(tender_text) if tender_text else ''
    for m in _EXTERNAL_UNIT_RE.finditer(raw_text):
        unit = m.group(1).strip()
        if len(unit) >= 5 and normalize_zh(unit) not in tender_norm:
            external_refs.add(unit)
    for m in _EXTERNAL_PROJ_CODE_RE.finditer(raw_text):
        code = m.group(1).strip()
        if not tender_text or code not in tender_text:
            external_refs.add(code)

    mojibake_snippets: set[str] = set()
    for m in _MOJIBAKE_ERR_RE.finditer(raw_text):
        snip = _mask_sensitive_text(m.group(1).strip())
        if len(snip) >= 6 and normalize_zh(snip) not in tender_norm:
            mojibake_snippets.add(snip)

    return {
        'mobiles': mobiles,
        'id_cards': id_cards,
        'persons': persons,
        'contracts': contracts,
        'external_refs': external_refs,
        'mojibake_snippets': mojibake_snippets,
    }


def _to_decimal(val: Any) -> Optional[Decimal]:
    if val is None or isinstance(val, bool):
        return None
    try:
        cleaned = str(val).replace(',', '').replace('¥', '').replace('￥', '').strip()
        if not cleaned:
            return None
        d = Decimal(cleaned)
        if not d.is_finite():
            return None
        return d
    except (InvalidOperation, ValueError):
        return None


def _compare_price_regularity(
    price_a: dict[str, Any],
    price_b: dict[str, Any],
) -> list[dict[str, Any]]:
    """使用 Decimal 精算比对两家对齐分项单价/合价的比例与固定差额规律（禁 float）。"""
    findings: list[dict[str, Any]] = []
    items_a = list(price_a.get('items') or [])
    items_b = list(price_b.get('items') or [])

    map_a: dict[str, Decimal] = {}
    for it in items_a:
        nm = normalize_zh(str(it.get('name') or ''))
        val = _to_decimal(it.get('unit_price')) or _to_decimal(it.get('total_price'))
        if nm and val is not None and val > Decimal('0'):
            map_a.setdefault(nm, val)

    aligned_pairs: list[tuple[str, Decimal, Decimal]] = []
    for it in items_b:
        nm = normalize_zh(str(it.get('name') or ''))
        val_b = _to_decimal(it.get('unit_price')) or _to_decimal(it.get('total_price'))
        if nm in map_a and val_b is not None and val_b > Decimal('0'):
            aligned_pairs.append((str(it.get('name') or nm), map_a[nm], val_b))

    # 若分项名称有微调但条目数≥4且一致，按顺序兜底对齐检验
    if len(aligned_pairs) < 4 and len(items_a) >= 4 and len(items_a) == len(items_b):
        seq_pairs: list[tuple[str, Decimal, Decimal]] = []
        for idx, (ia, ib) in enumerate(zip(items_a, items_b), 1):
            va = _to_decimal(ia.get('unit_price')) or _to_decimal(ia.get('total_price'))
            vb = _to_decimal(ib.get('unit_price')) or _to_decimal(ib.get('total_price'))
            if va is not None and vb is not None and va > Decimal('0') and vb > Decimal('0'):
                seq_pairs.append((str(ia.get('name') or f"分项#{idx}"), va, vb))
        if len(seq_pairs) >= 4:
            aligned_pairs = seq_pairs

    if len(aligned_pairs) >= 4:
        ratios = [vb / va for _, va, vb in aligned_pairs]
        diffs = [vb - va for _, va, vb in aligned_pairs]
        ratio_spread = max(ratios) - min(ratios)
        diff_spread = max(diffs) - min(diffs)

        if ratio_spread < Decimal('0.001'):
            avg_ratio = (sum(ratios, Decimal('0')) / Decimal(len(ratios))).quantize(
                Decimal('0.0001'), rounding=ROUND_HALF_UP
            )
            findings.append({
                'code': 'REGULAR_PRICE_RATIO',
                'severity': 'error',
                'dimension': 'price_regularity',
                'message': (
                    f"检测到 {len(aligned_pairs)} 项分项报价保持高度一致的比例关系"
                    f"（比值 ≈ {avg_ratio}，波动 < 0.001）"
                ),
                'detail': {
                    'aligned_count': len(aligned_pairs),
                    'ratio': str(avg_ratio),
                    'sample_items': [
                        {'name': nm, 'a_price': str(va), 'b_price': str(vb)}
                        for nm, va, vb in aligned_pairs[:5]
                    ],
                },
            })
        elif diff_spread <= Decimal('0.01') and abs(diffs[0]) > Decimal('0.00'):
            fixed_diff = diffs[0].quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            findings.append({
                'code': 'REGULAR_PRICE_DIFF',
                'severity': 'error',
                'dimension': 'price_regularity',
                'message': (
                    f"检测到 {len(aligned_pairs)} 项分项报价保持固定差额规律"
                    f"（差额 = {fixed_diff} 元）"
                ),
                'detail': {
                    'aligned_count': len(aligned_pairs),
                    'fixed_diff': str(fixed_diff),
                    'sample_items': [
                        {'name': nm, 'a_price': str(va), 'b_price': str(vb)}
                        for nm, va, vb in aligned_pairs[:5]
                    ],
                },
            })

    return findings


def _check_metadata_pair_collisions(
    fp_a: dict[str, Any],
    fp_b: dict[str, Any],
    label_a: str,
    label_b: str,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    meta_a = fp_a.get('metadata') or {}
    meta_b = fp_b.get('metadata') or {}

    author_fields = (
        ('author', '文档作者 (author)'),
        ('last_modified_by', '最后修改人 (lastModifiedBy)'),
        ('company', '创建单位属性 (Company)'),
    )
    matched_fields: list[tuple[str, str, str]] = []
    for key, zh_label in author_fields:
        va = str(meta_a.get(key) or '').strip()
        vb = str(meta_b.get(key) or '').strip()
        if va and vb and va.lower() == vb.lower() and va.lower() not in _LIBRARY_DEFAULT_AUTHORS:
            matched_fields.append((key, zh_label, va))

    if matched_fields:
        only_generic = all(val.lower() in _GENERIC_AUTHOR_NAMES for _, _, val in matched_fields)
        sev = 'warning' if only_generic else 'error'
        desc = '、'.join(f"{zl}={val!r}" for _, zl, val in matched_fields)
        findings.append({
            'code': 'SAME_DOC_AUTHOR',
            'severity': sev,
            'dimension': 'metadata_fingerprint',
            'a_label': label_a,
            'b_label': label_b,
            'message': f"「{label_a}」与「{label_b}」底层文档属性高度一致：{desc}",
            'detail': {
                'matched_attributes': [
                    {'field': k, 'label': zl, 'value': v}
                    for k, zl, v in matched_fields
                ],
                'is_generic_account': only_generic,
            },
        })

    # 修改/创建时间窗口碰撞（同一分钟内，跳过 python-docx 等模板固定历史默认时间如 2013-12-23）
    for time_key, time_zh in (('modified', '最后保存时间'), ('created', '文档创建时间')):
        dt_a = _parse_meta_datetime(meta_a.get(time_key))
        dt_b = _parse_meta_datetime(meta_b.get(time_key))
        if dt_a and dt_b and dt_a.year >= _MIN_VALID_META_YEAR and dt_b.year >= _MIN_VALID_META_YEAR:
            delta_s = abs((dt_a - dt_b).total_seconds())
            if delta_s <= 60.0:
                findings.append({
                    'code': 'SAME_TIME_WINDOW',
                    'severity': 'warning',
                    'dimension': 'metadata_fingerprint',
                    'a_label': label_a,
                    'b_label': label_b,
                    'message': (
                        f"「{label_a}」与「{label_b}」{time_zh}仅相隔 {int(delta_s)} 秒"
                        f"（均在 1 分钟窗口内生成/保存）"
                    ),
                    'detail': {
                        'field': time_key,
                        'a_time': dt_a.isoformat(),
                        'b_time': dt_b.isoformat(),
                        'delta_seconds': int(delta_s),
                    },
                })
                break

    # 内嵌图片二进制 SHA256 交集
    hashes_a = set(fp_a.get('media_hashes') or [])
    hashes_b = set(fp_b.get('media_hashes') or [])
    shared_hashes = sorted(hashes_a & hashes_b)
    if shared_hashes:
        findings.append({
            'code': 'SAME_EMBEDDED_IMAGE',
            'severity': 'error',
            'dimension': 'metadata_fingerprint',
            'a_label': label_a,
            'b_label': label_b,
            'message': (
                f"「{label_a}」与「{label_b}」包含 {len(shared_hashes)} 张字节流完全相同的内嵌图片"
                f"（SHA256 碰撞）"
            ),
            'detail': {
                'shared_count': len(shared_hashes),
                'hash_prefixes': [h[:16] for h in shared_hashes[:10]],
            },
        })

    return findings


def _extract_net_sample_excerpt(raw_text: str, net_norm: str, net_map: list[int], max_chars: int = 1600) -> str:
    """从净自编文本对应的原文窗口抽取可读样本段落（屏蔽个人手机与身份证号），供 AI 深层同构对比。"""
    if not raw_text:
        return ''
    if net_map:
        start_idx = net_map[0]
        end_idx = min(len(raw_text), net_map[min(len(net_map) - 1, max_chars)] + 1)
        sample = raw_text[start_idx:end_idx]
    else:
        sample = raw_text[:max_chars]
    # 若前段偏短，适当向后抽样中间部分
    if len(sample.strip()) < 200 and len(raw_text) > 400:
        mid = len(raw_text) // 3
        sample = raw_text[mid:mid + max_chars]
    return _mask_sensitive_text(sample[:max_chars])


def _parse_similarity_llm_json(raw: str) -> Optional[dict[str, Any]]:
    text = (raw or '').strip()
    if not text:
        return None
    fence = re.search(r'```(?:json)?\s*([\s\S]*?)```', text, flags=re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    for lo, hi in ((0, len(text)), (text.find('{'), text.rfind('}') + 1)):
        if lo < 0 or hi <= lo:
            continue
        try:
            obj = json.loads(text[lo:hi])
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


def run_similarity_ai_review(
    parsed_bids: list[dict[str, Any]],
    pair_matrix: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    client: Any,
    *,
    cancel_check: Optional[Callable[[], None]] = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """E38：可选 AI 语义同构深层核查与误报排查综述（严格客观审查，严禁输出洗稿或规避建议）。"""
    if client is None or len(parsed_bids) < 2 or not pair_matrix:
        return {
            'status': 'skipped',
            'summary': '未配置模型网关或未启用 AI 语义复核，本次报告仅基于五维确定性引擎结果生成。',
            'semantic_matches': [],
            'false_positive_notes': [],
        }, []

    if cancel_check is not None:
        cancel_check()

    bid_by_idx = {b['index']: b for b in parsed_bids}
    # 选取净相似度最高及已有告警的前 3 组做局部窗口语义研判
    pair_scores: list[tuple[float, dict[str, Any]]] = []
    for pr in pair_matrix:
        finding_bonus = sum(
            10.0 for f in findings
            if {f.get('a_label'), f.get('b_label')} == {pr['a_label'], pr['b_label']}
        )
        pair_scores.append((float(pr.get('net_similarity_pct') or 0.0) + finding_bonus, pr))
    pair_scores.sort(key=lambda x: x[0], reverse=True)
    top_pairs = [pr for _, pr in pair_scores[:3]]

    pair_blocks: list[str] = []
    for pr in top_pairs:
        ba = bid_by_idx.get(pr['a_index'])
        bb = bid_by_idx.get(pr['b_index'])
        if not ba or not bb:
            continue
        sa = _extract_net_sample_excerpt(ba['raw_text'], ba['net_norm'], ba['net_map'], max_chars=1200)
        sb = _extract_net_sample_excerpt(bb['raw_text'], bb['net_norm'], bb['net_map'], max_chars=1200)
        pair_blocks.append(
            f"### 比对组：{pr['a_label']} vs {pr['b_label']}（净相似度 {pr['net_similarity_pct']}%）\n"
            f"[{pr['a_label']} 样本]：\n{sa}\n"
            f"[{pr['b_label']} 样本]：\n{sb}"
        )

    finding_digest = '\n'.join(
        f"- [{f.get('severity')}] {f.get('code')} ({f.get('a_label')}↔{f.get('b_label')}): {_mask_sensitive_text(str(f.get('message') or '')[:120])}"
        for f in findings[:15]
    ) or '（确定性引擎未命中明显高危碰撞）'

    prompt = (
        "你是招投标合规审查辅助专家。请基于下方【多标书比对统计与文本窗口】执行严格客观的语义复核与误报排查：\n"
        "【严守红线】：仅给出客观交叉排查结论与误报澄清说明；**严禁提供任何洗稿改写文本、消重方法或规避清标审查的建议**。\n"
        f"【规则初筛命中摘要】：\n{finding_digest}\n\n"
        f"【待复核局部文本样本】：\n" + '\n\n'.join(pair_blocks) + "\n\n"
        "请返回纯 JSON：\n"
        '{"overall_summary": "180字内客观核查综述与人工复核优先级建议", '
        '"semantic_matches": [{"a_label": "单位A", "b_label": "单位B", "similarity_type": "semantic_isomorphism|industry_boilerplate", "reason": "判定理由", "a_quote": "来自A原文连续子串", "b_quote": "来自B原文连续子串"}], '
        '"false_positive_notes": ["若发现明显行业公文/国标导致的误报说明"]}'
    )

    try:
        resp = client.chat([{'role': 'user', 'content': prompt}], model_tier='flash')
        parsed = _parse_similarity_llm_json(str(getattr(resp, 'content', '') or ''))
        if not parsed:
            raise ValueError('AI 语义复核返回格式非有效 JSON')
    except Exception as exc:
        return {
            'status': 'unavailable',
            'summary': f'AI 语义深层复核未能完成（{type(exc).__name__}），保留五维确定性结果供人工研判。',
            'semantic_matches': [],
            'false_positive_notes': [],
        }, []

    bid_by_label = {b['label']: b for b in parsed_bids}
    verified_matches: list[dict[str, Any]] = []
    ai_findings: list[dict[str, Any]] = []

    for item in (parsed.get('semantic_matches') or [])[:8]:
        if not isinstance(item, dict):
            continue
        al = str(item.get('a_label') or '').strip()
        bl = str(item.get('b_label') or '').strip()
        ba = bid_by_label.get(al)
        bb = bid_by_label.get(bl)
        if not ba or not bb or al == bl:
            continue
        aq = str(item.get('a_quote') or '').strip()
        bq = str(item.get('b_quote') or '').strip()
        # 双侧原文强制 verify_quote 溯源校验，杜绝编造引用
        if not aq or not bq or not verify_quote(aq, ba['raw_text']) or not verify_quote(bq, bb['raw_text']):
            continue

        sim_type = str(item.get('similarity_type') or 'semantic_isomorphism').strip()
        if sim_type not in ('semantic_isomorphism', 'industry_boilerplate'):
            sim_type = 'semantic_isomorphism'
        reason = _mask_sensitive_text(str(item.get('reason') or '').strip())
        if re.search(r'(?:规避(?:清标|查重|检测|监管)|洗稿(?:技巧|改写|指南)|伪造(?:作者|元数据|指纹))', reason):
            continue
        aq_safe = _mask_sensitive_text(aq)
        bq_safe = _mask_sensitive_text(bq)

        verified_matches.append({
            'a_label': al,
            'b_label': bl,
            'similarity_type': sim_type,
            'reason': reason,
            'a_quote': aq_safe,
            'b_quote': bq_safe,
            'quote_verified': True,
        })

        if sim_type == 'semantic_isomorphism':
            ai_findings.append({
                'code': 'SEMANTIC_ISOMORPHISM',
                'severity': 'warning',
                'dimension': 'ai_semantic_review',
                'a_label': al,
                'b_label': bl,
                'message': f"「{al}」与「{bl}」AI 语义同构复核疑似同源逻辑：{reason}",
                'detail': {
                    'a_quote': aq_safe[:220],
                    'b_quote': bq_safe[:220],
                    'quote_verified': True,
                },
            })

    summary_txt = _mask_sensitive_text(str(parsed.get('overall_summary') or '').strip())
    if re.search(r'(?:规避(?:清标|查重|检测|监管)|洗稿(?:技巧|改写|指南)|伪造(?:作者|元数据|指纹))', summary_txt):
        summary_txt = 'AI 语义深层复核完成，请结合五维确定性证据明细与双栏摘录独立人工核查。'
    fp_notes = [
        _mask_sensitive_text(str(x or '').strip())
        for x in (parsed.get('false_positive_notes') or [])[:6]
        if str(x or '').strip()
        and not re.search(r'(?:规避(?:清标|查重|检测|监管)|洗稿(?:技巧|改写|指南)|伪造(?:作者|元数据|指纹))', str(x or ''))
    ]
    return {
        'status': 'done',
        'summary': summary_txt or 'AI 语义深层复核完成，请结合上方证据明细与双栏摘录复核。',
        'semantic_matches': verified_matches,
        'false_positive_notes': fp_notes,
    }, ai_findings


def analyze_bid_similarity(
    bid_files: list[dict[str, Any]],
    *,
    tender_path: Optional[str | Path] = None,
    enable_ai_review: bool = False,
    client: Any = None,
    progress_cb: Optional[Callable[[int, str], None]] = None,
    cancel_check: Optional[Callable[[], None]] = None,
) -> dict[str, Any]:
    """对 >=2 份参选投标文件执行五维 0-Token 交叉比对与可选 AI 深层语义复核。"""
    if not isinstance(bid_files, list) or len(bid_files) < 2:
        raise ValueError("bid_files 至少需要包含 2 份投标文件")

    def _emit(pct: int, stage: str) -> None:
        if cancel_check is not None:
            cancel_check()
        if progress_cb is not None:
            progress_cb(pct, stage)

    def _parse_doc_for_similarity(
        fpath: Path,
        *,
        pct_start: int,
        pct_span: int,
        stage_prefix: str,
    ) -> tuple[Any, bool]:
        """优先极速解析文本层（ocr_images=False），仅当正文极少（纯扫描件）时再逐页/逐图 OCR 并透传进度与中断信号。"""
        doc = parse_document(str(fpath), ocr_images=False)
        if cancel_check is not None:
            cancel_check()
        if len((doc.text or '').strip()) >= 200:
            return doc, False

        def _on_ocr_prog(done_cnt: int, total_cnt: int, cached_cnt: int) -> None:
            if total_cnt > 0:
                sub_pct = pct_start + min(max(1, pct_span - 1), int((done_cnt / max(1, total_cnt)) * max(1, pct_span)))
                _emit(sub_pct, f"{stage_prefix}（扫描件 OCR {done_cnt}/{total_cnt}，缓存 {cached_cnt}）")
            elif cancel_check is not None:
                cancel_check()

        ocr_doc = parse_document(str(fpath), ocr_images=True, progress_cb=_on_ocr_prog)
        if cancel_check is not None:
            cancel_check()
        return ocr_doc, True

    def _build_page_ctx_compat(fpath: Path, *, used_ocr: bool) -> dict[str, Any]:
        try:
            return build_unified_page_context(fpath, ocr_images=used_ocr)
        except TypeError:
            return build_unified_page_context(fpath)

    _emit(8, '阶段 1/4：解析招标公共底稿与构建过滤词片')
    t_doc = None
    tender_text = ''
    tender_norm = ''
    tender_shingles: set[str] = set()
    if tender_path is not None:
        t_doc, _t_ocr = _parse_doc_for_similarity(
            Path(tender_path),
            pct_start=8,
            pct_span=4,
            stage_prefix='阶段 1/4：解析招标公共底稿',
        )
        tender_text = t_doc.text or ''
        tender_norm = normalize_zh(tender_text)
        tender_shingles = _build_shingle_set(tender_norm, 12)

    parsed_bids: list[dict[str, Any]] = []
    total_files = len(bid_files)
    for idx, spec in enumerate(bid_files):
        base_pct = 12 + int((idx / max(1, total_files)) * 38)
        per_bid_span = max(2, int(38 / max(1, total_files)))
        _emit(
            base_pct,
            f"阶段 2/4：解析投标书元数据与净正文（{idx + 1}/{total_files}）",
        )
        fpath = Path(spec['path'])
        label = str(spec.get('label') or fpath.stem).strip() or f"标书#{idx + 1}"
        filename = str(spec.get('filename') or fpath.name)

        pdoc, used_ocr = _parse_doc_for_similarity(
            fpath,
            pct_start=base_pct,
            pct_span=max(1, per_bid_span // 2),
            stage_prefix=f"阶段 2/4：解析投标书（{idx + 1}/{total_files}）",
        )
        raw_text = pdoc.text or ''
        _emit(
            min(50, base_pct + max(1, per_bid_span // 2)),
            f"阶段 2/4：提取页码、文件指纹与分项报价（{idx + 1}/{total_files}）",
        )
        page_ctx = _build_page_ctx_compat(fpath, used_ocr=used_ocr)
        fp = extract_file_fingerprint(fpath)
        norm_full, pos_map = _normalize_with_pos_map(raw_text)
        net_norm, net_map = _compute_net_norm_and_map(
            norm_full,
            pos_map,
            tender_shingles,
            tender_norm=tender_norm,
        )
        raw_8grams = _build_shingle_set(norm_full, 8)
        net_8grams = _build_shingle_set(net_norm, 8)
        entities = _extract_entities_and_fingerprints(raw_text, tender_text=tender_text)
        price_res = extract_price_data(
            fpath,
            tender_path=tender_path,
            parsed_doc=pdoc,
            tender_parsed_doc=t_doc,
        )
        bid_total_dec = _to_decimal(price_res.get('bid_total')) or _to_decimal(price_res.get('reference_bid_total'))

        parsed_bids.append({
            'index': idx,
            'label': label,
            'filename': filename,
            'rel_path': str(spec.get('rel_path') or ''),
            'chars': len(raw_text),
            'net_chars': len(net_norm),
            'pages': int(pdoc.pages or 1),
            'raw_text': raw_text,
            'page_ctx': page_ctx,
            'fingerprint': fp,
            'net_norm': net_norm,
            'net_map': net_map,
            'raw_8grams': raw_8grams,
            'net_8grams': net_8grams,
            'entities': entities,
            'price_res': price_res,
            'bid_total': str(bid_total_dec) if bid_total_dec is not None else None,
            'bid_total_dec': bid_total_dec,
        })

    pair_matrix: list[dict[str, Any]] = []
    all_findings: list[dict[str, Any]] = []
    max_net_sim = Decimal('0.00')

    combos = list(itertools.combinations(range(len(parsed_bids)), 2))
    total_pairs = max(1, len(combos))
    for p_idx, (i, j) in enumerate(combos):
        _emit(
            52 + int((p_idx / total_pairs) * 28),
            f"阶段 3/4：五维交叉碰撞计算（{p_idx + 1}/{total_pairs} 组）",
        )
        bi = parsed_bids[i]
        bj = parsed_bids[j]
        la, lb = bi['label'], bj['label']

        net_pct = _jaccard_pct(bi['net_8grams'], bj['net_8grams'])
        raw_pct = _jaccard_pct(bi['raw_8grams'], bj['raw_8grams'])
        if net_pct > max_net_sim:
            max_net_sim = net_pct

        passages = _find_shared_passages(
            bi['raw_text'],
            bi['net_norm'],
            bi['net_map'],
            bi['page_ctx'],
            bj['raw_text'],
            bj['net_norm'],
            bj['net_map'],
            bj['page_ctx'],
            min_chars=25,
            max_passages=20,
        )

        # 维度 1：元数据 & 图片哈希
        meta_findings = _check_metadata_pair_collisions(
            bi['fingerprint'], bj['fingerprint'], la, lb
        )
        all_findings.extend(meta_findings)

        # 维度 2：净正文雷同率 & 长句碰撞
        if net_pct >= Decimal('35.00'):
            pair_risk = 'high'
            all_findings.append({
                'code': 'NET_TEXT_HIGH_SIMILARITY',
                'severity': 'error',
                'dimension': 'net_text_similarity',
                'a_label': la,
                'b_label': lb,
                'message': (
                    f"「{la}」与「{lb}」剔除公共条款后净正文相似度达 {net_pct}%"
                    f"（原文整体相似度 {raw_pct}%），检出 {len(passages)} 处 ≥25 字连续雷同句段"
                ),
                'detail': {
                    'net_similarity_pct': float(net_pct),
                    'raw_similarity_pct': float(raw_pct),
                    'shared_passages': passages[:5],
                },
            })
        elif net_pct >= Decimal('15.00') or any(p['norm_chars'] >= 45 for p in passages):
            pair_risk = 'medium'
            all_findings.append({
                'code': 'NET_TEXT_MODERATE_SIMILARITY',
                'severity': 'warning',
                'dimension': 'net_text_similarity',
                'a_label': la,
                'b_label': lb,
                'message': (
                    f"「{la}」与「{lb}」净正文相似度 {net_pct}%（原文整体 {raw_pct}%），"
                    f"存在 {len(passages)} 处 ≥25 字连续一致表述"
                ),
                'detail': {
                    'net_similarity_pct': float(net_pct),
                    'raw_similarity_pct': float(raw_pct),
                    'shared_passages': passages[:5],
                },
            })
        else:
            pair_risk = 'low'

        # 维度 3：非标指纹错误与外部单位/项目号碰撞
        ea, eb = bi['entities'], bj['entities']
        shared_ext = sorted(ea['external_refs'] & eb['external_refs'])
        shared_moji = sorted(ea['mojibake_snippets'] & eb['mojibake_snippets'])
        if shared_ext or shared_moji:
            all_findings.append({
                'code': 'FINGERPRINT_ERROR_COLLISION',
                'severity': 'error' if shared_ext else 'warning',
                'dimension': 'fingerprint_error',
                'a_label': la,
                'b_label': lb,
                'message': (
                    f"「{la}」与「{lb}」出现相同的非本项目外部实体或同源异常片段："
                    + '、'.join((shared_ext + shared_moji)[:4])
                ),
                'detail': {
                    'external_refs': shared_ext[:10],
                    'abnormal_snippets': shared_moji[:10],
                },
            })
            if pair_risk == 'low':
                pair_risk = 'medium'

        # 维度 4：人员 / 脱敏手机 / 脱敏身份证 / 合同编号撞车
        collision_items: list[dict[str, str]] = []
        for id_hash in sorted(set(ea['id_cards']) & set(eb['id_cards'])):
            collision_items.append({'type': '身份证号(脱敏)', 'value': ea['id_cards'][id_hash]})
        for mob_hash in sorted(set(ea['mobiles']) & set(eb['mobiles'])):
            collision_items.append({'type': '手机号(脱敏)', 'value': ea['mobiles'][mob_hash]})
        for cno in sorted(set(ea['contracts']) & set(eb['contracts'])):
            collision_items.append({'type': '业绩合同编号', 'value': ea['contracts'][cno]})
        for pname in sorted(set(ea['persons']) & set(eb['persons'])):
            collision_items.append({'type': '核心岗位人员', 'value': pname})

        if collision_items:
            has_hard_id = any(
                it['type'] in ('身份证号(脱敏)', '手机号(脱敏)', '业绩合同编号')
                for it in collision_items
            )
            all_findings.append({
                'code': 'ENTITY_COLLISION',
                'severity': 'error' if has_hard_id else 'warning',
                'dimension': 'entity_collision',
                'a_label': la,
                'b_label': lb,
                'message': (
                    f"「{la}」与「{lb}」存在 {len(collision_items)} 项关键主体/联系方式碰撞："
                    + '，'.join(f"{it['type']} {it['value']}" for it in collision_items[:4])
                ),
                'detail': {'collisions': collision_items},
            })
            if has_hard_id:
                pair_risk = 'high'

        # 维度 5：Decimal 分项报价规律性关联
        price_findings = _compare_price_regularity(bi['price_res'], bj['price_res'])
        for pf in price_findings:
            pf['a_label'] = la
            pf['b_label'] = lb
            pf['message'] = f"「{la}」与「{lb}」{pf['message']}"
            all_findings.append(pf)
            pair_risk = 'high'

        if any(f['severity'] == 'error' for f in meta_findings):
            pair_risk = 'high'
        elif any(f['severity'] == 'warning' for f in meta_findings) and pair_risk == 'low':
            pair_risk = 'medium'

        pair_matrix.append({
            'a_index': bi['index'],
            'b_index': bj['index'],
            'a_label': la,
            'b_label': lb,
            'net_similarity_pct': float(net_pct),
            'raw_similarity_pct': float(raw_pct),
            'risk_level': pair_risk,
            'shared_passage_count': len(passages),
            'shared_passages': passages,
        })

    # 3 家及以上总报价等差数列整体检验
    totals_with_label = [
        (b['label'], b['bid_total_dec'])
        for b in parsed_bids
        if b['bid_total_dec'] is not None and b['bid_total_dec'] > Decimal('0')
    ]
    if len(totals_with_label) >= 3:
        totals_sorted = sorted(totals_with_label, key=lambda x: x[1])
        gaps = [totals_sorted[k + 1][1] - totals_sorted[k][1] for k in range(len(totals_sorted) - 1)]
        if max(gaps) - min(gaps) <= Decimal('0.01') and gaps[0] > Decimal('0.00'):
            step_str = str(gaps[0].quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
            all_findings.append({
                'code': 'REGULAR_PRICE_DIFF',
                'severity': 'error',
                'dimension': 'price_regularity',
                'a_label': totals_sorted[0][0],
                'b_label': totals_sorted[-1][0],
                'message': (
                    f"{len(totals_sorted)} 家参选单位总报价呈现等差数列规律（公差 {step_str} 元）"
                ),
                'detail': {
                    'step': step_str,
                    'sorted_totals': [
                        {'label': lbl, 'bid_total': str(tot)} for lbl, tot in totals_sorted
                    ],
                },
            })

    if any(f['severity'] == 'error' for f in all_findings) or max_net_sim >= Decimal('35.00'):
        overall_risk = 'high'
    elif any(f['severity'] == 'warning' for f in all_findings) or max_net_sim >= Decimal('15.00'):
        overall_risk = 'medium'
    else:
        overall_risk = 'low'

    bidders_summary = [
        {
            'index': b['index'],
            'label': b['label'],
            'filename': b['filename'],
            'rel_path': b['rel_path'],
            'chars': b['chars'],
            'net_chars': b['net_chars'],
            'pages': b['pages'],
            'metadata': b['fingerprint']['metadata'],
            'media_count': b['fingerprint']['media_count'],
            'bid_total': b['bid_total'],
        }
        for b in parsed_bids
    ]

    ai_review: dict[str, Any] = {
        'status': 'skipped',
        'summary': '',
        'semantic_matches': [],
        'false_positive_notes': [],
    }
    if enable_ai_review:
        _emit(84, '阶段 4/4：AI 语义同构深层复核与误报排雷')
        ai_review, ai_findings = run_similarity_ai_review(
            parsed_bids,
            pair_matrix,
            all_findings,
            client,
            cancel_check=cancel_check,
        )
        if ai_findings:
            all_findings.extend(ai_findings)
            if overall_risk == 'low':
                overall_risk = 'medium'

    _emit(95, '整理比对矩阵与脱敏证据报告')
    return {
        'bidders': bidders_summary,
        'pair_matrix': pair_matrix,
        'findings': all_findings,
        'max_similarity_pct': str(max_net_sim),
        'risk_level': overall_risk,
        'ai_review': ai_review,
    }


def export_similarity_report_docx(
    report: dict[str, Any],
    output_path: str | Path,
) -> Path:
    """导出可读《多标书交叉比对与相似度分析报告.docx》（脱敏保障 + 双栏重句对照）。"""
    out_p = Path(output_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)

    doc = Document()
    normal_style = doc.styles['Normal']
    normal_style.font.name = '宋体'
    normal_style.font.size = Pt(10.5)

    title_txt = str(report.get('title') or '多标书交叉比对与相似度分析报告')
    h = doc.add_heading(title_txt, level=0)
    if h.runs:
        h.runs[0].font.color.rgb = RGBColor(0x0F, 0x17, 0x2A)

    tender_fn = report.get('tender_filename') or '未上传（未剔除招标公共条款）'
    risk_zh = {'high': '高风险', 'medium': '中风险（需复核）', 'low': '低风险（正常）'}.get(
        str(report.get('risk_level') or 'low'), '低风险'
    )
    meta_p = doc.add_paragraph()
    meta_p.add_run(f"综合风险等级：{risk_zh}  ｜  ").bold = True
    meta_p.add_run(f"最高净相似度：{report.get('max_similarity_pct', '0.00')}%  ｜  ")
    meta_p.add_run(f"招标参照底稿：{tender_fn}")

    # 1. 参选文件与元数据指纹总表
    doc.add_heading('一、参选文件清单与底层文档属性', level=1)
    bidders = list(report.get('bidders') or [])
    tbl_b = doc.add_table(rows=1, cols=6)
    tbl_b.style = 'Table Grid'
    for idx, col_t in enumerate(('简称', '文件名', '总字数 / 净字数', '作者/最后修改人', '内嵌图片数', '总报价(元)')):
        cell = tbl_b.rows[0].cells[idx]
        cell.text = col_t
        if cell.paragraphs and cell.paragraphs[0].runs:
            cell.paragraphs[0].runs[0].bold = True
    for b in bidders:
        meta = b.get('metadata') or {}
        author_disp = ' / '.join(
            x for x in (meta.get('author'), meta.get('last_modified_by')) if x
        ) or '—'
        row = tbl_b.add_row().cells
        row[0].text = str(b.get('label') or '')
        row[1].text = str(b.get('filename') or '')
        row[2].text = f"{b.get('chars', 0)} / {b.get('net_chars', 0)}"
        row[3].text = author_disp
        row[4].text = str(b.get('media_count', 0))
        row[5].text = str(b.get('bid_total') or '—')

    # 2. 两两对比矩阵
    doc.add_heading('二、两两对比净相似度矩阵', level=1)
    pairs = list(report.get('pair_matrix') or [])
    tbl_p = doc.add_table(rows=1, cols=5)
    tbl_p.style = 'Table Grid'
    for idx, col_t in enumerate(('比对组合', '净正文相似度', '原文整体相似度', '≥25字雷同句数', '风险等级')):
        cell = tbl_p.rows[0].cells[idx]
        cell.text = col_t
        if cell.paragraphs and cell.paragraphs[0].runs:
            cell.paragraphs[0].runs[0].bold = True
    for pr in pairs:
        row = tbl_p.add_row().cells
        row[0].text = f"{pr.get('a_label')} vs {pr.get('b_label')}"
        row[1].text = f"{pr.get('net_similarity_pct', 0)}%"
        row[2].text = f"{pr.get('raw_similarity_pct', 0)}%"
        row[3].text = str(pr.get('shared_passage_count', 0))
        row[4].text = str(pr.get('risk_level', 'low'))

    # 3. 五维风险证据清单
    doc.add_heading('三、五维风险碰撞证据明细', level=1)
    findings = list(report.get('findings') or [])
    if not findings:
        doc.add_paragraph('未检出明显的元数据同源、净正文高相似度、实体撞车或规律报价异常。')
    else:
        for idx, f in enumerate(findings, 1):
            p = doc.add_paragraph()
            p.add_run(f"[{idx}] [{str(f.get('severity') or 'info').upper()}] {f.get('code')}: ").bold = True
            p.add_run(str(f.get('message') or ''))

    # 4. 连续雷同长句双栏对照
    doc.add_heading('四、剔除招标公共条款后的连续雷同原句双栏对照', level=1)
    has_passages = False
    for pr in pairs:
        plist = list(pr.get('shared_passages') or [])
        if not plist:
            continue
        has_passages = True
        doc.add_heading(f"{pr.get('a_label')}  ↔  {pr.get('b_label')}", level=2)
        tbl_s = doc.add_table(rows=1, cols=3)
        tbl_s.style = 'Table Grid'
        tbl_s.rows[0].cells[0].text = '一致字数/页码'
        tbl_s.rows[0].cells[1].text = f"{pr.get('a_label')} 原文摘录"
        tbl_s.rows[0].cells[2].text = f"{pr.get('b_label')} 原文摘录"
        for ps in plist[:10]:
            row = tbl_s.add_row().cells
            row[0].text = (
                f"{ps.get('norm_chars', 0)} 字\n"
                f"A: P{ps.get('a_page') or '?'} / @{ps.get('a_offset')}\n"
                f"B: P{ps.get('b_page') or '?'} / @{ps.get('b_offset')}"
            )
            row[1].text = str(ps.get('a_quote') or '')
            row[2].text = str(ps.get('b_quote') or '')
    if not has_passages:
        doc.add_paragraph('各组对比剔除公共条款后未发现连续 ≥25 字一致的自编句段。')

    ai_rev = report.get('ai_review') or {}
    if isinstance(ai_rev, dict) and ai_rev.get('status') == 'done':
        doc.add_heading('五、AI 语义深层复核与误报排查综述', level=1)
        if ai_rev.get('summary'):
            doc.add_paragraph(str(ai_rev['summary']))
        for idx, sm in enumerate(ai_rev.get('semantic_matches') or [], 1):
            p_sm = doc.add_paragraph()
            p_sm.add_run(
                f"[{idx}] {sm.get('a_label')} ↔ {sm.get('b_label')}（{sm.get('similarity_type')}）："
            ).bold = True
            p_sm.add_run(str(sm.get('reason') or ''))
        for note in ai_rev.get('false_positive_notes') or []:
            doc.add_paragraph(f"误报排查参考：{note}")

    doc.add_paragraph('')
    note_p = doc.add_paragraph()
    note_p.add_run(
        '说明：本报告仅客观呈现多份文件在元数据、内嵌图片、净正文重合、敏感主体信息（已做前 3 后 4 位脱敏）'
        '及分项报价上的技术比对结果，供人工复核与独立编标自查使用。'
    ).italic = True

    doc.save(str(out_p))
    return out_p
