# -*- coding: utf-8 -*-
"""T1 / E1 文档解析模块（含 PDF 扫描件 OCR 与 docx 内嵌证照图片段落就近吸附离线 OCR）。

实现规格（签名不得修改）：
    parse_document(file_path) -> ParsedDoc

行为要求：
    1. .docx：用 python-docx（from docx import Document）。
       - 正文段落：遍历 doc.paragraphs，跳过 strip 后为空的段落；
       - E1 升级点 2（内嵌图片段落就近吸附 OCR）：检测段落及表格单元格 XML 内是否含
         <a:blip r:embed="rId..."/>（或 <v:imagedata r:id="rId..."/>），从 doc.part.related_parts
         取出图片字节流；仅对 >20KB、宽高 >=200px 且长宽比在 1:6 ~ 6:1 之间的图片执行离线 OCR，
         识别文字紧跟在所在段落文本下方拼接，并按图片 SHA1 缓存在 <原文件>.media.ocr.json；
       - 表格文字：遍历 doc.tables 每行每格 cell.text，strip 后非空则追加；
       - 所有文本片段用 '\n' 连接成一个字符串；
       - pages = 1（docx 无固定分页概念）。
    2. .pdf：用 PyMuPDF（import pymupdf）。
       - pymupdf.open(file_path) 后逐页 page.get_text()，用 '\n' 连接；
       - pages = 页数；with 语句确保关闭。
    3. .txt / .md：按字节读取，依次尝试 utf-8 / gbk / utf-16 解码，
       全部失败则 utf-8 + errors='replace'；pages = 1。
    4. 其他扩展名：raise ValueError(f'不支持的文件类型: {ext}，当前支持 docx/pdf/txt')
    5. 文件不存在：raise FileNotFoundError（不得自行捕获吞掉）。
    6. 解析过程中的任何异常（docx 损坏、pdf 加密等）：
       raise RuntimeError(f'解析失败: {原始异常}') —— 不得吞异常返回空串。

禁止事项：
    - 不得引入 requirements.txt 之外的第三方库；
    - 不得修改函数签名与 core/interfaces.py；
    - 不得 print（库模块静默工作）。
"""
import hashlib
import json
from pathlib import Path
import re
import struct
import threading

from core.interfaces import ParsedDoc

SUPPORTED = {'.docx', '.pdf', '.txt', '.md', '.png', '.jpg', '.jpeg'}
_IMAGE_EXTS = {'.png', '.jpg', '.jpeg'}

# ── T29 扫描件离线 OCR（RapidOCR + pymupdf 渲染）────────────────────────
# 文本层不足该字数视为扫描件/图片型 PDF，触发 OCR 兜底
OCR_MIN_CHARS = 100

# ── E1 升级点 2：docx 内嵌证照图片双重过滤阈值 ──────────────────────────
_MEDIA_MIN_BYTES = 20 * 1024     # 字节数下限：> 20KB（20480 bytes）
_MEDIA_MIN_DIM_PX = 200          # 宽高像素下限：width >= 200 且 height >= 200
_MEDIA_MAX_ASPECT_RATIO = 6.0    # 长宽比范围：1:6 ~ 6:1

_REL_EMBED_RE = re.compile(r'(?:r:embed|r:id)="([^"]+)"')

_OCR_ENGINE = None
_OCR_LOCK = threading.Lock()


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


def _get_image_dimensions(img_bytes: bytes) -> tuple[int, int] | None:
    """解析图片字节流的 (width, height) 像素尺寸（标准库 PNG/JPEG 头快速解析 + PyMuPDF 兜底）。"""
    if not img_bytes or len(img_bytes) < 24:
        return None

    # 1. PNG 头解析（Signature 8B + Length 4B + 'IHDR' 4B + Width 4B + Height 4B）
    if img_bytes[:8] == b'\x89PNG\r\n\x1a\n' and img_bytes[12:16] == b'IHDR':
        w, h = struct.unpack('>II', img_bytes[16:24])
        return int(w), int(h)

    # 2. JPEG SOF 段解析
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

    # 3. 兜底使用 PyMuPDF Pixmap 读取尺寸
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


def _save_docx_media_cache(cache_path: Path, cache_map: dict[str, dict]) -> None:
    """将图片 SHA1 -> 识别文本与段落位置写入 <docx>.media.ocr.json 伴生缓存。"""
    if not cache_map:
        return
    try:
        payload = {'images': cache_map}
        cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass


def _extract_xml_embedded_media_texts(
    xml_str: str,
    related_parts: dict,
    cache_map: dict[str, dict],
    para_idx: int,
    seen_sha1s: set[str],
) -> tuple[list[str], bool, bool]:
    """从段落或单元格 XML 提取内嵌图片并执行就近 OCR（命中缓存直接秒读，不重算）。

    Returns:
        (ocr_texts_list, cache_dirty, ocr_skipped)
    """
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

        # 优先命中伴生缓存：若 sha1 在缓存中，直接取缓存文本，绝不重新调用 OCR
        if sha1 in cache_map:
            cached_txt = str(cache_map[sha1].get('text') or '').strip()
            if cached_txt:
                ocr_texts.append(cached_txt)
            continue

        ocr_txt, skipped = _ocr_single_image_bytes(img_bytes)
        if skipped:
            ocr_skipped = True
            continue

        cache_map[sha1] = {
            'text': ocr_txt,
            'paragraph_index': para_idx,
        }
        cache_dirty = True
        if ocr_txt:
            ocr_texts.append(ocr_txt)

    return ocr_texts, cache_dirty, ocr_skipped


def _render_page_image(page):
    """pymupdf 页面 → ndarray（BGR 连续内存），供 RapidOCR 识别。"""
    import numpy as np
    pix = page.get_pixmap(dpi=200)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n
    )
    if pix.n == 4:
        img = img[:, :, :3]      # 去 alpha 通道
    # 反转通道产生负步长视图，cv2 预处理要求连续内存，必须 ascontiguousarray
    return np.ascontiguousarray(img[:, :, ::-1])


def _ocr_pdf_text(pdf_path: Path) -> str:
    """扫描件 OCR 主流程：缓存秒读 → 逐页渲染识别 → 成功写缓存。

    缓存策略：识别成功才写 <pdf路径>.ocr.txt 与 .ocr.json；后续 parse_document（含
    /api/qa/ask 读取源合同）检测到缓存直接返回，绝不重复跑 OCR。

    Raises:
        ValueError: OCR 识别后文本仍不足 OCR_MIN_CHARS（空白页/无效图片）。
        RuntimeError: OCR 引擎未安装等环境问题。
    """
    cache = _ocr_cache_path(pdf_path)
    if cache.exists():
        cached = cache.read_text(encoding='utf-8')
        if cached.strip():
            return cached

    engine = _get_ocr_engine()
    ocr_parts = []
    ocr_pages_data = []
    import pymupdf
    with pymupdf.open(str(pdf_path)) as doc:
        for p_idx, page in enumerate(doc):
            pix = page.get_pixmap(dpi=200)
            img_bgr = _render_page_image(page)
            result, _ = engine(img_bgr)
            page_lines = []
            if result:
                for line in result:
                    # RapidOCR 返回 [box, text, score]
                    if len(line) >= 2 and line[1]:
                        txt = str(line[1]).strip()
                        ocr_parts.append(txt)
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
            ocr_pages_data.append({
                'page_index': p_idx,
                'page_width': float(page.rect.width),
                'page_height': float(page.rect.height),
                'img_width': float(pix.width),
                'img_height': float(pix.height),
                'lines': page_lines
            })

    text = '\n'.join(p for p in ocr_parts if p)
    if len(text.strip()) < OCR_MIN_CHARS:
        raise ValueError(
            f'OCR 识别后仍仅得 {len(text.strip())} 字：该 PDF 为空白页或'
            '图片内容无法识别，请确认上传的是有效合同扫描件'
        )
    cache.write_text(text, encoding='utf-8')
    try:
        _ocr_json_cache_path(pdf_path).write_text(
            json.dumps(ocr_pages_data, ensure_ascii=False),
            encoding='utf-8'
        )
    except Exception:
        pass
    return text


def get_pdf_ocr_data(pdf_path: str | Path) -> list[dict]:
    """T46 获取扫描件 PDF 的 OCR 结构化数据（含每页 boxes、文本、页面尺寸与图像尺寸）。

    优先秒读伴生缓存 <pdf>.ocr.json；若缓存不存在则触发一次 OCR 流程。
    若该 PDF 并非扫描件或 OCR 无有效文本，返回空列表 []。
    """
    path = Path(pdf_path)
    json_cache = _ocr_json_cache_path(path)
    if json_cache.exists():
        try:
            data = json.loads(json_cache.read_text(encoding='utf-8'))
            if isinstance(data, list) and data:
                return data
        except Exception:
            pass

    # 若 json 缓存尚不存在，触发一次 OCR（若是扫描件则会生成 json 缓存）
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
    """图片格式合同（.png / .jpg / .jpeg）OCR 识别并写入伴生缓存 <路径>.ocr.txt。"""
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
            '请上传字迹清晰的合同图片'
        )
    cache.write_text(text, encoding='utf-8')
    return text


def parse_document(file_path: str) -> ParsedDoc:
    """解析合同/标书文件为纯文本。

    Args:
        file_path: 文件路径（.docx / .pdf / .txt / .md / .png / .jpg / .jpeg）。

    Returns:
        ParsedDoc: text 为全文纯文本；source_type 为 'docx'|'pdf'|'txt'；pages 为页数。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 扩展名不支持。
        RuntimeError: 解析失败（文件损坏、加密等）。
    """
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
            text_parts = []

            related_parts = getattr(doc.part, 'related_parts', {}) or {}
            media_cache_path = _docx_media_ocr_cache_path(path)
            media_cache_map = _load_docx_media_cache(media_cache_path)
            seen_media_sha1s: set[str] = set()
            cache_dirty = False
            ocr_skipped_any = False

            for p_idx, para in enumerate(doc.paragraphs):
                text = para.text.strip()
                if text:
                    text_parts.append(text)
                xml_str = getattr(para._p, 'xml', '') or ''
                img_texts, dirty, skipped = _extract_xml_embedded_media_texts(
                    xml_str, related_parts, media_cache_map, p_idx, seen_media_sha1s
                )
                if dirty:
                    cache_dirty = True
                if skipped:
                    ocr_skipped_any = True
                for itxt in img_texts:
                    text_parts.append(itxt)

            for table in doc.tables:
                # F1 修正（2026-11-04 滁州托育语料实测）：合并单元格的延续行会
                # 反复返回同一 tc 元素，且 WPS 生成的文件存在"同一内容写多个
                # vMerge 起点格"的怪结构——按格元素本体去重（引用入集合保持
                # 存活，严禁用 id()：对象回收后 id 复用会导致去重不稳定）。
                seen_tcs = set()
                for row in table.rows:
                    for cell in row.cells:
                        tc = cell._tc
                        if tc in seen_tcs:
                            continue
                        seen_tcs.add(tc)
                        text = cell.text.strip()
                        if text:
                            text_parts.append(text)
                        tc_xml = getattr(tc, 'xml', '') or ''
                        img_texts, dirty, skipped = _extract_xml_embedded_media_texts(
                            tc_xml, related_parts, media_cache_map, -1, seen_media_sha1s
                        )
                        if dirty:
                            cache_dirty = True
                        if skipped:
                            ocr_skipped_any = True
                        for itxt in img_texts:
                            text_parts.append(itxt)

            if cache_dirty:
                _save_docx_media_cache(media_cache_path, media_cache_map)

            parsed_doc = ParsedDoc(text="\n".join(text_parts), source_type=source_type, pages=1)
            parsed_doc.ocr_skipped = ocr_skipped_any
            parsed_doc.media_ocr_count = len(seen_media_sha1s)
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

            # T29 扫描件兜底：总量 <100 字且**每一页都没有文本层**（单页
            # 最多文本 <20 字）才判定为图片型 PDF 触发 OCR——短小但真实
            # 的文本型 PDF 不误伤（配合 orchestrator 终极防护）
            if len(text.strip()) < OCR_MIN_CHARS and max_page_chars < 20:
                text = _ocr_pdf_text(path)

            return ParsedDoc(text=text, source_type=source_type, pages=pages)

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

            return ParsedDoc(text=text, source_type=source_type, pages=1)

        elif ext in _IMAGE_EXTS:
            text = _ocr_image_text(path)
            return ParsedDoc(text=text, source_type=source_type, pages=1)

    except ValueError:
        # OCR 兜底的语义化失败（扫描件仍无有效文本）原样上抛，不包壳
        raise
    except Exception as e:
        raise RuntimeError(f"解析失败: {type(e).__name__}: {e}") from e