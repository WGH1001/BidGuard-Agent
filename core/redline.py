# -*- coding: utf-8 -*-
"""T17 · Redline 双引擎：Word 原生 OOXML 批注气泡 + PDF 高亮批注副本。

模块定位（V2.0 深度文档工程层 ②，HANDOVER_V2.md 第二节与 §4.4）：
    法务交付"最后一公里"——不再让法务对照独立报告逐条改合同，而是直接在
    用户上传的原版合同上插入 AI 审查批注：Word 输出原生右侧修订批注气泡
    （Comments），PDF 输出带彩色高亮与悬浮弹窗批注的副本。

技术风险声明（HANDOVER_V1_ARCHIVE.md 十一·附 2，接手必读）：
    ★ python-docx 无原生批注 API——必须手搓 OOXML：
      ① 新增 word/comments.xml（w:comment 元素集）；
      ② [Content_Types].xml 注册 comments 部件内容类型；
      ③ word/_rels/document.xml.rels 添加关系（.../comments）；
      ④ document.xml 中在目标文本前后插入
         w:commentRangeStart / w:commentRangeEnd / w:r>w:commentReference。
    ★ Finding.pos 是"提取纯文本"的偏移量、不是 docx run 的偏移——定位必须
      走 quote 文本匹配（段落 text 级归一化匹配 → run 级偏移映射），
      归一化复用 core.quote_verifier.normalize_zh。
    本任务是全路线图唯一高风险项，独立按天评估，测试要求最严。

设计红线：
    ★ 绝不丢失：任何一条 Finding 必须产出批注——quote 匹配不到段落/
      全篇缺失类风险，一律挂载到文档首段（标题处）作为全局批注。
    ★ 绝不篡改原文件：源文件只读；产物经 bytes 返回（指定 out 路径时
      额外落盘）；源文件字节级不变。
    ★ 依赖白名单：python-docx（含其传递依赖 lxml）+ PyMuPDF(fitz) +
      标准库 zipfile——三者均已在 requirements.txt，禁新增。
    ★ 库模块只抛不打印；批注后文档必须仍可被 python-docx / fitz 正常打开。
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
import pymupdf
import lxml.etree as ET

from core.quote_verifier import normalize_zh
from core.parser import OCR_MIN_CHARS, get_pdf_ocr_data

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

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_XML_NS = "http://www.w3.org/XML/1998/namespace"
_COMMENTS_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
_COMMENTS_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"


def _format_comment_text(finding, is_global: bool = False) -> str:
    """按写死的三段式规格格式化批注正文；全局/未命中兜底时前置 '[全篇]' 标记。"""
    sev_raw = getattr(finding, 'severity', 'low') or 'low'
    sev_zh = _SEVERITY_ZH.get(sev_raw, str(sev_raw))
    rule_name = getattr(finding, 'rule_name', '') or ''
    message = getattr(finding, 'message', '') or ''
    suggestion = getattr(finding, 'suggestion', '') or ''
    legal_basis = getattr(finding, 'legal_basis', '') or ''
    body = f"【{sev_zh}风险 · {rule_name}】{message}\n修改建议：{suggestion}\n依据：{legal_basis}"
    if is_global:
        return f"[全篇]{body}"
    return body


def _is_global_finding(finding) -> bool:
    """判断是否为全篇缺失类或无 quote 的全局风险。"""
    clause_no = (getattr(finding, 'clause_no', None) or '').strip()
    quote = (getattr(finding, 'quote', None) or '').strip()
    if not clause_no or clause_no == '全篇' or not quote:
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
    """构造 OOXML 批注起止锚点与引用节点三件套：
    <w:commentRangeStart w:id="N"/>
    <w:commentRangeEnd w:id="N"/>
    <w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:commentReference w:id="N"/></w:r>
    """
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

    # 仅收集实际携带 <w:t> 文本的 <w:r> 节点（跳过此前已插入的 CommentReference 空文本 run）
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
    pos_map = []  # norm_para 下标 -> (run_idx, char_idx_in_run)
    for r_idx, raw_t in enumerate(run_texts):
        for c_idx, ch in enumerate(raw_t):
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

    # 1. 先切分末尾 Run（若匹配终点不在该 Run 末尾字符），避免影响前序索引
    end_r_el = text_runs[end_run_idx]
    end_full_text = run_texts[end_run_idx]
    if end_char_idx < len(end_full_text) - 1:
        left_text = end_full_text[:end_char_idx + 1]
        right_text = end_full_text[end_char_idx + 1:]
        _set_run_text(end_r_el, left_text)
        run_texts[end_run_idx] = left_text
        right_r_el = _clone_run_with_text(end_r_el, right_text)
        end_r_el.addnext(right_r_el)

    # 2. 再切分起始 Run（若匹配起点不在该 Run 首字符）
    start_r_el = text_runs[start_run_idx]
    start_full_text = run_texts[start_run_idx]
    if start_char_idx > 0:
        prefix_text = start_full_text[:start_char_idx]
        target_text = start_full_text[start_char_idx:]
        prefix_r_el = _clone_run_with_text(start_r_el, prefix_text)
        start_r_el.addprevious(prefix_r_el)
        _set_run_text(start_r_el, target_text)
        run_texts[start_run_idx] = target_text

    # 3. 在首 Run 前插 commentRangeStart，末 Run 后插 commentRangeEnd 与 commentReference
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
    for child in root.findall(f"{{{_CT_NS}}}Override"):
        if child.get("PartName") == part_name:
            return ct_bytes
    override = ET.SubElement(root, f"{{{_CT_NS}}}Override")
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

    # 校验源文件是否为合法的可打开 .docx
    try:
        doc = docx.Document(io.BytesIO(src_bytes))
    except Exception as e:
        raise RuntimeError(f"无法解析 .docx 文件（格式损坏或非法）：{src_docx_path}: {e}") from e

    # 空 findings 列表：合法空操作，原样返回源文件 bytes
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

        # 第一遍：全段落精确归一化子串匹配
        if norm_q:
            for p in all_paragraphs:
                if _try_attach_quote_in_paragraph(p, norm_q, comment_id, allow_longest_submatch=False):
                    attached = True
                    break

        # 第二遍：若 quote 跨行或含局部省略，尝试最长连续公共子块匹配
        if not attached and norm_q:
            for p in all_paragraphs:
                if _try_attach_quote_in_paragraph(p, norm_q, comment_id, allow_longest_submatch=True):
                    attached = True
                    break

        # 第三遍：未命中任何段落时绝不丢失，挂载到文档首段并加 [全篇] 标记
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

    # 先将注入了 commentRangeStart/End/commentReference 的文档存入内存缓冲
    mid_buf = io.BytesIO()
    doc.save(mid_buf)
    mid_bytes = mid_buf.getvalue()

    # 重打包 ZIP：注入 word/comments.xml，更新 [Content_Types].xml 与 word/_rels/document.xml.rels
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


# ---------------------------------------------------------------- PDF 高亮批注实现
def _search_quote_in_pdf(pdf_doc, quote: str) -> list[tuple[int, list]]:
    """在 PDF 各页中检索 quote，返回 [(page_index, [Rect, ...]), ...]。
    若原串未命中，按规格使用 normalize_zh 截取首尾 20 字符分片重试一次。
    """
    raw_q = (quote or '').strip()
    if not raw_q:
        return []

    hits = []
    for page_idx in range(len(pdf_doc)):
        page = pdf_doc[page_idx]
        rects = page.search_for(raw_q)
        if rects:
            hits.append((page_idx, rects))
    if hits:
        return hits

    # 归一化容错重试：原样与空白折叠（多空格转单空格）的首尾 20 字符分片。
    # 注意：不可用 normalize_zh 去空白后的串做英文分片——去空格后
    # （如 'Penaltyshallbe50%oft'）永远无法命中带空格的 PDF 原文；
    # 中文无空格不受影响，两套分片对中文等价。
    fold_q = re.sub(r'\s+', ' ', raw_q).strip()
    fragments = []
    for candidate in (
        raw_q[:20],
        raw_q[-20:] if len(raw_q) > 20 else '',
        fold_q[:20],
        fold_q[-20:] if len(fold_q) > 20 else '',
    ):
        c = candidate.strip()
        if c and len(c) >= 2 and c not in fragments:
            fragments.append(c)

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

    return hits


# ── T46 扫描件双路线 Redline 支持（路线 A 夹心层 + 路线 B 直接高亮兜底）───────
def _apply_sandwich_layer(pdf_doc, ocr_pages_data: list[dict]):
    """路线 A：将 OCR 识别的文本按 bbox 写入透明夹心层（Sandwich PDF）。

    使用 render_mode=3（完全透明文本）+ 内置中文字体 'china-s'，
    使图片式扫描件成为原生可搜索 PDF，现有 search_for 链路可无缝命中。
    写入后在内存中重新打开文档，固化内容流与搜索索引。
    """
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
            # T46 修复：insert_textbox 在文本装不下矩形时返回负数且【不抛异常】
            # （原 except 兜底永不触发 → 零写入）。改为：字号逐步收缩重试，
            # 仍装不下再退化为 insert_text（从矩形左下方写入，不检查容纳性）。
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

    # 重新在内存中载入，固化内容流并建立完整的文本搜索索引
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
            if not norm_line:
                continue

            # T46 验收修正：OCR 在表格/签名区产出大量碎片段（"年"/"月"/短单元格），
            # 旧门槛（任意子串命中 / 4 字公共子串）导致批注错位（用户实测：
            # 质保期风险被锚到表格、签名区堆图标）。收紧为：片段 ≥8 字且
            # 公共子串 ≥8 字——中文法律文本中 8 字连续重合基本可断定同源。
            if len(norm_line) < 8:
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
    """在原版 .pdf 合同中按 Finding.quote 检索文本坐标，注入高危红/中危橙/
    低危黄高亮标注与弹窗批注内容，输出带批注的 PDF 副本。
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

    global_fallback_count = 0
    author_name = author or 'AI法务初审员'

    # 扫描件判定与夹心层预处理（与 core.parser OCR 判据保持严格一致）
    total_chars = sum(len(p.get_text().strip()) for p in pdf_doc)
    max_page_chars = max((len(p.get_text().strip()) for p in pdf_doc), default=0)
    is_scanned = (total_chars < OCR_MIN_CHARS and max_page_chars < 20)
    ocr_data = []

    if is_scanned:
        try:
            ocr_data = get_pdf_ocr_data(src_pdf_path)
            if ocr_data:
                # 路线 A：构建可搜索夹心层（Sandwich PDF）
                pdf_doc = _apply_sandwich_layer(pdf_doc, ocr_data)
        except Exception:
            pass

    try:
        for finding in findings:
            sev = getattr(finding, 'severity', 'low') or 'low'
            color = _SEVERITY_COLORS.get(sev, (1.0, 1.0, 0.0))

            if _is_global_finding(finding):
                hits = []
            else:
                hits = _search_quote_in_pdf(pdf_doc, finding.quote)
                # 路线 B 兜底：若 search_for 未命中，但具有 OCR bbox 数据，尝试模糊 bbox 定位
                if not hits and is_scanned and ocr_data:
                    hits = _match_ocr_boxes_fallback(ocr_data, finding.quote, pdf_doc)

            if hits:
                comment_text = _format_comment_text(finding, is_global=False)
                for page_idx, rects in hits:
                    page = pdf_doc[page_idx]
                    for rect in rects:
                        annot = page.add_highlight_annot(rect)
                        annot.set_colors(stroke=color)
                        annot.set_info(title=author_name, content=comment_text)
                        annot.update()
            else:
                # 绝不丢失：在首页顶部插入汇总 Text Annotation 图标，正文前置 [全篇]
                comment_text = _format_comment_text(finding, is_global=True)
                page0 = pdf_doc[0]
                x_pos = min(36 + global_fallback_count * 26, max(36, int(page0.rect.width) - 40))
                pt = pymupdf.Point(x_pos, 24)
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