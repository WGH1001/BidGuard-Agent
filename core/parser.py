# -*- coding: utf-8 -*-
"""T1 文档解析模块 [Gemini-Flash]

实现规格（签名不得修改）：
    parse_document(file_path) -> ParsedDoc

行为要求：
    1. .docx：用 python-docx（from docx import Document）。
       - 正文段落：遍历 doc.paragraphs，跳过 strip 后为空的段落；
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
import json
from pathlib import Path
import threading

from core.interfaces import ParsedDoc

SUPPORTED = {'.docx', '.pdf', '.txt', '.md', '.png', '.jpg', '.jpeg'}
_IMAGE_EXTS = {'.png', '.jpg', '.jpeg'}

# ── T29 扫描件离线 OCR（RapidOCR + pymupdf 渲染）────────────────────────
# 文本层不足该字数视为扫描件/图片型 PDF，触发 OCR 兜底
OCR_MIN_CHARS = 100

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
    """解析合同文件为纯文本。

    Args:
        file_path: 合同文件路径（.docx / .pdf / .txt / .md / .png / .jpg / .jpeg）。

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

            for para in doc.paragraphs:
                text = para.text.strip()
                if text:
                    text_parts.append(text)

            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        text = cell.text.strip()
                        if text:
                            text_parts.append(text)

            return ParsedDoc(text="\n".join(text_parts), source_type=source_type, pages=1)

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