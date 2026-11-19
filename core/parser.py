# -*- coding: utf-8 -*-
"""T1 / E1 / E3 / E6 文档解析模块（含 PDF 扫描件 OCR、docx 内嵌证照图片就近 OCR、PDF 印刷页码映射与 docx 页码双轨定位）。

实现规格（签名不得修改）：
    parse_document(file_path) -> ParsedDoc
    build_pdf_page_spans(file_path) -> list[tuple[int, int, int]]
    offset_to_page(offset, page_spans) -> int | None
    build_docx_page_spans(file_path) -> list[tuple[int, int, int, int | None]]
    offset_to_docx_page(offset, spans) -> dict | None
"""
import bisect
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import threading
from typing import Callable, Optional
import zipfile

import lxml.etree as ET

from core.interfaces import ParsedDoc

SUPPORTED = {'.docx', '.pdf', '.txt', '.md', '.png', '.jpg', '.jpeg'}
_IMAGE_EXTS = {'.png', '.jpg', '.jpeg'}

# ── T29 扫描件离线 OCR（RapidOCR + pymupdf 渲染）────────────────────────
OCR_MIN_CHARS = 100

# ── E1 升级点 2：docx 内嵌证照图片双重过滤阈值 ──────────────────────────
_MEDIA_MIN_BYTES = 20 * 1024     # 字节数下限：> 20KB（20480 bytes）
_MEDIA_MIN_DIM_PX = 200          # 宽高像素下限：width >= 200 且 height >= 200
_MEDIA_MAX_ASPECT_RATIO = 6.0    # 长宽比范围：1:6 ~ 6:1

_REL_EMBED_RE = re.compile(r'(?:r:embed|r:id)="([^"]+)"')

# ── E6 OpenXML 命名空间常量 ──────────────────────────────────────────────
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_REL_PKG_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_W_P = f"{{{_W_NS}}}p"
_W_TBL = f"{{{_W_NS}}}tbl"
_W_TR = f"{{{_W_NS}}}tr"
_W_TC = f"{{{_W_NS}}}tc"
_W_TCPR = f"{{{_W_NS}}}tcPr"
_W_VMERGE = f"{{{_W_NS}}}vMerge"
_W_GRIDSPAN = f"{{{_W_NS}}}gridSpan"
_W_PPR = f"{{{_W_NS}}}pPr"
_W_SECTPR = f"{{{_W_NS}}}sectPr"
_W_PGNUMTYPE = f"{{{_W_NS}}}pgNumType"
_W_FOOTERREF = f"{{{_W_NS}}}footerReference"
_W_R = f"{{{_W_NS}}}r"
_W_T = f"{{{_W_NS}}}t"
_W_TAB = f"{{{_W_NS}}}tab"
_W_BR = f"{{{_W_NS}}}br"
_W_CR = f"{{{_W_NS}}}cr"
_W_LAST_RENDERED_PB = f"{{{_W_NS}}}lastRenderedPageBreak"
_W_FLDSIMPLE = f"{{{_W_NS}}}fldSimple"
_W_INSTRTEXT = f"{{{_W_NS}}}instrText"
_W_VAL = f"{{{_W_NS}}}val"
_W_TYPE = f"{{{_W_NS}}}type"
_W_START = f"{{{_W_NS}}}start"
_W_FMT = f"{{{_W_NS}}}fmt"
_W_INSTR = f"{{{_W_NS}}}instr"
_R_ID = f"{{{_R_NS}}}id"

_PAGE_FIELD_RE = re.compile(r'\bPAGE\b', re.IGNORECASE)

_OCR_ENGINE = None
_OCR_LOCK = threading.Lock()


def _resolve_ocr_thread_limit() -> int:
    """限制 RapidOCR 单实例 ONNX 线程数，避免多核宿主机上 100% 占满全部 CPU。"""
    raw = (os.getenv('BIDGUARD_OCR_THREADS') or '').strip()
    if raw.isdigit():
        return max(1, min(16, int(raw)))
    cpu_cnt = os.cpu_count() or 2
    return max(1, min(4, cpu_cnt))


def _get_ocr_engine():
    """懒加载并单例化 RapidOCR 引擎（模型初始化数秒，绝不重复加载）。"""
    global _OCR_ENGINE
    with _OCR_LOCK:
        if _OCR_ENGINE is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError as e:
                raise RuntimeError(
                    '未安装 OCR 引擎，无法识别扫描件：请在项目环境执行 '
                    'pip install rapidocr-onnxruntime'
                ) from e
            ocr_threads = _resolve_ocr_thread_limit()
            try:
                _OCR_ENGINE = RapidOCR(
                    intra_op_num_threads=ocr_threads,
                    inter_op_num_threads=1,
                )
            except TypeError:
                _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE


def _ocr_cache_path(pdf_path: Path) -> Path:
    """OCR 结果伴生缓存：<原文件路径>.ocr.txt（与源文件同目录）。"""
    return Path(str(pdf_path) + '.ocr.txt')


def _ocr_json_cache_path(pdf_path: Path) -> Path:
    """T46 OCR 结构化结果（含 bbox）伴生缓存：<原文件路径>.ocr.json。"""
    return Path(str(pdf_path) + '.ocr.json')


def _docx_media_ocr_cache_path(docx_path: Path) -> Path:
    """E1 docx 内嵌图片 OCR 伴生缓存：<原文件路径>.media.ocr.json（与 docx 同目录）。"""
    return Path(str(docx_path) + '.media.ocr.json')


def _resolve_pdf_page_number(page, physical_index_0based: int) -> int:
    """E3 §2.2：优先读取 PDF 页的印刷页码 page.get_label()；
    若 label 为正整数（如 '1', '15'）则采用该印刷页码，否则回退为 1-based 物理页号。
    """
    try:
        label_fn = getattr(page, 'get_label', None)
        if callable(label_fn):
            lbl = str(label_fn() or '').strip()
            if lbl.isdigit():
                val = int(lbl)
                if val > 0:
                    return val
            m = re.match(r'^\s*(\d+)\s*$', lbl)
            if m:
                val = int(m.group(1))
                if val > 0:
                    return val
    except Exception:
        pass
    return physical_index_0based + 1


def build_pdf_page_spans(file_path: str | Path | None) -> list[tuple[int, int, int]]:
    """E3 §2.2：若输入为 PDF 文件，计算每页在 parse_document 拼接文本中的 (start_char, end_char, page_no)。
    优先采用 page.get_label() 印刷页码，无有效 label 时回退物理页号；docx 或非 PDF 一律返回 []。
    """
    if not file_path:
        return []
    path = Path(file_path)
    if not path.is_file() or path.suffix.lower() != '.pdf':
        return []
    try:
        import pymupdf
        spans: list[tuple[int, int, int]] = []
        cursor = 0
        with pymupdf.open(str(path)) as pdf_doc:
            for idx, page in enumerate(pdf_doc):
                ptxt = page.get_text()
                end = cursor + len(ptxt)
                page_no = _resolve_pdf_page_number(page, idx)
                spans.append((cursor, end, page_no))
                cursor = end + 1  # parse_document 使用 '\n'.join(text_parts)
        return spans
    except Exception:
        return []


def offset_to_page(offset: int | None, page_spans: list[tuple[int, int, int]]) -> int | None:
    """E3 §2.2：将字符偏移量映射为 PDF 页码；docx 或空 page_spans 恒返回 None（绝不编造页码）。"""
    if offset is None or offset < 0 or not page_spans:
        return None
    for lo, hi, page_no in page_spans:
        if lo <= offset <= hi:
            return page_no
    return page_spans[-1][2] if offset >= page_spans[-1][0] else None


# ── E6 · Word (.docx) 页锚点双轨解析（zipfile + lxml 直读，零 python-docx 抽象丢失）────

def _footer_xml_has_page_field(xml_bytes: bytes) -> bool:
    """检查 word/footer*.xml 是否包含 PAGE 页码域（w:fldSimple[@w:instr] 或 w:instrText）。"""
    if not xml_bytes:
        return False
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return False

    for fld in root.iter(_W_FLDSIMPLE):
        instr = fld.get(_W_INSTR) or fld.get('instr') or ''
        if _PAGE_FIELD_RE.search(instr):
            return True

    instr_chunks: list[str] = []
    for it in root.iter(_W_INSTRTEXT):
        if it.text:
            instr_chunks.append(it.text)
    if instr_chunks and _PAGE_FIELD_RE.search(''.join(instr_chunks)):
        return True

    return False


def _load_docx_rels_and_footers(zf: zipfile.ZipFile) -> tuple[dict[str, str], dict[str, bool]]:
    """从 docx zip 包读取 word/_rels/document.xml.rels，返回：
    - rel_target_map: rId -> zip 内规范路径（如 'word/footer1.xml' 或 'word/media/image1.png'）
    - footer_has_page_map: rId -> 该页脚是否包含 PAGE 域布尔值
    """
    rel_target_map: dict[str, str] = {}
    footer_has_page_map: dict[str, bool] = {}

    rels_path = 'word/_rels/document.xml.rels'
    if rels_path not in zf.namelist():
        return rel_target_map, footer_has_page_map

    try:
        rels_root = ET.fromstring(zf.read(rels_path))
    except Exception:
        return rel_target_map, footer_has_page_map

    names_set = set(zf.namelist())
    for rel in rels_root.iter(f"{{{_REL_PKG_NS}}}Relationship"):
        rid = rel.get('Id')
        target = (rel.get('Target') or '').strip()
        if not rid or not target:
            continue
        norm_target = target.lstrip('/')
        if not norm_target.startswith('word/'):
            norm_target = f"word/{norm_target.lstrip('./')}"
        rel_target_map[rid] = norm_target

        if 'footer' in norm_target.lower() and norm_target in names_set:
            try:
                footer_has_page_map[rid] = _footer_xml_has_page_field(zf.read(norm_target))
            except Exception:
                footer_has_page_map[rid] = False

    return rel_target_map, footer_has_page_map


def _parse_sectpr_config(
    sect_el: ET._Element,
    footer_has_page_map: dict[str, bool],
    inherited_has_footer: bool = False,
) -> dict[str, Optional[int | bool]]:
    """解析单个 <w:sectPr> 节点的页码配置：
    - start_num: int | None（若设置了正整数 w:start="N" 则返回 N，否则 None 表示延续前节）
    - fmt_decimal: bool（w:fmt 缺省或为 'decimal' 时 True，roman/letter 等其他格式为 False）
    - has_page_footer: bool（本节挂载了含 PAGE 域的 footerReference，或未显式声明 footerReference 时继承前节）
    """
    start_num: Optional[int] = None
    fmt_decimal = True

    pg_num_el = sect_el.find(_W_PGNUMTYPE)
    has_pgnum_tag = pg_num_el is not None
    if pg_num_el is not None:
        raw_fmt = (pg_num_el.get(_W_FMT) or pg_num_el.get('fmt') or '').strip()
        if raw_fmt and raw_fmt.lower() != 'decimal':
            fmt_decimal = False
        raw_start = (pg_num_el.get(_W_START) or pg_num_el.get('start') or '').strip()
        if raw_start.isdigit() and int(raw_start) > 0:
            start_num = int(raw_start)

    footer_refs = sect_el.findall(_W_FOOTERREF)
    if footer_refs:
        has_page_footer = any(
            footer_has_page_map.get(ref.get(_R_ID) or ref.get('id') or '', False)
            for ref in footer_refs
        )
    else:
        has_page_footer = inherited_has_footer

    return {
        'start_num': start_num,
        'fmt_decimal': fmt_decimal,
        'has_page_footer': has_page_footer,
        'has_pgnum_tag': has_pgnum_tag,
    }


def _extract_p_text_and_breaks_lxml(p_el: ET._Element) -> tuple[str, list[int]]:
    """用 lxml 严格复刻 python-docx 的 Paragraph.text 拼接逻辑，并记录段内每个分页锚点
    （<w:lastRenderedPageBreak/> 或 <w:br w:type="page"/>）在该段落 raw text 中的字符位置。
    """
    chars: list[str] = []
    break_raw_positions: list[int] = []

    for elem in p_el.iter():
        tag = elem.tag
        if tag == _W_LAST_RENDERED_PB:
            break_raw_positions.append(len(chars))
        elif tag == _W_BR:
            br_type = (elem.get(_W_TYPE) or elem.get('type') or '').strip().lower()
            if br_type == 'page':
                break_raw_positions.append(len(chars))
            elif br_type in ('', 'textwrapping'):
                chars.append('\n')
        elif tag == _W_CR:
            chars.append('\n')
        elif tag == _W_TAB:
            chars.append('\t')
        elif tag == _W_T:
            if elem.text:
                chars.append(elem.text)

    raw_text = ''.join(chars)
    stripped = raw_text.strip()
    if not stripped:
        return '', [0] * len(break_raw_positions)

    leading_ws = len(raw_text) - len(raw_text.lstrip())
    stripped_len = len(stripped)
    mapped_breaks: list[int] = []
    for pos in break_raw_positions:
        adj = max(0, min(stripped_len, pos - leading_ws))
        mapped_breaks.append(adj)

    return stripped, mapped_breaks


def _extract_table_unique_tcs_lxml(tbl_el: ET._Element) -> list[ET._Element]:
    """用 lxml 严格复刻 python-docx 遍历 table.rows -> row.cells 并按 _tc 去重的顺序（兼容 gridSpan 与 vMerge 跨行合并）。"""
    rows_els = tbl_el.findall(_W_TR)
    if not rows_els:
        return []

    # 跟踪每一列当前的上方单元格元素（用于复刻 vMerge continue 指向同一 _tc 的去重行为）
    col_active_tc: dict[int, ET._Element] = {}
    unique_tcs: list[ET._Element] = []
    seen_tc_objs: set[ET._Element] = set()

    for tr_el in rows_els:
        col_idx = 0
        for tc_el in tr_el.findall(_W_TC):
            tc_pr = tc_el.find(_W_TCPR)
            grid_span = 1
            is_vmerge_continue = False

            if tc_pr is not None:
                gs_el = tc_pr.find(_W_GRIDSPAN)
                if gs_el is not None:
                    gs_val = (gs_el.get(_W_VAL) or gs_el.get('val') or '1').strip()
                    if gs_val.isdigit() and int(gs_val) > 1:
                        grid_span = int(gs_val)
                vm_el = tc_pr.find(_W_VMERGE)
                if vm_el is not None:
                    vm_val = (vm_el.get(_W_VAL) or vm_el.get('val') or '').strip().lower()
                    if vm_val != 'restart':
                        is_vmerge_continue = True

            if is_vmerge_continue and col_idx in col_active_tc:
                effective_tc = col_active_tc[col_idx]
            else:
                effective_tc = tc_el
                for c_offset in range(grid_span):
                    col_active_tc[col_idx + c_offset] = tc_el

            if effective_tc not in seen_tc_objs:
                seen_tc_objs.add(effective_tc)
                unique_tcs.append(effective_tc)

            col_idx += grid_span

    return unique_tcs


def _extract_tc_text_and_breaks_lxml(tc_el: ET._Element) -> tuple[str, list[int]]:
    """用 lxml 严格复刻 python-docx _Cell.text.strip() 拼接逻辑，并记录单元格内分页锚点的相对字符偏移。"""
    # 注意：仅取直接子级 <w:p>（与 python-docx cell.paragraphs 一致）
    p_els = tc_el.findall(_W_P)
    raw_p_texts: list[str] = []
    raw_breaks: list[int] = []
    cursor = 0

    for p_idx, p_el in enumerate(p_els):
        chars: list[str] = []
        for elem in p_el.iter():
            tag = elem.tag
            if tag == _W_LAST_RENDERED_PB:
                raw_breaks.append(cursor + len(chars))
            elif tag == _W_BR:
                br_type = (elem.get(_W_TYPE) or elem.get('type') or '').strip().lower()
                if br_type == 'page':
                    raw_breaks.append(cursor + len(chars))
                elif br_type in ('', 'textwrapping'):
                    chars.append('\n')
            elif tag == _W_CR:
                chars.append('\n')
            elif tag == _W_TAB:
                chars.append('\t')
            elif tag == _W_T and elem.text:
                chars.append(elem.text)

        p_raw = ''.join(chars)
        raw_p_texts.append(p_raw)
        cursor += len(p_raw) + (1 if p_idx < len(p_els) - 1 else 0)

    cell_raw = '\n'.join(raw_p_texts)
    stripped = cell_raw.strip()
    if not stripped:
        return '', [0] * len(raw_breaks)

    leading_ws = len(cell_raw) - len(cell_raw.lstrip())
    stripped_len = len(stripped)
    mapped = [max(0, min(stripped_len, b - leading_ws)) for b in raw_breaks]
    return stripped, mapped


class _DocxMediaOcrSession:
    """共享 docx 内嵌图片 OCR 状态管理器（统一缓存、进度回调、增量落盘与 ocr_images 开关）。"""

    def __init__(
        self,
        doc_path: Path,
        doc,
        *,
        ocr_images: bool,
        progress_cb: Optional[Callable[[int, int, int], None]] = None,
    ) -> None:
        self.ocr_images = bool(ocr_images)
        self.progress_cb = progress_cb
        self.related_parts = getattr(doc.part, 'related_parts', {}) or {}
        self.media_cache_path = _docx_media_ocr_cache_path(doc_path)
        self.media_cache_map = _load_docx_media_cache(self.media_cache_path) if self.ocr_images else {}
        self.seen_media_sha1s: set[str] = set()
        self.cache_dirty = False
        self.ocr_skipped_any = False
        self.total_uncached = 0
        self.cached_hits = 0
        self.uncached_done = 0
        self.unflushed_new = 0

        if self.ocr_images and (self.progress_cb is not None or self.related_parts):
            self.total_uncached, self.cached_hits = _count_docx_media_targets(
                doc, self.related_parts, self.media_cache_map
            )
            if self.progress_cb is not None and (self.total_uncached > 0 or self.cached_hits > 0):
                try:
                    self.progress_cb(0, self.total_uncached, self.cached_hits)
                except Exception:
                    pass

    def _on_new_ocr(self) -> None:
        self.uncached_done += 1
        self.unflushed_new += 1
        if self.unflushed_new >= _DOCX_MEDIA_INCREMENTAL_FLUSH:
            _save_docx_media_cache(self.media_cache_path, self.media_cache_map)
            self.unflushed_new = 0
            self.cache_dirty = False
        if self.progress_cb is not None:
            self.progress_cb(self.uncached_done, self.total_uncached, self.cached_hits)

    def extract(self, xml_str: str, para_idx: int) -> list[str]:
        if not self.ocr_images:
            return []
        img_texts, dirty, skipped = _extract_xml_embedded_media_texts(
            xml_str,
            self.related_parts,
            self.media_cache_map,
            para_idx,
            self.seen_media_sha1s,
            on_new_ocr=self._on_new_ocr,
        )
        if dirty:
            self.cache_dirty = True
        if skipped:
            self.ocr_skipped_any = True
        return img_texts

    def flush(self) -> None:
        if self.ocr_images and (self.cache_dirty or self.unflushed_new > 0):
            _save_docx_media_cache(self.media_cache_path, self.media_cache_map)
            self.cache_dirty = False
            self.unflushed_new = 0


def _parse_docx_with_page_spans_internal(
    file_path: str | Path,
    *,
    ocr_images: bool = True,
    progress_cb: Optional[Callable[[int, int, int], None]] = None,
) -> tuple[str, list[tuple[int, int, int, Optional[int]]], bool, int]:
    """用 zipfile + lxml 单趟解析 docx，同时产出：
    1. full_text（与 parse_document 逐字节 100% SHA256 等值）；
    2. page_spans: [(start_offset, end_offset, physical_page, footer_page), ...]；
    3. ocr_skipped_any: bool；
    4. media_ocr_count: int。
    """
    path = Path(file_path)

    # ── 权威偏移空间 = python-docx 部件序列（与 parse_document 拼接严格同构）──
    # E6 验收修正：lxml 单独重建全文无法对特殊构造（文本框/超链接/域字符/修订标记）
    # 逐字节复刻 python-docx 的取文逻辑——实测 4 份真实标书有 1 份尾部漂移 10 字符。
    # 改为：python-docx 部件为偏移权威（offsets 天然落在 parse_document 空间，零漂移），
    # lxml 只负责提供段内分页锚点（lastRenderedPageBreak / w:br page）的位置。
    with zipfile.ZipFile(str(path), 'r') as zf:
        if 'word/document.xml' not in zf.namelist():
            raise RuntimeError(f"非法的 docx 文件（缺少 word/document.xml）：{file_path}")
        rel_target_map, footer_has_page_map = _load_docx_rels_and_footers(zf)

    from docx import Document
    doc = Document(str(path))
    media_session = _DocxMediaOcrSession(
        path,
        doc,
        ocr_images=ocr_images,
        progress_cb=progress_cb,
    )

    top_p_els = [para._p for para in doc.paragraphs]
    top_tbl_els = [table._tbl for table in doc.tables]
    body_el = doc.element.body

    # 收集所有节定义（段内 pPr/sectPr 按顺序管辖各前序节，body/sectPr 管辖最后一节）
    section_configs: list[dict[str, Optional[int | bool]]] = []
    inherited_footer = False
    for p_el in top_p_els:
        ppr = p_el.find(_W_PPR)
        sect_el = ppr.find(_W_SECTPR) if ppr is not None else None
        if sect_el is not None:
            cfg = _parse_sectpr_config(sect_el, footer_has_page_map, inherited_footer)
            inherited_footer = bool(cfg['has_page_footer'])
            section_configs.append(cfg)

    body_sect_el = body_el.find(_W_SECTPR)
    if body_sect_el is not None:
        cfg = _parse_sectpr_config(body_sect_el, footer_has_page_map, inherited_footer)
        section_configs.append(cfg)
    elif not section_configs:
        section_configs.append({
            'start_num': None,
            'fmt_decimal': True,
            'has_page_footer': False,
            'has_pgnum_tag': False,
        })

    # ── 组装：部件序列与分页锚点事件（偏移全部落在 python-docx 权威空间）──
    text_parts: list[str] = []
    # events: (char_offset_in_full_text, 'page_break' | 'section_break')，偏移即 parse_document 空间
    events: list[tuple[int, str]] = []
    current_offset = 0
    has_any_anchor = False

    def _append_part(part_str: str, inner_breaks: list[int]) -> None:
        nonlocal current_offset, has_any_anchor
        if text_parts:
            current_offset += 1  # '\n' 分隔符
        base_off = current_offset
        for b_pos in inner_breaks:
            events.append((base_off + min(b_pos, len(part_str)), 'page_break'))
            has_any_anchor = True
        text_parts.append(part_str)
        current_offset += len(part_str)

    def _boundary_event(ev_type: str) -> None:
        nonlocal has_any_anchor
        events.append((current_offset, ev_type))
        has_any_anchor = True

    try:
        # 阶段 1：顶层段落（python-docx 与 lxml 元素同序一对一配对）
        for p_idx, para in enumerate(doc.paragraphs):
            p_el = top_p_els[p_idx]
            _p_text, p_breaks = _extract_p_text_and_breaks_lxml(p_el)
            pdx_text = para.text.strip()
            xml_str = getattr(para._p, 'xml', '') or ''
            img_texts = media_session.extract(xml_str, p_idx)
            if pdx_text:
                _append_part(pdx_text, p_breaks)
            elif p_breaks:
                for _ in p_breaks:
                    _boundary_event('page_break')
            for itxt in img_texts:
                if itxt:
                    _append_part(itxt, [])

            ppr = p_el.find(_W_PPR)
            if ppr is not None and ppr.find(_W_SECTPR) is not None:
                _boundary_event('section_break')

        # 阶段 2：顶层表格（逐表配对；唯一 tc 序列复刻 python-docx row.cells 的 gridSpan/vMerge 去重序）
        for table in doc.tables:
            tbl_el = table._tbl
            tc_els = _extract_table_unique_tcs_lxml(tbl_el)
            seen_tcs: set = set()
            pdx_cells = []
            for row in table.rows:
                for cell in row.cells:
                    tc = cell._tc
                    if tc in seen_tcs:
                        continue
                    seen_tcs.add(tc)
                    pdx_cells.append(cell)
            if len(pdx_cells) != len(tc_els):
                # 配对失败：本表放弃锚点（部件照常拼接保证全文等值）——绝不乱锚
                for cell in pdx_cells:
                    txt = cell.text.strip()
                    if txt:
                        _append_part(txt, [])
                    xml_str = getattr(cell._tc, 'xml', '') or ''
                    for itxt in media_session.extract(xml_str, -1):
                        if itxt:
                            _append_part(itxt, [])
                continue
            for cell, tc_el in zip(pdx_cells, tc_els):
                _tc_text, tc_breaks = _extract_tc_text_and_breaks_lxml(tc_el)
                pdx_text = cell.text.strip()
                xml_str = getattr(cell._tc, 'xml', '') or ''
                img_texts = media_session.extract(xml_str, -1)
                if pdx_text:
                    _append_part(pdx_text, tc_breaks)
                elif tc_breaks:
                    for _ in tc_breaks:
                        _boundary_event('page_break')
                for itxt in img_texts:
                    if itxt:
                        _append_part(itxt, [])
    except BaseException:
        media_session.flush()
        raise

    media_session.flush()

    full_text = '\n'.join(text_parts)
    total_len = len(full_text)

    # 检查是否存在任何有效分页锚点或页脚页码设置
    any_footer_or_pgnum = any(
        bool(cfg.get('has_page_footer')) or bool(cfg.get('has_pgnum_tag'))
        for cfg in section_configs
    )
    if not has_any_anchor and not any_footer_or_pgnum:
        # §2.5 绝不编造：纯机器生成无任何分页锚点与页脚的 docx 返回空 spans
        return full_text, [], media_session.ocr_skipped_any, len(media_session.seen_media_sha1s)

    # ── 将事件序列转化为连续无缝的 (start_offset, end_offset, physical_page, footer_page) 区间 ──
    sec_idx = 0
    phys_page = 1
    first_cfg = section_configs[0]
    printed_counter = int(first_cfg['start_num']) if first_cfg.get('start_num') is not None else 1

    def _current_footer_page(s_i: int, p_counter: int) -> Optional[int]:
        cfg = section_configs[min(s_i, len(section_configs) - 1)]
        if not cfg.get('has_page_footer') or not cfg.get('fmt_decimal', True):
            return None
        return p_counter

    spans: list[tuple[int, int, int, Optional[int]]] = []
    seg_start = 0

    for ev_offset, ev_type in events:
        clamped_off = max(seg_start, min(total_len, ev_offset))
        spans.append((
            seg_start,
            clamped_off,
            phys_page,
            _current_footer_page(sec_idx, printed_counter),
        ))
        seg_start = clamped_off
        phys_page += 1

        if ev_type == 'section_break':
            sec_idx = min(sec_idx + 1, len(section_configs) - 1)
            next_cfg = section_configs[sec_idx]
            if next_cfg.get('start_num') is not None:
                printed_counter = int(next_cfg['start_num'])
            else:
                printed_counter += 1
        else:
            printed_counter += 1

    spans.append((
        seg_start,
        total_len,
        phys_page,
        _current_footer_page(sec_idx, printed_counter),
    ))

    # 合并因段首连续换页产生的零长度空区间（保留最新页码）
    cleaned_spans: list[tuple[int, int, int, Optional[int]]] = []
    for sp in spans:
        if cleaned_spans and sp[0] == cleaned_spans[-1][0] and cleaned_spans[-1][0] == cleaned_spans[-1][1]:
            cleaned_spans[-1] = sp
        else:
            cleaned_spans.append(sp)

    return full_text, cleaned_spans, media_session.ocr_skipped_any, len(media_session.seen_media_sha1s)


def build_docx_page_spans(
    file_path: str | Path | None,
    *,
    ocr_images: bool = True,
    progress_cb: Optional[Callable[[int, int, int], None]] = None,
) -> list[tuple[int, int, int, int | None]]:
    """E6 §1 & §2：解析 .docx 文件的物理页与底部印刷页码区间。

    Returns:
        [(start_offset, end_offset, physical_page, footer_page), ...]
        按 start_offset 升序且区间连续无缝；
        若非 docx 或整份文档无任何分页锚点（纯机器生成 docx），返回 []。
    """
    if not file_path:
        return []
    path = Path(file_path)
    if not path.is_file() or path.suffix.lower() != '.docx':
        return []
    try:
        _text, spans, _skipped, _cnt = _parse_docx_with_page_spans_internal(
            path, ocr_images=ocr_images, progress_cb=progress_cb
        )
        return spans
    except Exception:
        return []


def offset_to_docx_page(
    offset: int | None,
    spans: list[tuple[int, int, int, int | None]],
) -> dict | None:
    """E6 §1 & §2.4：用二分查找将字符偏移量映射为 {'physical': X, 'footer': Y | None}。
    未命中或 spans 为空时诚实返回 None。
    """
    if offset is None or offset < 0 or not spans:
        return None

    starts = [s[0] for s in spans]
    idx = bisect.bisect_right(starts, offset) - 1
    if idx < 0:
        idx = 0
    lo, hi, phys, footer = spans[idx]
    if lo <= offset <= hi or idx == len(spans) - 1:
        return {'physical': phys, 'footer': footer}
    return None


# ── 图片尺寸与 PDF OCR 既有实现（一字不动）──────────────────────────────────

def _get_image_dimensions(img_bytes: bytes) -> tuple[int, int] | None:
    """解析图片字节流的 (width, height) 像素尺寸（标准库 PNG/JPEG 头快速解析 + PyMuPDF 兜底）。"""
    if not img_bytes or len(img_bytes) < 24:
        return None

    if img_bytes[:8] == b'\x89PNG\r\n\x1a\n' and img_bytes[12:16] == b'IHDR':
        w, h = struct.unpack('>II', img_bytes[16:24])
        return int(w), int(h)

    if img_bytes[:2] == b'\xff\xd8':
        idx = 2
        length = len(img_bytes)
        while idx + 9 < length:
            if img_bytes[idx] != 0xFF:
                idx += 1
                continue
            marker = img_bytes[idx + 1]
            if marker == 0xFF:
                idx += 1
                continue
            if marker in (0xD8, 0xD9) or (0xD0 <= marker <= 0xD7):
                idx += 2
                continue
            seg_len = struct.unpack('>H', img_bytes[idx + 2:idx + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack('>HH', img_bytes[idx + 5:idx + 9])
                return int(w), int(h)
            if seg_len < 2:
                break
            idx += 2 + seg_len

    try:
        import pymupdf
        pix = pymupdf.Pixmap(img_bytes)
        return int(pix.width), int(pix.height)
    except Exception:
        return None


def _passes_media_filter(img_bytes: bytes) -> bool:
    """E1 双重尺寸过滤：字节数 > 20KB、宽高均 >= 200px、长宽比在 1:6 ~ 6:1 之间。"""
    if not img_bytes or len(img_bytes) <= _MEDIA_MIN_BYTES:
        return False
    dims = _get_image_dimensions(img_bytes)
    if dims is None:
        return False
    w, h = dims
    if w < _MEDIA_MIN_DIM_PX or h < _MEDIA_MIN_DIM_PX:
        return False
    ratio = w / h if h > 0 else 999.0
    if ratio < (1.0 / _MEDIA_MAX_ASPECT_RATIO) or ratio > _MEDIA_MAX_ASPECT_RATIO:
        return False
    return True


def _ocr_single_image_bytes(img_bytes: bytes) -> tuple[str, bool]:
    """对单张图片字节流执行 RapidOCR 识别，返回 (ocr_text, engine_missing_skipped)。"""
    try:
        engine = _get_ocr_engine()
    except RuntimeError:
        return '', True

    try:
        import numpy as np
        import pymupdf
        pix = pymupdf.Pixmap(img_bytes)
        if pix.alpha:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        elif pix.n not in (1, 3):
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)

        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if pix.n == 1:
            img_bgr = np.repeat(arr, 3, axis=2)
        else:
            img_bgr = np.ascontiguousarray(arr[:, :, ::-1])

        result, _ = engine(img_bgr)
        lines = []
        if result:
            for item in result:
                if len(item) >= 2 and item[1]:
                    txt = str(item[1]).strip()
                    if txt:
                        lines.append(txt)
        return '\n'.join(lines), False
    except Exception:
        return '', False


def _load_docx_media_cache(cache_path: Path) -> dict[str, dict]:
    """加载 <docx>.media.ocr.json 伴生缓存，返回 sha1 -> {'text': str, 'paragraph_index': int} 映射。"""
    if not cache_path.is_file():
        return {}
    try:
        raw = json.loads(cache_path.read_text(encoding='utf-8'))
        if not isinstance(raw, dict):
            return {}
        images_map = raw.get('images') if isinstance(raw.get('images'), dict) else raw
        out: dict[str, dict] = {}
        for sha_k, val in images_map.items():
            if isinstance(val, dict) and 'text' in val:
                out[str(sha_k)] = val
            elif isinstance(val, str):
                out[str(sha_k)] = {'text': val, 'paragraph_index': 0}
        return out
    except Exception:
        return {}


def _atomic_write_text(target: Path, content: str) -> None:
    """跨平台原子落盘辅助（先写临时文件再 replace，防中途中断截断缓存 JSON）。"""
    tmp = target.with_name(f"{target.name}.tmp")
    try:
        tmp.write_text(content, encoding='utf-8', newline='\n')
        tmp.replace(target)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        target.write_text(content, encoding='utf-8', newline='\n')


def _save_docx_media_cache(cache_path: Path, cache_map: dict[str, dict]) -> None:
    """将图片 SHA1 -> 识别文本与段落位置写入 <docx>.media.ocr.json 伴生缓存。"""
    if not cache_map:
        return
    try:
        payload = {'images': cache_map}
        _atomic_write_text(cache_path, json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception:
        pass


_PDF_PAGE_INCREMENTAL_FLUSH = 5


def _load_pdf_resume_pages(
    json_cache: Path,
    total_pages: int,
    source_size: int,
    source_mtime_ns: int,
) -> dict[int, dict]:
    """加载扫描 PDF 未完成断点缓存或完整结构化缓存，返回 page_index -> page_data。"""
    if not json_cache.is_file():
        return {}
    try:
        raw = json.loads(json_cache.read_text(encoding='utf-8'))
    except Exception:
        return {}

    pages_list: list = []
    if isinstance(raw, list) and len(raw) == total_pages:
        pages_list = raw
    elif isinstance(raw, dict) and raw.get('partial') is True:
        if (
            int(raw.get('total_pages', -1)) != total_pages
            or int(raw.get('source_size', -1)) != source_size
            or int(raw.get('source_mtime_ns', -1)) != source_mtime_ns
        ):
            return {}
        raw_pages = raw.get('pages')
        if isinstance(raw_pages, list):
            pages_list = raw_pages
    else:
        return {}

    out: dict[int, dict] = {}
    for item in pages_list:
        if not isinstance(item, dict) or 'page_index' not in item:
            continue
        try:
            idx = int(item['page_index'])
        except (TypeError, ValueError):
            continue
        if 0 <= idx < total_pages and isinstance(item.get('lines'), list):
            out[idx] = item
    return out


def _save_pdf_partial_ocr_cache(
    json_cache: Path,
    total_pages: int,
    source_size: int,
    source_mtime_ns: int,
    pages_by_idx: dict[int, dict],
) -> None:
    """分批增量落盘已完成页到 <pdf>.ocr.json（dict 包装标记 partial=True，防止未完成时被 get_pdf_ocr_data 误判完整）。"""
    if not pages_by_idx:
        return
    try:
        ordered = [pages_by_idx[i] for i in sorted(pages_by_idx.keys())]
        payload = {
            'partial': True,
            'total_pages': total_pages,
            'source_size': source_size,
            'source_mtime_ns': source_mtime_ns,
            'pages': ordered,
        }
        _atomic_write_text(json_cache, json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


def _count_docx_media_targets(
    doc,
    related_parts: dict,
    cache_map: dict[str, dict],
) -> tuple[int, int]:
    """预扫描 docx 段落与表格引用的合规内嵌图片，返回 (total_uncached, cached_hits)。"""
    seen: set[str] = set()
    uncached = 0
    cached = 0

    def _scan_xml(xml_str: str) -> None:
        nonlocal uncached, cached
        if not xml_str or ('blip' not in xml_str and 'imagedata' not in xml_str):
            return
        for m in _REL_EMBED_RE.finditer(xml_str):
            rid = m.group(1)
            if not rid:
                continue
            part = related_parts.get(rid)
            if part is None:
                continue
            img_bytes = getattr(part, 'blob', None)
            if not img_bytes or not _passes_media_filter(img_bytes):
                continue
            sha1 = hashlib.sha1(img_bytes).hexdigest()
            if sha1 in seen:
                continue
            seen.add(sha1)
            if sha1 in cache_map:
                cached += 1
            else:
                uncached += 1

    for para in doc.paragraphs:
        _scan_xml(getattr(para._p, 'xml', '') or '')
    for table in doc.tables:
        seen_tcs = set()
        for row in table.rows:
            for cell in row.cells:
                tc = cell._tc
                if tc in seen_tcs:
                    continue
                seen_tcs.add(tc)
                _scan_xml(getattr(tc, 'xml', '') or '')
    return uncached, cached


def _extract_xml_embedded_media_texts(
    xml_str: str,
    related_parts: dict,
    cache_map: dict[str, dict],
    para_idx: int,
    seen_sha1s: set[str],
    on_new_ocr: Optional[Callable[[], None]] = None,
) -> tuple[list[str], bool, bool]:
    """从段落或单元格 XML 提取内嵌图片并执行就近 OCR（命中缓存直接秒读，不重算）。"""
    if not xml_str or ('blip' not in xml_str and 'imagedata' not in xml_str):
        return [], False, False

    rids = []
    for m in _REL_EMBED_RE.finditer(xml_str):
        rid = m.group(1)
        if rid and rid not in rids:
            rids.append(rid)

    if not rids:
        return [], False, False

    ocr_texts: list[str] = []
    cache_dirty = False
    ocr_skipped = False

    for rid in rids:
        part = related_parts.get(rid)
        if part is None:
            continue
        img_bytes = getattr(part, 'blob', None)
        if not img_bytes or not _passes_media_filter(img_bytes):
            continue

        sha1 = hashlib.sha1(img_bytes).hexdigest()
        if sha1 in seen_sha1s:
            continue
        seen_sha1s.add(sha1)

        if sha1 in cache_map:
            cached_txt = str(cache_map[sha1].get('text') or '').strip()
            if cached_txt:
                ocr_texts.append(cached_txt)
            continue

        ocr_txt, skipped = _ocr_single_image_bytes(img_bytes)
        if skipped:
            ocr_skipped = True
            if on_new_ocr is not None:
                try:
                    on_new_ocr()
                except Exception:
                    pass
            continue

        cache_map[sha1] = {
            'text': ocr_txt,
            'paragraph_index': para_idx,
        }
        cache_dirty = True
        if ocr_txt:
            ocr_texts.append(ocr_txt)
        if on_new_ocr is not None:
            try:
                on_new_ocr()
            except Exception:
                pass

    return ocr_texts, cache_dirty, ocr_skipped


def _render_page_image(page):
    """pymupdf 页面 → ndarray（BGR 连续内存），供 RapidOCR 识别。"""
    import numpy as np
    pix = page.get_pixmap(dpi=200)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n
    )
    if pix.n == 4:
        img = img[:, :, :3]
    return np.ascontiguousarray(img[:, :, ::-1])


def _ocr_pdf_text(
    pdf_path: Path,
    progress_cb: Optional[Callable[[int, int, int], None]] = None,
) -> str:
    """扫描件 OCR 主流程：缓存秒读 → 断点续传/分批增量落盘 → 全部完成后落盘正式缓存。"""
    cache = _ocr_cache_path(pdf_path)
    json_cache = _ocr_json_cache_path(pdf_path)
    if cache.exists():
        cached = cache.read_text(encoding='utf-8')
        if cached.strip():
            if progress_cb is not None:
                cached_pages = 0
                if json_cache.is_file():
                    try:
                        raw = json.loads(json_cache.read_text(encoding='utf-8'))
                        if isinstance(raw, list):
                            cached_pages = len(raw)
                    except Exception:
                        pass
                try:
                    progress_cb(0, 0, cached_pages)
                except Exception:
                    pass
            return cached

    import pymupdf
    file_stat = pdf_path.stat()
    source_size = int(file_stat.st_size)
    source_mtime_ns = int(file_stat.st_mtime_ns)

    pages_by_idx: dict[int, dict] = {}
    total_pages = 0
    unflushed_new = 0

    with pymupdf.open(str(pdf_path)) as doc:
        total_pages = int(doc.page_count)
        pages_by_idx = _load_pdf_resume_pages(json_cache, total_pages, source_size, source_mtime_ns)
        total_uncached = sum(1 for i in range(total_pages) if i not in pages_by_idx)
        cached_hits = total_pages - total_uncached
        uncached_done = 0

        if progress_cb is not None and (total_uncached > 0 or cached_hits > 0):
            try:
                progress_cb(0, total_uncached, cached_hits)
            except Exception:
                pass

        engine = _get_ocr_engine() if total_uncached > 0 else None

        try:
            for p_idx, page in enumerate(doc):
                if p_idx in pages_by_idx:
                    continue
                pix = page.get_pixmap(dpi=200)
                img_bgr = _render_page_image(page)
                result, _ = engine(img_bgr)
                page_lines = []
                if result:
                    for line in result:
                        if len(line) >= 2 and line[1]:
                            txt = str(line[1]).strip()
                            if not txt:
                                continue
                            raw_box = line[0]
                            box = []
                            if isinstance(raw_box, (list, tuple)):
                                box = [[float(pt[0]), float(pt[1])] for pt in raw_box]
                            score = float(line[2]) if len(line) >= 3 else 1.0
                            page_lines.append({
                                'box': box,
                                'text': txt,
                                'score': score
                            })
                pages_by_idx[p_idx] = {
                    'page_index': p_idx,
                    'page_width': float(page.rect.width),
                    'page_height': float(page.rect.height),
                    'img_width': float(pix.width),
                    'img_height': float(pix.height),
                    'lines': page_lines,
                }
                uncached_done += 1
                unflushed_new += 1
                if unflushed_new >= _PDF_PAGE_INCREMENTAL_FLUSH:
                    _save_pdf_partial_ocr_cache(
                        json_cache, total_pages, source_size, source_mtime_ns, pages_by_idx
                    )
                    unflushed_new = 0
                if progress_cb is not None:
                    try:
                        progress_cb(uncached_done, total_uncached, cached_hits)
                    except Exception:
                        pass
        except BaseException:
            if unflushed_new > 0:
                _save_pdf_partial_ocr_cache(
                    json_cache, total_pages, source_size, source_mtime_ns, pages_by_idx
                )
            raise

    ocr_pages_data = [pages_by_idx[i] for i in range(total_pages) if i in pages_by_idx]
    ocr_parts = []
    for p_item in ocr_pages_data:
        for line_item in p_item.get('lines') or []:
            txt = str(line_item.get('text') or '').strip()
            if txt:
                ocr_parts.append(txt)

    text = '\n'.join(ocr_parts)
    if len(text.strip()) < OCR_MIN_CHARS:
        try:
            json_cache.unlink(missing_ok=True)
        except Exception:
            pass
        raise ValueError(
            f'OCR 识别后仍仅得 {len(text.strip())} 字：该 PDF 为空白页或'
            '图片内容无法识别，请确认上传的是有效合同扫描件'
        )
    try:
        _atomic_write_text(json_cache, json.dumps(ocr_pages_data, ensure_ascii=False))
    except Exception:
        pass
    _atomic_write_text(cache, text)
    return text


def get_pdf_ocr_data(pdf_path: str | Path) -> list[dict]:
    """T46 获取扫描件 PDF 的 OCR 结构化数据（含每页 boxes、文本、页面尺寸与图像尺寸）。"""
    path = Path(pdf_path)
    json_cache = _ocr_json_cache_path(path)
    if json_cache.exists():
        try:
            data = json.loads(json_cache.read_text(encoding='utf-8'))
            if isinstance(data, list) and data:
                return data
        except Exception:
            pass

    try:
        _ocr_pdf_text(path)
    except Exception:
        pass

    if json_cache.exists():
        try:
            data = json.loads(json_cache.read_text(encoding='utf-8'))
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def _ocr_image_text(img_path: Path) -> str:
    """图片格式文件（.png / .jpg / .jpeg）OCR 识别并写入伴生缓存 <路径>.ocr.txt。"""
    cache = _ocr_cache_path(img_path)
    if cache.exists():
        cached = cache.read_text(encoding='utf-8')
        if cached.strip():
            return cached

    engine = _get_ocr_engine()
    ocr_parts = []
    import pymupdf
    with pymupdf.open(str(img_path)) as doc:
        for page in doc:
            img_bgr = _render_page_image(page)
            result, _ = engine(img_bgr)
            if result:
                for line in result:
                    if len(line) >= 2 and line[1]:
                        txt = str(line[1]).strip()
                        if txt:
                            ocr_parts.append(txt)

    text = '\n'.join(ocr_parts)
    if len(text.strip()) < OCR_MIN_CHARS:
        raise ValueError(
            f'图片 OCR 识别后仅得 {len(text.strip())} 字：图片为空白或清晰度不足，'
            '请上传字迹清晰的图片'
        )
    cache.write_text(text, encoding='utf-8')
    return text


_DOCX_MEDIA_INCREMENTAL_FLUSH = 10


def parse_document(
    file_path: str,
    *,
    ocr_images: bool = True,
    progress_cb: Optional[Callable[[int, int, int], None]] = None,
) -> ParsedDoc:
    """解析文档文件为纯文本（兼容原有单参数签名，E27 新增可选 ocr_images 与 progress_cb）。"""
    path = Path(file_path)

    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    ext = path.suffix.lower()

    if ext not in SUPPORTED:
        raise ValueError(f"不支持的文件类型: {ext}，当前支持 docx/pdf/txt/md/png/jpg")

    source_type = ext.replace('.', '')
    if source_type == 'md' or ext in _IMAGE_EXTS:
        source_type = 'txt'

    try:
        if ext == '.docx':
            from docx import Document
            doc = Document(file_path)
            text_parts: list[str] = []
            media_ocr_spans: list[dict[str, int]] = []
            cur_off = 0

            def _append_part(part_text: str, *, is_ocr: bool = False, para_idx: int = -1) -> None:
                nonlocal cur_off
                start = cur_off if not text_parts else cur_off + 1
                text_parts.append(part_text)
                end = start + len(part_text)
                cur_off = end
                if is_ocr:
                    media_ocr_spans.append({
                        'start': start,
                        'end': end,
                        'paragraph_index': para_idx,
                    })

            media_session = _DocxMediaOcrSession(
                path,
                doc,
                ocr_images=ocr_images,
                progress_cb=progress_cb,
            )

            try:
                for p_idx, para in enumerate(doc.paragraphs):
                    text = para.text.strip()
                    if text:
                        _append_part(text, is_ocr=False, para_idx=p_idx)
                    if ocr_images:
                        xml_str = getattr(para._p, 'xml', '') or ''
                        for itxt in media_session.extract(xml_str, p_idx):
                            if itxt:
                                _append_part(itxt, is_ocr=True, para_idx=p_idx)

                for table in doc.tables:
                    seen_tcs = set()
                    for row in table.rows:
                        for cell in row.cells:
                            tc = cell._tc
                            if tc in seen_tcs:
                                continue
                            seen_tcs.add(tc)
                            text = cell.text.strip()
                            if text:
                                _append_part(text, is_ocr=False, para_idx=-1)
                            if ocr_images:
                                tc_xml = getattr(tc, 'xml', '') or ''
                                for itxt in media_session.extract(tc_xml, -1):
                                    if itxt:
                                        _append_part(itxt, is_ocr=True, para_idx=-1)
            except BaseException:
                media_session.flush()
                raise

            media_session.flush()

            parsed_doc = ParsedDoc(text="\n".join(text_parts), source_type=source_type, pages=1)
            parsed_doc.ocr_skipped = media_session.ocr_skipped_any
            parsed_doc.media_ocr_count = len(media_session.seen_media_sha1s)
            parsed_doc.media_ocr_spans = media_ocr_spans
            return parsed_doc

        elif ext == '.pdf':
            import pymupdf
            text_parts = []
            pages = 0
            max_page_chars = 0
            with pymupdf.open(file_path) as doc:
                pages = doc.page_count
                for page in doc:
                    page_text = page.get_text()
                    text_parts.append(page_text)
                    max_page_chars = max(max_page_chars, len(page_text.strip()))
            text = "\n".join(text_parts)

            if ocr_images and len(text.strip()) < OCR_MIN_CHARS and max_page_chars < 20:
                text = _ocr_pdf_text(path, progress_cb=progress_cb)

            parsed_doc = ParsedDoc(text=text, source_type=source_type, pages=pages)
            parsed_doc.media_ocr_spans = []
            return parsed_doc

        elif ext in {'.txt', '.md'}:
            raw_bytes = path.read_bytes()
            text = None
            for encoding in ['utf-8', 'gbk', 'utf-16']:
                try:
                    text = raw_bytes.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue

            if text is None:
                text = raw_bytes.decode('utf-8', errors='replace')

            parsed_doc = ParsedDoc(text=text, source_type=source_type, pages=1)
            parsed_doc.media_ocr_spans = []
            return parsed_doc

        elif ext in _IMAGE_EXTS:
            text = _ocr_image_text(path)
            parsed_doc = ParsedDoc(text=text, source_type=source_type, pages=1)
            parsed_doc.media_ocr_spans = []
            return parsed_doc

    except ValueError:
        raise
    except Exception as e:
        raise RuntimeError(f"解析失败: {type(e).__name__}: {e}") from e


def _ocr_image_text_low_threshold(img_path: Path) -> tuple[str, Optional[int], Optional[int]]:
    """E24 §2.1：F7 问答图片附件专用低门槛 OCR（取消 OCR_MIN_CHARS=100 硬限制，0~99 字短截图或纯公章页亦正常返回并写缓存）。"""
    raw_bytes = img_path.read_bytes()
    dims = _get_image_dimensions(raw_bytes)
    width = dims[0] if dims else None
    height = dims[1] if dims else None

    import pymupdf
    try:
        with pymupdf.open(str(img_path)) as check_doc:
            if check_doc.page_count < 1:
                raise ValueError('图片不包含有效页面')
            if width is None or height is None:
                rect = check_doc[0].rect
                width = int(rect.width)
                height = int(rect.height)
    except Exception as exc:
        raise ValueError(f"图片文件损坏或格式不受支持: {exc}") from exc

    cache = _ocr_cache_path(img_path)
    if cache.exists():
        cached = cache.read_text(encoding='utf-8')
        return cached, width, height

    try:
        engine = _get_ocr_engine()
    except RuntimeError:
        cache.write_text('', encoding='utf-8')
        return '', width, height

    ocr_parts: list[str] = []
    with pymupdf.open(str(img_path)) as doc:
        for page in doc:
            img_bgr = _render_page_image(page)
            result, _ = engine(img_bgr)
            if result:
                for line in result:
                    if len(line) >= 2 and line[1]:
                        txt = str(line[1]).strip()
                        if txt:
                            ocr_parts.append(txt)

    text = '\n'.join(ocr_parts)
    cache.write_text(text, encoding='utf-8')
    return text, width, height


def parse_qa_attachment(file_path: str | Path) -> dict:
    """E24 §2.1：F7 会话附件专用解析入口（不改 parse_document 既有签名与双书 100 字红线）。

    Returns:
        {
            'text': str,
            'source_type': str,
            'pages': int,
            'is_image': bool,
            'ocr_chars': int,
            'has_text': bool,
            'image_width': int | None,
            'image_height': int | None,
        }
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"附件文件不存在: {file_path}")

    ext = path.suffix.lower()
    if ext not in SUPPORTED:
        raise ValueError(f"不支持的附件格式: {ext}")

    if path.stat().st_size <= 0:
        raise ValueError('上传的附件内容为空')

    if ext in _IMAGE_EXTS:
        text, w, h = _ocr_image_text_low_threshold(path)
        clean_len = len(text.strip())
        return {
            'text': text,
            'source_type': 'image',
            'pages': 1,
            'is_image': True,
            'ocr_chars': clean_len,
            'has_text': clean_len > 0,
            'image_width': w,
            'image_height': h,
        }

    if ext == '.pdf':
        import pymupdf
        try:
            text_parts: list[str] = []
            pages = 0
            max_page_chars = 0
            with pymupdf.open(str(path)) as doc:
                pages = doc.page_count
                for page in doc:
                    pt = page.get_text()
                    text_parts.append(pt)
                    max_page_chars = max(max_page_chars, len(pt.strip()))
            text = '\n'.join(text_parts)
        except Exception as exc:
            raise ValueError(f"PDF 附件损坏或无法打开: {exc}") from exc

        ocr_chars = 0
        if len(text.strip()) < OCR_MIN_CHARS and max_page_chars < 20:
            cache = _ocr_cache_path(path)
            if cache.exists():
                text = cache.read_text(encoding='utf-8')
            else:
                try:
                    engine = _get_ocr_engine()
                    ocr_parts: list[str] = []
                    with pymupdf.open(str(path)) as doc:
                        for page in doc:
                            img_bgr = _render_page_image(page)
                            result, _ = engine(img_bgr)
                            if result:
                                for line in result:
                                    if len(line) >= 2 and line[1]:
                                        t_line = str(line[1]).strip()
                                        if t_line:
                                            ocr_parts.append(t_line)
                    text = '\n'.join(ocr_parts)
                    cache.write_text(text, encoding='utf-8')
                except Exception:
                    pass
            ocr_chars = len(text.strip())

        clean_len = len(text.strip())
        if clean_len == 0:
            raise ValueError('PDF 附件未提取出任何有效文字')
        return {
            'text': text,
            'source_type': 'pdf',
            'pages': max(1, pages),
            'is_image': False,
            'ocr_chars': ocr_chars,
            'has_text': True,
            'image_width': None,
            'image_height': None,
        }

    try:
        parsed = parse_document(str(path))
    except Exception as exc:
        raise ValueError(f"文档附件解析失败: {exc}") from exc

    clean_len = len((parsed.text or '').strip())
    if clean_len == 0:
        raise ValueError('文档附件内容为空，未提取出有效文字')

    return {
        'text': parsed.text,
        'source_type': parsed.source_type,
        'pages': parsed.pages,
        'is_image': False,
        'ocr_chars': 0,
        'has_text': True,
        'image_width': None,
        'image_height': None,
    }
