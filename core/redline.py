# -*- coding: utf-8 -*-
"""T17 / E5 · Redline 双引擎：Word 原生 OOXML 批注气泡 + PDF 多级容错定位、总览页与点击跳转高亮副本。

模块定位（V2.0 深度文档工程层 ② & E5 标红定位增强）：
    1. Word (.docx)：输出原生右侧修订批注气泡（Comments）；
    2. PDF (.pdf)：
       - _search_quote_in_pdf 五级降级定位链（L1 原串 -> L2 首尾20字分片 -> L3 标点归一特征片段
         -> L4 rawdict 逐字符归一化滑窗跨行匹配 -> L5 60%与8字置信护栏，宁缺毋滥）；
       - _build_overview_page：在 PDF 首页前插入 A4 竖版「标红批注总览」页（含编号 N01/N02、
         严重级色块、规则类目、风险摘要、定位状态）；
       - 已定位风险：在总览页对应行与顶部快捷定位区注入 PDF 内部跳转链接（LINK_GOTO，点击直接跳至
         正文标红页与坐标）及悬浮风险详情提示，并在正文原位逐 Rect 高亮；
       - 未定位风险：首页顶部图标保留，批注正文前缀升级为 `[全篇·总览 Nxx]` 与总览页编号一一对应。
"""
import copy
import datetime
import difflib
import io
import os
import re
import zipfile

import docx
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
import lxml.etree as ET
import pymupdf

from core.parser import OCR_MIN_CHARS, get_pdf_ocr_data
from core.quote_verifier import normalize_zh

# severity → PDF 高亮颜色（RGB 0~1 浮点，Word 侧颜色仅用于说明文字不设底色）
_SEVERITY_COLORS = {
    'high': (1.0, 0.0, 0.0),
    'medium': (1.0, 0.5, 0.0),
    'low': (1.0, 1.0, 0.0),
}

_SEVERITY_ZH = {
    'high': '高',
    'medium': '中',
    'low': '低',
}

_SEVERITY_OVERVIEW_LABEL = {
    'high': '★高',
    'medium': '▲中',
    'low': '低',
}

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_CT_NS = "http://schemas.openxmlformats.org/package/content-types"
_CT_NS_STD = "http://schemas.openxmlformats.org/package/2006/content-types"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_XML_NS = "http://www.w3.org/XML/1998/namespace"
_COMMENTS_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
_COMMENTS_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"

# L3 标点切分与特征词正则
_L3_PUNCT_SPLIT_RE = re.compile(r'[，。：；！？、“”‘’（）《》－—·、【】［］,.:;!?()"\'\[\]\s\u3000]+')
_L3_FEATURE_RE = re.compile(r'[0-9０-９.%‰亿万元角分条章款项第★▲]')

# L5 置信护栏常量：L4 命中的归一化长度 >= quote 归一化长度的 60% 且 >= 8 字符
_L5_MIN_RATIO = 0.60
_L5_MIN_CHARS = 8


def _format_comment_text(
    finding,
    is_global: bool = False,
    overview_no: str = None,
) -> str:
    """按写死的三段式规格格式化批注正文；
    全局/未命中兜底时：若传入 overview_no（如 'N01'）则前置 '[全篇·总览 N01]'，否则前置 '[全篇]'。
    """
    sev_raw = getattr(finding, 'severity', 'low') or 'low'
    sev_zh = _SEVERITY_ZH.get(sev_raw, str(sev_raw))
    rule_name = getattr(finding, 'rule_name', '') or ''
    message = getattr(finding, 'message', '') or ''
    suggestion = getattr(finding, 'suggestion', '') or ''
    legal_basis = getattr(finding, 'legal_basis', '') or ''
    body = f"【{sev_zh}风险 · {rule_name}】{message}\n修改建议：{suggestion}\n依据：{legal_basis}"
    if is_global:
        if overview_no:
            return f"[全篇·总览 {overview_no}]{body}"
        return f"[全篇]{body}"
    return body


def _is_global_finding(finding) -> bool:
    """判断是否为显式全篇缺失类或无 quote 的全局风险。
    注意：当 quote 非空且 clause_no 不为 '全篇' 时（即使 clause_no 为空串），必须尝试在原文中定位标红。
    """
    clause_no = (getattr(finding, 'clause_no', None) or '').strip()
    quote = (getattr(finding, 'quote', None) or '').strip()
    if not quote or clause_no == '全篇':
        return True
    return False


# ---------------------------------------------------------------- Word OOXML 批注辅助函数
def _clone_run_with_text(src_r_el, new_text: str):
    """克隆 <w:r> 节点（完整保留原 <w:rPr> 样式属性）并写入 new_text。"""
    new_r = OxmlElement('w:r')
    rPr = src_r_el.find(qn('w:rPr'))
    if rPr is not None:
        new_r.append(copy.deepcopy(rPr))
    t = OxmlElement('w:t')
    if new_text and (new_text[0].isspace() or new_text[-1].isspace()):
        t.set(qn('xml:space'), 'preserve')
    t.text = new_text
    new_r.append(t)
    return new_r


def _set_run_text(r_el, new_text: str):
    """将既有 <w:r> 节点内的 <w:t> 替换为单个含 new_text 的 <w:t>。"""
    for old_t in r_el.findall(qn('w:t')):
        r_el.remove(old_t)
    t = OxmlElement('w:t')
    if new_text and (new_text[0].isspace() or new_text[-1].isspace()):
        t.set(qn('xml:space'), 'preserve')
    t.text = new_text
    r_el.append(t)


def _make_comment_markers(comment_id: int):
    """构造 OOXML 批注起止锚点与引用节点三件套。"""
    start_el = OxmlElement('w:commentRangeStart')
    start_el.set(qn('w:id'), str(comment_id))

    end_el = OxmlElement('w:commentRangeEnd')
    end_el.set(qn('w:id'), str(comment_id))

    ref_run = OxmlElement('w:r')
    rPr = OxmlElement('w:rPr')
    rStyle = OxmlElement('w:rStyle')
    rStyle.set(qn('w:val'), 'CommentReference')
    rPr.append(rStyle)
    ref_run.append(rPr)
    ref_el = OxmlElement('w:commentReference')
    ref_el.set(qn('w:id'), str(comment_id))
    ref_run.append(ref_el)

    return start_el, end_el, ref_run


def _iter_all_paragraphs(doc) -> list:
    """收集文档正文段落与表格单元格内的段落（保持文档顺序）。"""
    paras = list(doc.paragraphs)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    if p not in paras:
                        paras.append(p)
    return paras


def _try_attach_quote_in_paragraph(p, norm_quote: str, comment_id: int,
                                   allow_longest_submatch: bool = False) -> bool:
    """在单个段落 p 中按 normalize_zh 映射定位 norm_quote，支持单 Run 与跨 Run 切分包裹。"""
    if not norm_quote:
        return False

    text_runs = []
    run_texts = []
    for r in p.runs:
        raw_t = ''.join(t.text or '' for t in r._r.findall(qn('w:t')))
        if raw_t:
            text_runs.append(r._r)
            run_texts.append(raw_t)

    if not text_runs:
        return False

    norm_chars = []
    pos_map = []
    for r_idx, raw_t in enumerate(run_texts):
        for c_idx, ch in enumerate(run_texts[r_idx]):
            n_ch = normalize_zh(ch)
            for sub_ch in n_ch:
                norm_chars.append(sub_ch)
                pos_map.append((r_idx, c_idx))

    norm_para = ''.join(norm_chars)
    if not norm_para:
        return False

    match_idx = norm_para.find(norm_quote)
    match_len = len(norm_quote)

    if match_idx == -1 and allow_longest_submatch and len(norm_quote) >= 10:
        matcher = difflib.SequenceMatcher(None, norm_quote, norm_para, autojunk=False)
        longest = matcher.find_longest_match(0, len(norm_quote), 0, len(norm_para))
        if longest.size >= max(8, int(len(norm_quote) * 0.75)):
            match_idx = longest.b
            match_len = longest.size

    if match_idx == -1 or match_len <= 0:
        return False

    start_run_idx, start_char_idx = pos_map[match_idx]
    end_run_idx, end_char_idx = pos_map[match_idx + match_len - 1]

    end_r_el = text_runs[end_run_idx]
    end_full_text = run_texts[end_run_idx]
    if end_char_idx < len(end_full_text) - 1:
        left_text = end_full_text[:end_char_idx + 1]
        right_text = end_full_text[end_char_idx + 1:]
        _set_run_text(end_r_el, left_text)
        run_texts[end_run_idx] = left_text
        right_r_el = _clone_run_with_text(end_r_el, right_text)
        end_r_el.addnext(right_r_el)

    start_r_el = text_runs[start_run_idx]
    start_full_text = run_texts[start_run_idx]
    if start_char_idx > 0:
        prefix_text = start_full_text[:start_char_idx]
        target_text = start_full_text[start_char_idx:]
        prefix_r_el = _clone_run_with_text(start_r_el, prefix_text)
        start_r_el.addprevious(prefix_r_el)
        _set_run_text(start_r_el, target_text)
        run_texts[start_run_idx] = target_text

    start_el, end_el, ref_run = _make_comment_markers(comment_id)
    start_r_el.addprevious(start_el)
    end_r_el.addnext(end_el)
    end_el.addnext(ref_run)
    return True


def _attach_to_first_paragraph(doc, comment_id: int):
    """将全篇缺失类或未命中 quote 的 Finding 挂载到文档首段（标题处）。"""
    if not doc.paragraphs:
        doc.add_paragraph('')
    p0 = doc.paragraphs[0]
    content_runs = [
        r._r for r in p0.runs
        if r._r.findall(qn('w:t')) and r._r.find(qn('w:commentReference')) is None
    ]
    start_el, end_el, ref_run = _make_comment_markers(comment_id)
    if content_runs:
        content_runs[0].addprevious(start_el)
        content_runs[-1].addnext(end_el)
        end_el.addnext(ref_run)
    else:
        p0._p.append(start_el)
        p0._p.append(end_el)
        p0._p.append(ref_run)


def _build_comments_xml_bytes(comments_records: list[dict]) -> bytes:
    """构建符合 OOXML 规范的 word/comments.xml 字节流。"""
    nsmap = {'w': _W_NS}
    root = ET.Element(f"{{{_W_NS}}}comments", nsmap=nsmap)
    for rec in comments_records:
        c_el = ET.SubElement(root, f"{{{_W_NS}}}comment")
        c_el.set(f"{{{_W_NS}}}id", str(rec['id']))
        c_el.set(f"{{{_W_NS}}}author", str(rec['author']))
        c_el.set(f"{{{_W_NS}}}date", str(rec['date']))
        c_el.set(f"{{{_W_NS}}}initials", "AI")

        lines = (rec['text'] or '').split('\n')
        for line in lines:
            p_el = ET.SubElement(c_el, f"{{{_W_NS}}}p")
            r_el = ET.SubElement(p_el, f"{{{_W_NS}}}r")
            t_el = ET.SubElement(r_el, f"{{{_W_NS}}}t")
            t_el.set(f"{{{_XML_NS}}}space", "preserve")
            t_el.text = line
    return ET.tostring(root, xml_declaration=True, encoding='UTF-8', standalone='yes')


def _ensure_content_types_xml(ct_bytes: bytes) -> bytes:
    """在 [Content_Types].xml 中注册 /word/comments.xml 内容类型。"""
    root = ET.fromstring(ct_bytes)
    part_name = "/word/comments.xml"
    for child in root.findall(f"{{{_CT_NS_STD}}}Override"):
        if child.get("PartName") == part_name:
            return ct_bytes
    override = ET.SubElement(root, f"{{{_CT_NS_STD}}}Override")
    override.set("PartName", part_name)
    override.set("ContentType", _COMMENTS_CONTENT_TYPE)
    return ET.tostring(root, xml_declaration=True, encoding='UTF-8', standalone='yes')


def _ensure_document_rels_xml(rels_bytes: bytes) -> bytes:
    """在 word/_rels/document.xml.rels 中注册 comments.xml 关系。"""
    root = ET.fromstring(rels_bytes)
    for child in root.findall(f"{{{_REL_NS}}}Relationship"):
        if child.get("Type") == _COMMENTS_REL_TYPE:
            return rels_bytes
    existing_ids = {child.get("Id") for child in root.findall(f"{{{_REL_NS}}}Relationship")}
    idx = len(existing_ids) + 1
    while f"rId{idx}" in existing_ids:
        idx += 1
    rel = ET.SubElement(root, f"{{{_REL_NS}}}Relationship")
    rel.set("Id", f"rId{idx}")
    rel.set("Type", _COMMENTS_REL_TYPE)
    rel.set("Target", "comments.xml")
    return ET.tostring(root, xml_declaration=True, encoding='UTF-8', standalone='yes')


def inject_word_comments(src_docx_path: str, findings: list,
                          out_docx_path: str = None, author: str = 'AI法务初审员') -> bytes:
    """在原版 .docx 合同中按 Finding.quote 精确定位文本，直接写入 Word 原生
    修订批注气泡（Comments）。
    """
    if not isinstance(src_docx_path, str) or not src_docx_path.strip():
        raise RuntimeError("Word 源文件路径不能为空且必须为字符串")
    if not os.path.isfile(src_docx_path):
        raise RuntimeError(f"Word 源文件不存在：{src_docx_path}")
    if not src_docx_path.lower().endswith('.docx'):
        raise RuntimeError(f"源文件不是受支持的 .docx 格式：{src_docx_path}")
    if findings is None or not isinstance(findings, (list, tuple)):
        raise TypeError("findings 必须为 list 或 tuple")

    try:
        with open(src_docx_path, 'rb') as f:
            src_bytes = f.read()
    except OSError as e:
        raise RuntimeError(f"无法读取 Word 源文件 {src_docx_path}: {e}") from e

    try:
        doc = docx.Document(io.BytesIO(src_bytes))
    except Exception as e:
        raise RuntimeError(f"无法解析 .docx 文件（格式损坏或非法）：{src_docx_path}: {e}") from e

    if not findings:
        if out_docx_path:
            os.makedirs(os.path.dirname(os.path.abspath(out_docx_path)), exist_ok=True)
            with open(out_docx_path, 'wb') as f:
                f.write(src_bytes)
        return src_bytes

    all_paragraphs = _iter_all_paragraphs(doc)
    iso_now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    comments_records = []

    for idx, finding in enumerate(findings, start=1):
        comment_id = idx
        if _is_global_finding(finding):
            _attach_to_first_paragraph(doc, comment_id)
            comments_records.append({
                'id': comment_id,
                'author': author or 'AI法务初审员',
                'date': iso_now,
                'text': _format_comment_text(finding, is_global=True),
            })
            continue

        norm_q = normalize_zh(finding.quote)
        attached = False

        if norm_q:
            for p in all_paragraphs:
                if _try_attach_quote_in_paragraph(p, norm_q, comment_id, allow_longest_submatch=False):
                    attached = True
                    break

        if not attached and norm_q:
            for p in all_paragraphs:
                if _try_attach_quote_in_paragraph(p, norm_q, comment_id, allow_longest_submatch=True):
                    attached = True
                    break

        if not attached:
            _attach_to_first_paragraph(doc, comment_id)
            comment_text = _format_comment_text(finding, is_global=True)
        else:
            comment_text = _format_comment_text(finding, is_global=False)

        comments_records.append({
            'id': comment_id,
            'author': author or 'AI法务初审员',
            'date': iso_now,
            'text': comment_text,
        })

    mid_buf = io.BytesIO()
    doc.save(mid_buf)
    mid_bytes = mid_buf.getvalue()

    out_buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(mid_bytes), 'r') as zin:
        with zipfile.ZipFile(out_buf, 'w', zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                data = zin.read(info.filename)
                if info.filename == '[Content_Types].xml':
                    data = _ensure_content_types_xml(data)
                elif info.filename == 'word/_rels/document.xml.rels':
                    data = _ensure_document_rels_xml(data)
                elif info.filename == 'word/comments.xml':
                    continue
                zout.writestr(info, data)
            zout.writestr('word/comments.xml', _build_comments_xml_bytes(comments_records))

    result_bytes = out_buf.getvalue()
    if out_docx_path:
        os.makedirs(os.path.dirname(os.path.abspath(out_docx_path)), exist_ok=True)
        with open(out_docx_path, 'wb') as f:
            f.write(result_bytes)
    return result_bytes


# ---------------------------------------------------------------- PDF 高亮批注与 E5 多级定位增强实现

def _extract_l3_feature_fragments(raw_q: str) -> list[str]:
    """L3 标点归一特征片段提取：
    将 quote 中全角/半角标点替换为空格切分，优先选含数字/金额/编号特征的 8~30 字片段，
    其次选纯汉字 10~30 字片段，去重返回。
    """
    tokens = [t.strip() for t in _L3_PUNCT_SPLIT_RE.split(raw_q or '') if t and t.strip()]
    feature_frags: list[str] = []
    plain_frags: list[str] = []

    for tok in tokens:
        candidates = [tok]
        if len(tok) > 30:
            candidates = [tok[:24], tok[-24:]]
        for c in candidates:
            c = c.strip()
            if not c:
                continue
            has_feature = bool(_L3_FEATURE_RE.search(c))
            if has_feature and 8 <= len(c) <= 30:
                if c not in feature_frags:
                    feature_frags.append(c)
            elif not has_feature and 10 <= len(c) <= 30:
                if c not in plain_frags:
                    plain_frags.append(c)

    return feature_frags + plain_frags


def _norm_char_for_l4(ch: str, strip_punct: bool = False) -> str:
    """L4 字符级归一化：全角转半角、大写转小写、删除全部空白字符；
    strip_punct=True 时进一步剔除标点符号，仅保留中英文与数字以穿透全半角标点混用。
    """
    if not ch or ch.isspace() or ch == '\u3000':
        return ''
    n = normalize_zh(ch).lower()
    if not n:
        return ''
    if strip_punct:
        n = re.sub(r'[^0-9a-z\u4e00-\u9fa5★▲]', '', n)
    return n


def _extract_page_rawdict_chars(page, strip_punct: bool = False) -> list[tuple[str, pymupdf.Rect, tuple[int, int]]]:
    """从 page.get_text('rawdict') 提取逐字符 (norm_char, char_rect, (block_idx, line_idx)) 序列。"""
    try:
        raw_dict = page.get_text("rawdict")
    except Exception:
        return []

    seq: list[tuple[str, pymupdf.Rect, tuple[int, int]]] = []
    for b_idx, block in enumerate(raw_dict.get("blocks", [])):
        if block.get("type", 0) != 0:
            continue
        for l_idx, line in enumerate(block.get("lines", [])):
            for span in line.get("spans", []):
                chars = span.get("chars")
                if chars:
                    for ch_info in chars:
                        c_str = ch_info.get("c", "")
                        bbox = ch_info.get("bbox")
                        n_ch = _norm_char_for_l4(c_str, strip_punct=strip_punct)
                        if n_ch and bbox and len(bbox) == 4:
                            r = pymupdf.Rect(bbox)
                            if r.width > 0 and r.height > 0:
                                for sub_c in n_ch:
                                    seq.append((sub_c, r, (b_idx, l_idx)))
                else:
                    # 少数 PDF 字体无 chars 明细时按 span text 均分 bbox 兜底
                    s_txt = span.get("text", "")
                    bbox = span.get("bbox")
                    if s_txt and bbox and len(bbox) == 4:
                        r = pymupdf.Rect(bbox)
                        if r.width > 0 and r.height > 0:
                            for ch in s_txt:
                                n_ch = _norm_char_for_l4(ch, strip_punct=strip_punct)
                                for sub_c in n_ch:
                                    seq.append((sub_c, r, (b_idx, l_idx)))
    return seq


def _merge_char_rects_by_line(
    matched_slice: list[tuple[str, pymupdf.Rect, tuple[int, int]]],
) -> list[pymupdf.Rect]:
    """将命中字符区间的 bbox 按所属行（y 坐标相近）分组，每组合并为一个 Rect，支持跨行多 Rect 高亮。"""
    if not matched_slice:
        return []

    line_groups: list[list[pymupdf.Rect]] = []
    current_group: list[pymupdf.Rect] = [matched_slice[0][1]]
    current_cy = (matched_slice[0][1].y0 + matched_slice[0][1].y1) / 2.0

    for _ch, rect, _lkey in matched_slice[1:]:
        cy = (rect.y0 + rect.y1) / 2.0
        tol = max(4.5, min(rect.height, current_group[-1].height) * 0.65)
        if abs(cy - current_cy) <= tol:
            current_group.append(rect)
            current_cy = sum((r.y0 + r.y1) / 2.0 for r in current_group) / len(current_group)
        else:
            line_groups.append(current_group)
            current_group = [rect]
            current_cy = cy

    if current_group:
        line_groups.append(current_group)

    merged_rects: list[pymupdf.Rect] = []
    for grp in line_groups:
        x0 = min(r.x0 for r in grp)
        y0 = min(r.y0 for r in grp)
        x1 = max(r.x1 for r in grp)
        y1 = max(r.y1 for r in grp)
        u_rect = pymupdf.Rect(x0, y0, x1, y1)
        if u_rect.width > 0 and u_rect.height > 0:
            merged_rects.append(u_rect)

    return merged_rects


def _search_l4_with_l5_guard(pdf_doc, raw_q: str) -> list[tuple[int, list]]:
    """L4 字符序列归一匹配 + L5 置信护栏（命中长度 >= quote 归一化长度 60% 且 >= 8 字符）。"""
    for strip_punct in (False, True):
        q_norm = ''.join(_norm_char_for_l4(ch, strip_punct=strip_punct) for ch in raw_q)
        if len(q_norm) < _L5_MIN_CHARS:
            continue

        min_required_len = max(_L5_MIN_CHARS, int(len(q_norm) * _L5_MIN_RATIO + 0.9999))
        hits: list[tuple[int, list]] = []

        for page_idx in range(len(pdf_doc)):
            page = pdf_doc[page_idx]
            char_seq = _extract_page_rawdict_chars(page, strip_punct=strip_punct)
            if len(char_seq) < min_required_len:
                continue

            page_norm_str = ''.join(item[0] for item in char_seq)
            pos = page_norm_str.find(q_norm)
            if pos != -1:
                m_start = pos
                m_len = len(q_norm)
            else:
                sm = difflib.SequenceMatcher(None, q_norm, page_norm_str, autojunk=False)
                longest = sm.find_longest_match(0, len(q_norm), 0, len(page_norm_str))
                m_start = longest.b
                m_len = longest.size

            # L5 置信护栏：必须同时满足 >= 8 字符 且 >= 60% 归一化长度，宁缺毋滥
            if m_len >= _L5_MIN_CHARS and (m_len / len(q_norm)) >= _L5_MIN_RATIO:
                rects = _merge_char_rects_by_line(char_seq[m_start:m_start + m_len])
                if rects:
                    hits.append((page_idx, rects))

        if hits:
            return hits

    return []


def _search_quote_in_pdf(pdf_doc, quote: str) -> list[tuple[int, list]]:
    """在 PDF 各页中按五级降级链检索 quote，任一级命中即止，返回 [(page_index, [Rect, ...]), ...]。

    - L1：原串 page.search_for(raw_q) 全页扫；
    - L2：原样/空白折叠的首尾 20 字分片重试（仅当原串 >= 8 字时启用分片，防极短残片误锚）；
    - L3：标点归一特征片段（8~30 字含数字/金额/编号特征优先，纯汉字 10~30 字次之）；
    - L4：page.get_text("rawdict") 逐字符归一化序列滑窗匹配，按行合并跨行 Rect；
    - L5：置信护栏（L4 命中长度 >= 60% quote 归一化长度 且 >= 8 字符，否则拒绝乱锚）。
    """
    raw_q = (quote or '').strip()
    if not raw_q:
        return []

    # ── L1：原串直接检索 ──
    hits: list[tuple[int, list]] = []
    for page_idx in range(len(pdf_doc)):
        page = pdf_doc[page_idx]
        rects = page.search_for(raw_q)
        if rects:
            hits.append((page_idx, rects))
    if hits:
        return hits

    # ── L2：原样与空白折叠的首尾 20 字符分片重试 ──
    fold_q = re.sub(r'\s+', ' ', raw_q).strip()
    fragments = []
    for candidate in (
        raw_q[:20] if len(raw_q) >= 8 else '',
        raw_q[-20:] if len(raw_q) > 20 else '',
        fold_q[:20] if len(fold_q) >= 8 else '',
        fold_q[-20:] if len(fold_q) > 20 else '',
    ):
        c = candidate.strip()
        if c and len(c) >= 6 and c not in fragments:
            fragments.append(c)

    if fragments:
        for page_idx in range(len(pdf_doc)):
            page = pdf_doc[page_idx]
            page_rects = []
            for frag in fragments:
                r_list = page.search_for(frag)
                for r in r_list:
                    if r not in page_rects:
                        page_rects.append(r)
            if page_rects:
                hits.append((page_idx, page_rects))
        if hits:
            return hits

    # ── L3：标点归一特征片段检索 ──
    l3_frags = _extract_l3_feature_fragments(raw_q)
    if l3_frags:
        for page_idx in range(len(pdf_doc)):
            page = pdf_doc[page_idx]
            page_rects = []
            for frag in l3_frags:
                r_list = page.search_for(frag)
                for r in r_list:
                    if r not in page_rects:
                        page_rects.append(r)
            if page_rects:
                hits.append((page_idx, page_rects))
        if hits:
            return hits

    # ── L4 + L5：rawdict 逐字符归一化滑窗匹配 + 60% & 8字置信护栏 ──
    return _search_l4_with_l5_guard(pdf_doc, raw_q)


def _build_overview_page(
    pdf_doc,
    findings: list,
    anchored_map: dict[int, list[tuple[int, list]]],
    author_name: str = 'AI法务初审员',
) -> int:
    """E5 §3：在 PDF 最前插入 A4 竖版「标红批注总览」页（支持多页自动续表），
    并为每条已定位风险注入顶部快捷跳转图标与行内点击跳转链接（LINK_GOTO 直达正文标红处）+ 悬浮风险详情。

    Args:
        pdf_doc: 已打开的 pymupdf.Document。
        findings: 风险列表。
        anchored_map: {finding_1based_idx: [(orig_page_idx, [Rect, ...]), ...]}。
        author_name: 批注作者名。

    Returns:
        inserted_pages_count: 实际在文档头部插入的总览页页数 k（原正文页索引整体后移 +k）。
    """
    if not findings:
        return 0

    page_w, page_h = 595.0, 842.0
    rows_per_page = 16
    total_findings = len(findings)
    num_ov_pages = max(1, (total_findings + rows_per_page - 1) // rows_per_page)

    # 先在文档头部依次插入 num_ov_pages 张总览页（pno=0, 1, ...）
    ov_pages = []
    for p_i in range(num_ov_pages):
        ov_p = pdf_doc.new_page(pno=p_i, width=page_w, height=page_h)
        ov_pages.append(ov_p)

    # 此时原正文页的真实页索引 = orig_page_idx + num_ov_pages
    anchored_count = len(anchored_map)
    unanchored_count = total_findings - anchored_count

    # 表格列宽定义（总宽 595 - 左右边距 36*2 = 523）
    col_x = [36.0, 78.0, 130.0, 235.0, 435.0, 559.0]
    headers = ['编号', '严重级', '规则类目', '风险说明摘要', '定位状态（点击跳转）']

    for page_no_idx, ov_page in enumerate(ov_pages):
        # 1. 顶部标题区
        title_suffix = f"（第 {page_no_idx + 1}/{num_ov_pages} 页）" if num_ov_pages > 1 else ""
        ov_page.insert_textbox(
            pymupdf.Rect(36, 48, 559, 80),
            f"标红批注总览{title_suffix}",
            fontsize=16,
            fontname="china-s",
            color=(0.06, 0.09, 0.16),
        )
        summary_line = (
            f"共检出风险 {total_findings} 项：已定位并原位高亮 {anchored_count} 项（点击下方蓝色链接或顶部图标可直达原文） ｜ "
            f"全篇级未锚定 {unanchored_count} 项"
        )
        ov_page.insert_textbox(
            pymupdf.Rect(36, 78, 559, 100),
            summary_line,
            fontsize=9.5,
            fontname="china-s",
            color=(0.28, 0.33, 0.41),
        )

        # 2. 表头绘制
        y_top = 108.0
        hdr_h = 24.0
        ov_page.draw_rect(
            pymupdf.Rect(col_x[0], y_top, col_x[-1], y_top + hdr_h),
            color=(0.80, 0.84, 0.88),
            fill=(0.94, 0.96, 0.98),
        )
        for c_i, h_text in enumerate(headers):
            ov_page.insert_textbox(
                pymupdf.Rect(col_x[c_i] + 4, y_top + 4, col_x[c_i + 1] - 4, y_top + hdr_h - 2),
                h_text,
                fontsize=9.5,
                fontname="china-s",
                color=(0.06, 0.09, 0.16),
            )

        # 3. 逐行绘制 findings 明细
        start_f_idx = page_no_idx * rows_per_page
        end_f_idx = min(total_findings, start_f_idx + rows_per_page)
        row_y = y_top + hdr_h
        row_h = 40.0

        for f_zero_idx in range(start_f_idx, end_f_idx):
            f_idx = f_zero_idx + 1
            finding = findings[f_zero_idx]
            n_code = f"N{f_idx:02d}"
            sev_raw = (getattr(finding, 'severity', 'low') or 'low').lower()
            sev_label = _SEVERITY_OVERVIEW_LABEL.get(sev_raw, '低')
            sev_color = _SEVERITY_COLORS.get(sev_raw, (1.0, 1.0, 0.0))

            rule_name = (getattr(finding, 'rule_name', '') or '合规核查项').strip()
            raw_msg = re.sub(r'\s+', ' ', (getattr(finding, 'message', '') or '').strip())
            msg_short = (raw_msg[:38] + '…') if len(raw_msg) > 40 else raw_msg

            is_anchored = f_idx in anchored_map
            status_str = "已高亮于正文\n见正文红色高亮" if is_anchored else "全篇级·未锚定"

            row_rect = pymupdf.Rect(col_x[0], row_y, col_x[-1], row_y + row_h)
            bg_fill = (1.0, 1.0, 1.0) if (f_idx % 2 == 1) else (0.98, 0.99, 1.0)
            ov_page.draw_rect(row_rect, color=(0.86, 0.89, 0.92), fill=bg_fill)

            # 列 1：编号 N01
            ov_page.insert_textbox(
                pymupdf.Rect(col_x[0] + 4, row_y + 6, col_x[1] - 4, row_y + row_h - 4),
                n_code,
                fontsize=9.5,
                fontname="china-s",
                color=(0.06, 0.09, 0.16),
            )

            # 列 2：严重级（带 _SEVERITY_COLORS 同源浅化底色块）
            badge_rect = pymupdf.Rect(col_x[1] + 4, row_y + 8, col_x[2] - 6, row_y + row_h - 8)
            tint_fill = (
                min(1.0, sev_color[0] * 0.25 + 0.75),
                min(1.0, sev_color[1] * 0.25 + 0.75),
                min(1.0, sev_color[2] * 0.25 + 0.75),
            )
            ov_page.draw_rect(badge_rect, color=sev_color, fill=tint_fill)
            ov_page.insert_textbox(
                pymupdf.Rect(badge_rect.x0 + 3, badge_rect.y0 + 3, badge_rect.x1 - 2, badge_rect.y1 - 2),
                sev_label,
                fontsize=8.5,
                fontname="china-s",
                color=(0.1, 0.1, 0.1),
            )

            # 列 3：规则类目
            ov_page.insert_textbox(
                pymupdf.Rect(col_x[2] + 4, row_y + 5, col_x[3] - 4, row_y + row_h - 4),
                rule_name[:22],
                fontsize=8.5,
                fontname="china-s",
                color=(0.12, 0.16, 0.23),
            )

            # 列 4：message 摘要
            ov_page.insert_textbox(
                pymupdf.Rect(col_x[3] + 4, row_y + 5, col_x[4] - 4, row_y + row_h - 4),
                msg_short,
                fontsize=8.5,
                fontname="china-s",
                color=(0.12, 0.16, 0.23),
            )

            # 列 5：定位状态 + 内部跳转链接 LINK_GOTO + 风险详情悬浮提示
            status_rect = pymupdf.Rect(col_x[4] + 4, row_y + 5, col_x[5] - 4, row_y + row_h - 4)
            status_color = (0.11, 0.31, 0.85) if is_anchored else (0.55, 0.25, 0.05)
            ov_page.insert_textbox(
                status_rect,
                status_str,
                fontsize=8.0,
                fontname="china-s",
                color=status_color,
            )

            if is_anchored:
                first_orig_page, first_rects = anchored_map[f_idx][0]
                target_page_idx = first_orig_page + num_ov_pages
                target_pt = (
                    pymupdf.Point(first_rects[0].x0, max(0.0, first_rects[0].y0 - 20.0))
                    if first_rects
                    else pymupdf.Point(0.0, 0.0)
                )
                # 点击整行直接跳转到正文对应标红页与坐标位置
                try:
                    ov_page.insert_link({
                        'kind': pymupdf.LINK_GOTO,
                        'from': row_rect,
                        'page': target_page_idx,
                        'to': target_pt,
                    })
                except Exception:
                    pass

                # 在总览页顶部快捷栏同步放置带完整风险内容的图标与跳转链接（满足用户"顶部图标显示风险详情+点击跳转"）
                if page_no_idx == 0 and f_idx <= 18:
                    top_x = min(36.0 + (f_idx - 1) * 28.0, page_w - 42.0)
                    top_rect = pymupdf.Rect(top_x, 8.0, top_x + 24.0, 22.0)
                    ov_page.draw_rect(top_rect, color=sev_color, fill=tint_fill)
                    ov_page.insert_textbox(
                        top_rect,
                        n_code,
                        fontsize=7.0,
                        fontname="china-s",
                        color=(0.06, 0.09, 0.16),
                    )
                    try:
                        ov_page.insert_link({
                            'kind': pymupdf.LINK_GOTO,
                            'from': top_rect,
                            'page': target_page_idx,
                            'to': target_pt,
                        })
                    except Exception:
                        pass

            row_y += row_h

    return num_ov_pages


# ── T46 扫描件双路线 Redline 支持（路线 A 夹心层 + 路线 B 直接高亮兜底）───────
def _apply_sandwich_layer(pdf_doc, ocr_pages_data: list[dict]):
    """路线 A：将 OCR 识别的文本按 bbox 写入透明夹心层（Sandwich PDF）。"""
    if not ocr_pages_data:
        return pdf_doc

    for page_data in ocr_pages_data:
        p_idx = page_data.get('page_index', 0)
        if p_idx >= len(pdf_doc):
            continue
        page = pdf_doc[p_idx]
        img_w = page_data.get('img_width', page.rect.width) or page.rect.width
        img_h = page_data.get('img_height', page.rect.height) or page.rect.height
        scale_x = page.rect.width / img_w if img_w > 0 else 1.0
        scale_y = page.rect.height / img_h if img_h > 0 else 1.0

        for line in page_data.get('lines', []):
            text = (line.get('text') or '').strip()
            box = line.get('box')
            if not text or not box or len(box) < 4:
                continue

            xs = [pt[0] * scale_x for pt in box]
            ys = [pt[1] * scale_y for pt in box]
            x0 = min(xs) + page.rect.x0
            y0 = min(ys) + page.rect.y0
            x1 = max(xs) + page.rect.x0
            y1 = max(ys) + page.rect.y0

            rect = pymupdf.Rect(x0, y0, x1, y1)
            if rect.width <= 0 or rect.height <= 0:
                continue

            fontsize = max(6.0, min(rect.height * 0.85, 36.0))
            rc = page.insert_textbox(
                rect,
                text,
                fontsize=fontsize,
                fontname="china-s",
                render_mode=3,
            )
            shrink = fontsize
            while rc < 0 and shrink > 4.0:
                shrink = max(4.0, shrink - 1.0)
                rc = page.insert_textbox(
                    rect,
                    text,
                    fontsize=shrink,
                    fontname="china-s",
                    render_mode=3,
                )
            if rc < 0:
                try:
                    pt = pymupdf.Point(rect.x0, rect.y0 + rect.height * 0.8)
                    page.insert_text(
                        pt,
                        text,
                        fontsize=fontsize,
                        fontname="china-s",
                        render_mode=3,
                    )
                except Exception:
                    pass

    sandwich_bytes = pdf_doc.tobytes()
    pdf_doc.close()
    return pymupdf.open(stream=sandwich_bytes, filetype="pdf")


def _match_ocr_boxes_fallback(ocr_pages_data: list[dict], quote: str, pdf_doc) -> list[tuple[int, list]]:
    """路线 B 兜底：当 Sandwich search_for 未命中时，根据 OCR bbox 进行模糊匹配定位。"""
    raw_q = (quote or '').strip()
    if not raw_q or not ocr_pages_data:
        return []

    norm_q = normalize_zh(raw_q)
    if not norm_q:
        return []

    hits_by_page = {}

    for page_data in ocr_pages_data:
        p_idx = page_data.get('page_index', 0)
        if p_idx >= len(pdf_doc):
            continue
        page = pdf_doc[p_idx]
        img_w = page_data.get('img_width', page.rect.width) or page.rect.width
        img_h = page_data.get('img_height', page.rect.height) or page.rect.height
        scale_x = page.rect.width / img_w if img_w > 0 else 1.0
        scale_y = page.rect.height / img_h if img_h > 0 else 1.0

        for line in page_data.get('lines', []):
            line_text = line.get('text', '').strip()
            if not line_text:
                continue
            norm_line = normalize_zh(line_text)
            if not norm_line or len(norm_line) < 8:
                continue

            matched = False
            if len(norm_q) >= 8 and (norm_line in norm_q or norm_q in norm_line):
                matched = True
            elif len(norm_line) >= 8 and len(norm_q) >= 8:
                matcher = difflib.SequenceMatcher(None, norm_q, norm_line, autojunk=False)
                longest = matcher.find_longest_match(0, len(norm_q), 0, len(norm_line))
                if longest.size >= max(8, int(min(len(norm_q), len(norm_line)) * 0.6)):
                    matched = True

            if matched:
                box = line.get('box')
                if box and len(box) >= 4:
                    xs = [pt[0] * scale_x for pt in box]
                    ys = [pt[1] * scale_y for pt in box]
                    x0 = min(xs) + page.rect.x0
                    y0 = min(ys) + page.rect.y0
                    x1 = max(xs) + page.rect.x0
                    y1 = max(ys) + page.rect.y0
                    r = pymupdf.Rect(x0, y0, x1, y1)
                    if r.width > 0 and r.height > 0:
                        hits_by_page.setdefault(p_idx, []).append(r)

    return [(p_idx, rects) for p_idx, rects in hits_by_page.items()]


def inject_pdf_highlights(src_pdf_path: str, findings: list,
                          out_pdf_path: str = None, author: str = 'AI法务初审员') -> bytes:
    """在原版 .pdf 文档中按 Finding.quote 执行五级定位，生成首部「标红批注总览」页（带点击跳转链接），
    并在正文原位注入高危红/中危橙/低危黄高亮标注与弹窗批注内容。
    """
    if not isinstance(src_pdf_path, str) or not src_pdf_path.strip():
        raise RuntimeError("PDF 源文件路径不能为空且必须为字符串")
    if not os.path.isfile(src_pdf_path):
        raise RuntimeError(f"PDF 源文件不存在：{src_pdf_path}")
    if not src_pdf_path.lower().endswith('.pdf'):
        raise RuntimeError(f"源文件不是受支持的 .pdf 格式：{src_pdf_path}")
    if findings is None or not isinstance(findings, (list, tuple)):
        raise TypeError("findings 必须为 list 或 tuple")

    try:
        with open(src_pdf_path, 'rb') as f:
            src_bytes = f.read()
    except OSError as e:
        raise RuntimeError(f"无法读取 PDF 源文件 {src_pdf_path}: {e}") from e

    try:
        pdf_doc = pymupdf.open(stream=src_bytes, filetype="pdf")
        if len(pdf_doc) < 1:
            pdf_doc.close()
            raise RuntimeError(f"PDF 文件不包含任何页面：{src_pdf_path}")
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"无法解析 .pdf 文件（格式损坏或非法）：{src_pdf_path}: {e}") from e

    if not findings:
        pdf_doc.close()
        if out_pdf_path:
            os.makedirs(os.path.dirname(os.path.abspath(out_pdf_path)), exist_ok=True)
            with open(out_pdf_path, 'wb') as f:
                f.write(src_bytes)
        return src_bytes

    author_name = author or 'AI法务初审员'

    total_chars = sum(len(p.get_text().strip()) for p in pdf_doc)
    max_page_chars = max((len(p.get_text().strip()) for p in pdf_doc), default=0)
    is_scanned = (total_chars < OCR_MIN_CHARS and max_page_chars < 20)
    ocr_data = []

    if is_scanned:
        try:
            ocr_data = get_pdf_ocr_data(src_pdf_path)
            if ocr_data:
                pdf_doc = _apply_sandwich_layer(pdf_doc, ocr_data)
        except Exception:
            pass

    try:
        # 1. 先对全部 findings 跑五级定位链，得到 anchored_map（键为 1-based 序号）
        anchored_map: dict[int, list[tuple[int, list]]] = {}
        for f_idx, finding in enumerate(findings, start=1):
            if _is_global_finding(finding):
                hits = []
            else:
                hits = _search_quote_in_pdf(pdf_doc, getattr(finding, 'quote', '') or '')
                if not hits and is_scanned and ocr_data:
                    hits = _match_ocr_boxes_fallback(ocr_data, getattr(finding, 'quote', '') or '', pdf_doc)
            if hits:
                anchored_map[f_idx] = hits

        # 2. 在文档最前插入「标红批注总览」页（内部自动为已定位项绑定 LINK_GOTO 跳转链接）
        ov_page_count = _build_overview_page(pdf_doc, list(findings), anchored_map, author_name=author_name)

        # 3. 已定位项在正文页（orig_page_idx + ov_page_count）逐 Rect 高亮；
        #    未定位项在首页顶部生成图标，且批注正文前缀升级为 [全篇·总览 Nxx]
        global_fallback_count = 0
        for f_idx, finding in enumerate(findings, start=1):
            sev = getattr(finding, 'severity', 'low') or 'low'
            color = _SEVERITY_COLORS.get(sev, (1.0, 1.0, 0.0))
            n_code = f"N{f_idx:02d}"

            if f_idx in anchored_map:
                comment_text = _format_comment_text(finding, is_global=False)
                for orig_page_idx, rects in anchored_map[f_idx]:
                    page = pdf_doc[orig_page_idx + ov_page_count]
                    for rect in rects:
                        annot = page.add_highlight_annot(rect)
                        annot.set_colors(stroke=color)
                        annot.set_info(title=author_name, content=comment_text)
                        annot.update()
            else:
                comment_text = _format_comment_text(finding, is_global=True, overview_no=n_code)
                page0 = pdf_doc[0]
                x_pos = min(36 + global_fallback_count * 26, max(36, int(page0.rect.width) - 40))
                pt = pymupdf.Point(x_pos, 26)
                annot = page0.add_text_annot(pt, comment_text)
                try:
                    annot.set_colors(stroke=color)
                except Exception:
                    pass
                annot.set_info(title=author_name, content=comment_text)
                annot.update()
                global_fallback_count += 1

        result_bytes = pdf_doc.tobytes()
    finally:
        pdf_doc.close()

    if out_pdf_path:
        os.makedirs(os.path.dirname(os.path.abspath(out_pdf_path)), exist_ok=True)
        with open(out_pdf_path, 'wb') as f:
            f.write(result_bytes)
    return result_bytes