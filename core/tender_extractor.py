# -*- coding: utf-8 -*-
"""F1 招标文件废标红线与★/▲条款提取器 —— 确定性层（0 Token，纯规则）。

模块定位（PRD-02 §7 F1 第一层）：
    在 LLM 分段抽取之前，用确定性规则把整本招标文件中的强制条款"圈出来"，
    产出结构化候选清单。三层职责：
    ① ★/▲ 标记条款提取（标记型语料）；
    ② 无标记措辞型强制条款提取（全文无任何 ★/▲ 标记，旧称"无星措辞型"）（依据 2026-11-04 徐州份人工核验沉淀的两类句式：
       模式 A「不允许偏离的实质性要求」模板句 / 模式 B「无效投标处理」触发词）；
    ③ 章节与审查区定位（资格审查 / 符合性审查 / 评标办法·评分细则表），供
       LLM 分段层按区域投喂、防止整本直塞导致注意力衰减（PRD R-01）。

实现规格（签名不得修改）：
    load_document(file_path) -> ParsedDoc
    locate_chapters(text) -> list[dict]              # 第X章标题与偏移
    extract_marked_clauses(text) -> list[dict]       # ★/▲ 标记条款
    extract_invalid_bid_clauses(text) -> list[dict]  # 无标记措辞型（模式A/B）
    locate_keyword_sections(text, keywords) -> list[dict]  # 审查区/评分区定位
    build_review_checklist(file_path) -> dict        # 四类候选汇总（一次成稿）

设计红线：
    - 0 Token：本模块严禁出现任何 LLM 调用（LLM 分段层在 orchestration 之上）；
    - 可溯源：每条候选必须携带 start/end 字符偏移，能还原原文（复用 ParsedDoc.text）；
    - 绝不静默：正则不命中不是错误（返回空列表），但解析失败必须抛异常；
    - 库模块只抛不打印。
"""
import re

from core.interfaces import ParsedDoc
from core.parser import parse_document

# ★/▲ 标记（语料实证：#4 南京病理 ★19/▲10、#9 芜湖 ★6/▲3、#7 连云港 ▲62、#11 滁州托育 ▲26）
_MARK_RE = re.compile(r'[★▲]')

# 条款编号头（向前吸附用）：（十四）/ (14) / 2.1 / 第X条 / 一、 等
_CLAUSE_HEADER_RE = re.compile(
    r'（[一二三四五六七八九十百零两]+）'
    r'|\([一二三四五六七八九十百零两]+\)'
    r'|（\d{1,3}）'
    r'|\(\d{1,3}\)'
    r'|第[一二三四五六七八九十百千零两]+条'
    r'|[一二三四五六七八九十百零两]+、'
    r'|\d{1,2}\.\d{1,2}'
)

# 章标题：第一章 投标邀请 / 第五章 评标方法与评标标准
_CHAPTER_RE = re.compile(r'第[一二三四五六七八九十百零两]+章\s*[^\n第]{0,40}')

# 模式 A：实质性要求模板句（徐州份核验：句式固定、自带承诺函名称）
_PATTERN_A_RE = re.compile(r'不允许偏离的实质性要求')
_PROMISE_DOC_RE = re.compile(r'《([^》]{2,30})》')

# 模式 B：无效投标触发词（语料实证变体，要求带"处理"落点防误伤泛论）
_PATTERN_B_RE = re.compile(
    r'无效投标处理|投标无效处理|作无效投标|视为无效投标|按无效投标|无效响应处理'
)

# 标记使用说明语（元说明，非采购要求本身，F1 输出要求清单时应排除）：
# 语料实证五类——滁州物业 #10「根据采购需求中标"▲"功能要求，投标人须提供…」、
# 无锡梁溪 #2《节能产品政府采购品目清单》「属于清单中打★品目的…」、
# 亳州 #8「标有★的参数为实质性参数…」「▲为政府强制采购节能产品…」（2026-11-04
# 用户独立复核新增）、南京病理 #4 评分说明「打★指标为必须满足项…打▲指标为
# 重要指标…」（同上）。
# 保守起见只匹配已实证的模板形态，新形态由 ground truth 定稿时人工补。
_USAGE_NOTE_RE = re.compile(
    r'品目清单'
    r'|根据采购需求.{0,8}[“"]?[▲★][”"]?功能要求'
    r'|打[★▲]指标'                      # 评分说明：打★指标为必须满足项 / 打▲指标为重要指标
    r'|标有\s*[“"]?\s*[★▲]\s*[”"]?\s*的'  # 实质性说明：标有"★"的参数为实质性参数（亳州实证含空格+引号）
    r'|[★▲]为(政府)?强制采购'           # 节能说明：▲为政府强制采购节能产品
)

# 章节语义关键词（定位 评标办法 / 资格审查 / 符合性审查 / 评分细则）
_EVAL_CHAPTER_KEYS = ('评标办法', '评标方法', '评标标准', '评分细则')
_QUALIFY_KEYS = ('资格性审查', '资格审查', '申请人的资格要求', '供应商资格要求')
_COMPLIANCE_KEYS = ('符合性审查', '实质性要求审查')


def load_document(file_path: str) -> ParsedDoc:
    """解析招标文件为 ParsedDoc（复用 core.parser，含扫描件 OCR 兜底）。"""
    return parse_document(file_path)


def _find_clause_header_before(text: str, pos: int, window: int = 800) -> str:
    """在 pos 之前 window 字符窗口内找最近的条款编号头，返回编号原文（无则空串）。"""
    seg_start = max(0, pos - window)
    segment = text[seg_start:pos]
    last = ''
    for m in _CLAUSE_HEADER_RE.finditer(segment):
        last = m.group(0)
    return last


def _expand_enclosing_clause(text: str, hit_start: int, hit_end: int,
                             max_len: int = 600) -> tuple[int, int, str]:
    """把命中点扩展为所在编号条款的 [start, end) 与条款编号。

    向前找最近条款头；向后找下一个条款头，截断到句号边界与 max_len。
    """
    clause_no = _find_clause_header_before(text, hit_start)
    start = hit_start
    if clause_no:
        idx = text.rfind(clause_no, max(0, hit_start - 800), hit_start)
        if idx >= 0:
            start = idx
    nxt = _CLAUSE_HEADER_RE.search(text, hit_end, hit_end + max_len)
    end = nxt.start() if nxt else min(len(text), hit_end + max_len)
    period = text.rfind('。', start, end)
    if period > start + 20:
        end = period + 1
    if end - start > max_len:
        end = start + max_len
    return start, end, clause_no


def locate_chapters(text: str) -> list[dict]:
    """定位全部"第X章 标题"。

    Returns:
        [{'index': '第五章', 'title': '评标方法与评标标准', 'start': int}, ...]
        title 已去首尾空白；start 为章标题起始偏移（可还原原文）。
    """
    chapters = []
    seen_spans = set()
    for m in _CHAPTER_RE.finditer(text):
        key = (m.start(), m.end())
        if key in seen_spans:
            continue
        seen_spans.add(key)
        raw = m.group(0)
        parts = raw.split(None, 1)
        index = parts[0]
        title = parts[1].strip() if len(parts) > 1 else ''
        chapters.append({'index': index, 'title': title, 'start': m.start()})
    return chapters


def extract_marked_clauses(text: str) -> list[dict]:
    """提取 ★/▲ 标记条款（标记型语料的确定性主通道）。

    按"行"捕获、按标记类型分组合并：连续的同类型标记行合并为一条（★ 参数
    常跨行续写）；同一行同时含 ★ 与 ▲ 的**要求行**分别产出两条记录（两类
    强制要求各自独立跟踪），绝不丢标记。

    **说明语行例外**（2026-11-04 用户独立复核南京病理实测定案）：命中
    _USAGE_NOTE_RE 的行只产出**一条** usage_note 记录（mark 取行内首个符号），
    不进双通道——否则"打★指标为必须满足项…打▲指标为重要指标"这类同句
    双符号说明语会被 ★/▲ 通道各提一条、逐字符守恒被破坏（幽灵符号 +1★+1▲）。

    Returns:
        [{'mark': '★'|'▲', 'text': 条款全文, 'start': int, 'end': int,
          'clause_no': 就近编号头（可空串）,
          'kind': 'requirement'|'usage_note'}, ...] 按 start 升序。
        usage_note = 标记使用说明语（如"▲功能要求须提供证明材料"、节能品目
        清单条款、"打★指标为必须满足项"评分说明），是元说明而非采购要求，
        消费方应从要求清单排除。
    """
    lines = []
    offset = 0
    for raw_line in text.split('\n'):
        lines.append((offset, raw_line))
        offset += len(raw_line) + 1

    hits = []
    # 预扫描：说明语行独立成档（每行仅一条），★/▲ 双通道均跳过这些行
    note_line_idx: set[int] = set()
    for idx, (line_start, line_text) in enumerate(lines):
        if _MARK_RE.search(line_text) and _USAGE_NOTE_RE.search(line_text):
            note_line_idx.add(idx)
            hits.append({
                'mark': _MARK_RE.search(line_text).group(),
                'text': line_text.strip(),
                'start': line_start,
                'end': line_start + len(line_text),
                'clause_no': _find_clause_header_before(text, line_start),
                'kind': 'usage_note',
            })

    # 按标记类型各自独立扫描合并：★ 与 ▲ 是两类不同强度的强制条款，严禁混并
    for mark in ('★', '▲'):
        i = 0
        while i < len(lines):
            line_start, line_text = lines[i]
            if mark not in line_text or i in note_line_idx:
                i += 1
                continue
            j = i
            merged_end = line_start + len(line_text)
            merged_lines = [line_text.strip()]
            while j + 1 < len(lines):
                nxt_start, nxt_text = lines[j + 1]
                # 双向防混并：①说明语行不并入要求记录（终止合并，自成 usage_note）；
                # ②说明语记录也不吞并后续要求行（梁溪★品目清单语后紧跟★参数的形态）
                if (mark in nxt_text and len(nxt_text.strip()) < 300
                        and not _USAGE_NOTE_RE.search(nxt_text)
                        and not _USAGE_NOTE_RE.search('\n'.join(merged_lines))):
                    merged_lines.append(nxt_text.strip())
                    merged_end = nxt_start + len(nxt_text)
                    j += 1
                else:
                    break
            clause_no = _find_clause_header_before(text, line_start)
            merged_text = '\n'.join(merged_lines)
            hits.append({
                'mark': mark,
                'text': merged_text,
                'start': line_start,
                'end': merged_end,
                'clause_no': clause_no,
                'kind': 'usage_note' if _USAGE_NOTE_RE.search(merged_text) else 'requirement',
            })
            i = j + 1
    hits.sort(key=lambda h: h['start'])
    return hits


def extract_invalid_bid_clauses(text: str) -> list[dict]:
    """提取无标记措辞型强制条款（全文无任何 ★/▲ 标记）（徐州份核验沉淀的模式 A / 模式 B）。

    Returns:
        [{'pattern': 'A'|'B', 'text': 所在条款全文, 'start': int, 'end': int,
          'clause_no': 编号头（可空）, 'promise_doc': 模式A捕获的承诺函名（可空）}, ...]
        同一条款同时命中 A/B 时按 start 去重（A 优先保留）。
    """
    records = []

    for m in _PATTERN_A_RE.finditer(text):
        start, end, clause_no = _expand_enclosing_clause(text, m.start(), m.end())
        segment = text[m.start():min(len(text), m.end() + 200)]
        promise = ''
        pm = _PROMISE_DOC_RE.search(segment)
        if pm:
            promise = pm.group(1)
        records.append({
            'pattern': 'A', 'text': text[start:end], 'start': start, 'end': end,
            'clause_no': clause_no, 'promise_doc': promise,
        })

    for m in _PATTERN_B_RE.finditer(text):
        start, end, clause_no = _expand_enclosing_clause(text, m.start(), m.end())
        records.append({
            'pattern': 'B', 'text': text[start:end], 'start': start, 'end': end,
            'clause_no': clause_no, 'promise_doc': '',
        })

    records.sort(key=lambda r: (r['start'], 0 if r['pattern'] == 'A' else 1))
    deduped = []
    for r in records:
        if deduped and r['start'] < deduped[-1]['end']:
            continue  # 与上一条（更优先的 A）重叠，丢弃
        deduped.append(r)
    return deduped


def locate_keyword_sections(text: str, keywords: tuple[str, ...],
                            context_after: int = 1200) -> list[dict]:
    """定位关键词出现的区段（资格审查 / 符合性审查 / 评分细则 等）。

    Returns:
        [{'keyword': 命中词, 'start': int, 'excerpt': 命中点后 context_after 字},
         ...] 按 start 升序。
    """
    hits = []
    for kw in keywords:
        begin = 0
        while True:
            idx = text.find(kw, begin)
            if idx < 0:
                break
            hits.append({
                'keyword': kw,
                'start': idx,
                'excerpt': text[idx:idx + context_after],
            })
            begin = idx + len(kw)
    hits.sort(key=lambda h: h['start'])
    return hits


def build_review_checklist(file_path: str) -> dict:
    """对一份招标文件跑确定性层全流程，汇总四类候选。

    Returns:
        {'file': 路径, 'source_type': str, 'pages': int, 'chars': int,
         'chapters': [...], 'marked_clauses': [...], 'invalid_bid_clauses': [...],
         'qualification_hits': [...], 'compliance_hits': [...],
         'eval_chapter': {'index','title','start'} | None, 'scoring_hits': [...]}
    """
    doc = load_document(file_path)
    text = doc.text
    chapters = locate_chapters(text)
    # 评标章：过滤目录行（目录条目带点线引导符或以页码结尾），取正文最后一处命中
    eval_candidates = [ch for ch in chapters if any(k in ch['title'] for k in _EVAL_CHAPTER_KEYS)]
    real = [ch for ch in eval_candidates
            if '…' not in ch['title'] and not re.search(r'[.·\t]*\d+\s*$', ch['title'])]
    eval_chapter = real[-1] if real else (eval_candidates[-1] if eval_candidates else None)
    return {
        'file': file_path,
        'source_type': doc.source_type,
        'pages': doc.pages,
        'chars': len(text),
        'chapters': chapters,
        'marked_clauses': extract_marked_clauses(text),
        'invalid_bid_clauses': extract_invalid_bid_clauses(text),
        'qualification_hits': locate_keyword_sections(text, _QUALIFY_KEYS),
        'compliance_hits': locate_keyword_sections(text, _COMPLIANCE_KEYS),
        'eval_chapter': eval_chapter,
        'scoring_hits': locate_keyword_sections(text, ('评分细则', '评分标准', '评分办法')),
    }
