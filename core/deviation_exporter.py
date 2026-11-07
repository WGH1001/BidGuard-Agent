# -*- coding: utf-8 -*-
"""F5 / E6 《技术与商务偏离表.docx》一键对齐生成器（含 Word 双轨估算页码展示）。

模块职责（对应 PRD-02 §7 F5、F5 任务卡与 E6 任务卡）：
1. 主入口 export_deviation_table(tender_path, bid_path, output_path, requirements=None, client=None)：
   - 内部调用 F4 run_response_check 拿到逐条核验结果 star_results；
   - 与 _filter_valid_requirements 过滤后的有效要求清单严格同序 zip 配对；
   - 生成标准六列 Word 表格（Table Grid）：
     序号 | 招标条款号 | 招标文件要求 | 我方投标响应 | 偏离情况 | 位置索引
2. 偏离判定严禁二次判定，只消费 F4 三态结果：
   - RESPONSE_OK      -> 无偏离（绿色）
   - MISSING_RESPONSE -> 负偏离（漏应答）（红色加粗，我方投标响应填"（未检出响应内容）"）
   - CONTRADICTION    -> 负偏离（响应冲突）（红色加粗）
3. 位置索引列纪律（E6 升级）：
   - PDF 使用 pymupdf 真实页码（"招标书第 X 页 | 投标书第 Y 页"）；
   - docx 若解析到分页锚点：有底部印刷页码时输出 "招标书 第X页(印刷页Y·估算)" / "投标书 第X页(印刷页Y·估算)"，
     无印刷页码时输出 "招标书 第X页(物理页·估算)" / "投标书 第X页(物理页·估算)"；
   - docx 无分页锚点（纯机器生成）：保持 "招标书@字符123 | 投标书@字符456"；
   - 漏应答行注明"投标书未检出响应"。
"""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Optional

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor

from core.response_checker import _filter_valid_requirements, run_response_check
from core.tender_extractor import build_review_checklist

DEVIATION_TABLE_HEADERS = (
    '序号',
    '招标条款号',
    '招标文件要求',
    '我方投标响应',
    '偏离情况',
    '位置索引',
)

_COLOR_RED = RGBColor(0xDC, 0x26, 0x26)
_COLOR_GREEN = RGBColor(0x16, 0xA3, 0x4A)
_COLOR_NAVY = RGBColor(0x0F, 0x17, 0x2A)
_COLOR_MUTED = RGBColor(0x47, 0x55, 0x69)

_MISSING_RESPONSE_PLACEHOLDER = '（未检出响应内容）'


def _extract_consequence_tag(req: dict) -> str:
    """从要求字典（或 structured 子字典）提取评分后果后缀标签。"""
    structured = req.get('structured') if isinstance(req.get('structured'), dict) else {}
    consequence = (req.get('consequence') or structured.get('consequence') or '').strip().lower()
    mark = (req.get('mark') or '').strip()

    if consequence == 'veto':
        prefix = mark if mark else '★'
        return f"{prefix}一票否决项"
    if consequence == 'deduction':
        prefix = mark if mark else '▲'
        return f"{prefix}重要扣分项"
    if consequence == 'bonus':
        prefix = mark if mark else '▲'
        return f"{prefix}重要加分项"
    return ''


def _format_clause_no(req: dict, star_res: dict) -> str:
    """提取并格式化招标条款号列内容。"""
    structured = req.get('structured') if isinstance(req.get('structured'), dict) else {}
    clause_no = (
        star_res.get('clause_no')
        or structured.get('clause_no')
        or req.get('clause_no')
        or ''
    ).strip()
    mark = (star_res.get('mark') or req.get('mark') or '').strip()

    if clause_no and mark and mark not in clause_no:
        return f"{mark}{clause_no}"
    if clause_no:
        return clause_no
    if mark:
        return mark
    if req.get('pattern'):
        return f"模式{req['pattern']}"
    return '—'


def _format_deviation_label(status: str, req: dict) -> tuple[str, bool]:
    """根据 F4 的三态结果映射偏离情况文案与是否为负偏离。"""
    if status == 'MISSING_RESPONSE':
        base = '负偏离（漏应答）'
        is_neg = True
    elif status == 'CONTRADICTION':
        base = '负偏离（响应冲突）'
        is_neg = True
    else:
        base = '无偏离'
        is_neg = False

    cons_tag = _extract_consequence_tag(req)
    if cons_tag:
        return f"{base}，{cons_tag}", is_neg
    return base, is_neg


def _format_single_doc_loc(
    doc_label: str,
    page: Optional[int],
    footer_page: Optional[int],
    is_docx: bool,
    char_offset: Optional[int],
) -> str:
    """格式化单侧（招标书或投标书）位置描述：
    - docx 且有页码：第X页(印刷页Y·估算) 或 第X页(物理页·估算)
    - PDF 且有页码：第 X 页
    - 无页码：@字符N
    """
    if page is not None and page > 0:
        if is_docx:
            if footer_page is not None and footer_page > 0:
                return f"{doc_label} 第{page}页(印刷页{footer_page}·估算)"
            return f"{doc_label} 第{page}页(物理页·估算)"
        return f"{doc_label}第 {page} 页"
    return f"{doc_label}@字符{char_offset if char_offset is not None else 0}"


def _format_location_index(star_res: dict) -> str:
    """格式化第 6 列「位置索引」（招标书与投标书各自独立判定格式）。"""
    t_part = _format_single_doc_loc(
        doc_label='招标书',
        page=star_res.get('tender_page'),
        footer_page=star_res.get('tender_footer_page'),
        is_docx=bool(star_res.get('tender_is_docx', False)),
        char_offset=star_res.get('tender_start', 0),
    )

    status = star_res.get('status')
    b_start = star_res.get('bid_start')

    if status == 'MISSING_RESPONSE' or b_start is None or b_start < 0:
        b_part = '投标书未检出响应'
    else:
        b_part = _format_single_doc_loc(
            doc_label='投标书',
            page=star_res.get('bid_page'),
            footer_page=star_res.get('bid_footer_page'),
            is_docx=bool(star_res.get('bid_is_docx', False)),
            char_offset=b_start,
        )

    return f"{t_part} | {b_part}"


def export_deviation_table(
    tender_path: str | Path,
    bid_path: str | Path,
    output_path: str | Path,
    requirements: Optional[list[dict]] = None,
    client=None,
) -> dict[str, Any]:
    """一键生成《技术与商务偏离表.docx》。"""
    t_path = Path(tender_path)
    b_path = Path(bid_path)
    out_path = Path(output_path)

    if requirements is None:
        checklist = build_review_checklist(str(t_path))
        requirements = checklist['marked_clauses'] + checklist['invalid_bid_clauses']

    valid_reqs = _filter_valid_requirements(requirements)

    f4_res = run_response_check(
        tender_path=t_path,
        bid_path=b_path,
        requirements=valid_reqs,
        client=client,
    )
    star_results = f4_res.get('star_results', [])

    if len(valid_reqs) != len(star_results):
        raise RuntimeError(
            f"配对契约破坏：valid_reqs({len(valid_reqs)}) 与 star_results({len(star_results)}) 长度不一致"
        )

    rows_data: list[dict[str, Any]] = []
    no_dev_count = 0
    neg_dev_count = 0
    missing_count = 0
    contra_count = 0

    for seq_idx, (req, s_res) in enumerate(zip(valid_reqs, star_results), start=1):
        status = s_res.get('status', 'RESPONSE_OK')
        clause_no_str = _format_clause_no(req, s_res)

        tender_q = (s_res.get('tender_quote') or '').strip()
        bid_q = (s_res.get('bid_quote') or '').strip()

        if status == 'MISSING_RESPONSE':
            bid_display = _MISSING_RESPONSE_PLACEHOLDER
            missing_count += 1
        elif status == 'CONTRADICTION':
            bid_display = bid_q if bid_q else _MISSING_RESPONSE_PLACEHOLDER
            contra_count += 1
        else:
            bid_display = bid_q if bid_q else '完全响应招标文件要求'

        dev_label, is_neg = _format_deviation_label(status, req)
        if is_neg:
            neg_dev_count += 1
        else:
            no_dev_count += 1

        loc_str = _format_location_index(s_res)

        rows_data.append({
            'seq': str(seq_idx),
            'clause_no': clause_no_str,
            'tender_requirement': tender_q,
            'bid_response': bid_display,
            'deviation': dev_label,
            'location': loc_str,
            'is_negative': is_neg,
            'status': status,
        })

    doc = Document()

    title_p = doc.add_paragraph()
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title_run = title_p.add_run('技术与商务偏离表')
    title_run.bold = True
    title_run.font.size = Pt(16)
    title_run.font.color.rgb = _COLOR_NAVY

    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    meta_p = doc.add_paragraph()
    meta_run = meta_p.add_run(
        f"招标文件：{t_path.name} ｜ 投标文件：{b_path.name}\n"
        f"核验统计：共核验实质性要求 {len(rows_data)} 项，无偏离 {no_dev_count} 项，"
        f"负偏离 {neg_dev_count} 项（其中漏应答 {missing_count} 项、响应冲突 {contra_count} 项） ｜ "
        f"生成时间：{now_str}\n"
        f"注：Word 页码为系统估算，可能与实际排版有出入，以字符偏移为准。"
    )
    meta_run.font.size = Pt(9.5)
    meta_run.font.color.rgb = _COLOR_MUTED

    table = doc.add_table(rows=1, cols=len(DEVIATION_TABLE_HEADERS))
    table.style = 'Table Grid'
    table.alignment = WD_TABLE_ALIGNMENT.CENTER

    hdr_cells = table.rows[0].cells
    for col_i, h_text in enumerate(DEVIATION_TABLE_HEADERS):
        p = hdr_cells[col_i].paragraphs[0]
        r = p.add_run(h_text)
        r.bold = True
        r.font.size = Pt(10.5)
        r.font.color.rgb = _COLOR_NAVY

    for item in rows_data:
        row_cells = table.add_row().cells
        col_values = (
            item['seq'],
            item['clause_no'],
            item['tender_requirement'],
            item['bid_response'],
            item['deviation'],
            item['location'],
        )
        for col_i, val in enumerate(col_values):
            p = row_cells[col_i].paragraphs[0]
            r = p.add_run(val)
            r.font.size = Pt(10)
            if col_i == 4:
                if item['is_negative']:
                    r.bold = True
                    r.font.color.rgb = _COLOR_RED
                else:
                    r.font.color.rgb = _COLOR_GREEN

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))

    return {
        'output_path': str(out_path),
        'total_rows': len(rows_data),
        'no_deviation_count': no_dev_count,
        'negative_deviation_count': neg_dev_count,
        'missing_count': missing_count,
        'contradiction_count': contra_count,
        'rows': rows_data,
        'f4_result': f4_res,
    }