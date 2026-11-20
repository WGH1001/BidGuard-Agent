# -*- coding: utf-8 -*-
"""F1 LLM 分段抽取层 —— 确定性层圈选候选，LLM 逐段结构化，quote 溯源锁账。

设计要点（对应 ground_truth/_README.md 已知边界）：
1. **分段不整本**：只把确定性层圈出的候选段扩窗后送 LLM（R-01 防注意力衰减），
   绝不整本招标文件丢进上下文；
2. **扩窗吸附**：确定性记录的 text 可能是 PDF 断行截短片段（如 `▲残值回`），
   按 start 偏移向前取条款号区、向后取续行区组成窗口，LLM 在窗口内摘录完整条款；
3. **溯源锁账**：LLM 输出的 full_text 强制通过 quote_verifier 校验（逐字摘自
   窗口原文），不通过的标记 `quote_verified=False` 并保留原始候选——大模型的
   结论同样必须可溯源，引用不到原文的一律不采信；
4. **0 Token 可回退**：client=None 时仅返回确定性层结果，LLM 层故障不阻塞主链。

签名约定（业务侧唯一入口）：
    extract_tender_with_llm(file_path, client=None, env=None) -> dict
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
import json
import os
import re
from typing import Callable, Optional

from core.llm_gateway import get_llm_client
from core.quote_verifier import verify_quote
from core.tender_extractor import (
    _parse_cn_numeral_int,
    build_review_checklist,
    extract_scoring_tables,
    locate_keyword_sections,
    locate_chapters,
)

# 扩窗半径：向前吸附条款编号头，向后吸附断行续写（PDF 表格单元格断行实测
# 截短片段仅 3-4 字，其完整条款续行集中在 start 后数百字符内）
_WINDOW_BEFORE = 120
_WINDOW_AFTER = 700

_STRUCTURE_PROMPT = """你是招标文件审查专家。下面是从招标文件中按字符偏移截取的一个【原文窗口】，
其中包含一条候选条款（★=实质性强制参数，▲=重要参数/主要标的）。请把这条候选
结构化为一个 JSON 对象。

【原文窗口】
{window}

【确定性提取的候选文本】
{candidate}

要求：
1. is_requirement：该候选是否为投标人须逐条响应的实质要求。标记使用说明语
   （如"标注▲的标的为主要标的"）、附表行标记碎片（如行尾★的行业名）均为 false；
   ⚠️ 注意：候选文本可能被 PDF 表格断行截短（如候选"▲资金使"实为窗口中
   "▲资金使用监测——对资金使用的情况进行监测…"功能项的首段），判定前必须
   先在窗口中向后查找其完整条款行，基于完整内容判定，不要因候选短而误判为碎片；
2. clause_type：star_param（★强制参数）/ triangle_param（▲重要参数）/
   qualification（资格审查项）/ compliance（符合性审查项）/ scoring（评分项）/ other；
3. consequence：违反/满足该条款的评分后果——veto（不满足即投标无效/否决）/
   deduction（不满足扣分）/ bonus（满足加分）/ none（无评分后果）；
4. clause_no：条款编号（如 "★1)" "（十四）" "2.1"，窗口中有则逐字抄录，无则空串）；
5. full_text：该条款在窗口中的**完整原文**——逐字摘录，含标记符号，从条款开头
   到条款结束（窗口截断则以窗口末尾为界），严禁改写、缩写、翻译或补写；
6. reason：30 字以内判定理由。

只输出 JSON，不要任何其他文字。"""

_JSON_BLOCK_RE = re.compile(r'\{.*\}', re.DOTALL)


def _parse_llm_json(content: str) -> Optional[dict]:
    """宽松解析 LLM 输出中的 JSON 对象（容忍 markdown 代码块与前后杂文字）。"""
    if not content:
        return None
    m = _JSON_BLOCK_RE.search(content)
    if not m:
        return None
    try:
        obj = json.loads(m.group())
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _expand_window(text: str, start: int, end: int) -> str:
    """按记录偏移截取 LLM 窗口：向前吸附条款编号区，向后吸附续行区。"""
    lo = max(0, start - _WINDOW_BEFORE)
    hi = min(len(text), end + _WINDOW_AFTER)
    return text[lo:hi]


def structure_candidate(window: str, candidate: dict, client) -> dict:
    """单个候选段结构化：LLM 判定 + quote 溯源校验。

    Returns:
        原 candidate 的浅拷贝，追加字段：
        - structured: LLM 判定结果 dict（is_requirement/clause_type/clause_no/
          full_text/reason；解析失败时为 None）
        - quote_verified: bool，full_text 是否通过溯源校验
        - full_text: 采信的完整条款文本——溯源通过取 LLM 摘录，否则回退候选原文
    """
    out = dict(candidate)
    prompt = _STRUCTURE_PROMPT.format(window=window, candidate=candidate['text'])
    resp = client.chat(
        [{'role': 'user', 'content': prompt}],
        model_tier='flash',
    )
    parsed = _parse_llm_json(getattr(resp, 'content', '') or '')
    out['structured'] = parsed

    full_text = ''
    if parsed and isinstance(parsed.get('full_text'), str):
        full_text = parsed['full_text'].strip()

    if parsed and full_text and verify_quote(full_text, window):
        out['quote_verified'] = True
        out['full_text'] = full_text
    else:
        # 溯源不过：不采信 LLM 摘录，回退候选原文（引用不到原文的一律不采信）
        out['quote_verified'] = False
        out['full_text'] = candidate['text']
        if parsed and full_text:
            parsed['full_text'] = ''
    return out


def extract_tender_with_llm(
    file_path: str,
    client=None,
    env: Optional[str] = None,
    *,
    open_date_fallback: bool = False,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """F1 主入口：确定性层圈选 → LLM 逐段结构化。

    Args:
        file_path: 招标文件路径（docx/pdf/txt）。
        client: LLMClient 实例；None 时自动 get_llm_client(env)。
        env: 模型环境名（demo=智谱 / prod=内网网关），client 传入时忽略。
        open_date_fallback: E23 为 True 且确定性层未识别到开标日期时，用局部窗向 LLM 兜底提取并过 verify_quote。

    Returns:
        build_review_checklist 的结果 + 'llm_structured'（结构化后的 requirement
        列表，含 usage_note/table_marker 原样保留不送 LLM）+ 'llm_usage'（成本）。
        client 为 None 时仅返回确定性层结果（llm_structured 缺省）。
    """
    result = build_review_checklist(file_path)

    reqs = [c for c in result['marked_clauses'] if c.get('kind') == 'requirement']
    # 模式 A/B 措辞型候选同样送结构化（无标记语料的实质要求全靠它们）
    wording = list(result['invalid_bid_clauses'])

    def _get_full_text() -> str:
        txt = result['text'] if 'text' in result else None
        if txt is None:
            # build_review_checklist 不回传全文，按记录偏移直接从文件解析一次
            from core.parser import parse_document
            txt = parse_document(file_path).text
        return txt

    if not reqs and not wording:
        calls = 0
        if open_date_fallback and not result.get('open_date'):
            if client is None:
                client = get_llm_client(env)
            full_text = _get_full_text()
            od_ev = extract_open_date_fallback_with_llm(full_text, client)
            if od_ev:
                result['open_date'] = od_ev['open_date']
                result['open_date_source'] = 'tender_llm'
                result['open_date_quote'] = od_ev['quote']
                calls += int(od_ev.get('llm_calls') or 1)
        result['llm_structured'] = []
        result['llm_usage'] = {'calls': calls}
        return result

    if client is None:
        client = get_llm_client(env)

    full_text = _get_full_text()

    candidates = reqs + wording
    total_cands = len(candidates)
    if progress_cb is not None and total_cands > 0:
        progress_cb(0, total_cands)

    structured: list[dict] = []
    if total_cands > 0:
        raw_conc = (os.getenv('BIDGUARD_F1_CONCURRENCY') or '').strip()
        max_workers = max(1, min(16, int(raw_conc))) if raw_conc.isdigit() else 8
        workers = min(max_workers, total_cands)
        ordered_results: list[Optional[dict]] = [None] * total_cands

        def _eval_one(orig_idx: int, cand_item: dict) -> tuple[int, dict]:
            win = _expand_window(full_text, cand_item['start'], cand_item['end'])
            return orig_idx, structure_candidate(win, cand_item, client)

        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            futs = [pool.submit(_eval_one, i, c) for i, c in enumerate(candidates)]
            done_cnt = 0
            for fut in as_completed(futs):
                orig_idx, res_item = fut.result()
                ordered_results[orig_idx] = res_item
                done_cnt += 1
                if progress_cb is not None:
                    progress_cb(done_cnt, total_cands)
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            pool.shutdown(wait=True)
        structured = [r for r in ordered_results if r is not None]

    extra_calls = 0
    if open_date_fallback and not result.get('open_date'):
        od_ev = extract_open_date_fallback_with_llm(full_text, client)
        if od_ev:
            result['open_date'] = od_ev['open_date']
            result['open_date_source'] = 'tender_llm'
            result['open_date_quote'] = od_ev['quote']
            extra_calls += int(od_ev.get('llm_calls') or 1)

    usage = client.total_usage() if hasattr(client, 'total_usage') else {}
    result['llm_structured'] = structured
    result['llm_usage'] = {
        'calls': len(structured) + extra_calls,
        'usage': usage,
    }
    return result


# ── 评分细则结构化（F1 第四张清单，段落式语料的 LLM 兜底通道）──────────────
_SCORE_WINDOW = 1500          # 评分关键词锚点开窗半径
_SCORE_REGION_MIN = 1000      # 评标章健康阈值：小于此值视为假章节头，改用锚点开窗
_SCORE_CHUNK = 4000           # 章节区切块大小
_SCORE_MAX_WINDOWS = 8        # 单文件窗口上限（防超长语料爆调用）

_SCORING_PROMPT = """你是招标文件评标专家。下面是招标文件【评标办法/评分标准】区段的
一个原文窗口。请把窗口中所有评分项提取为 JSON 数组，每项一个对象：

【原文窗口】
{window}

每项字段：
1. factor：评分因素名称（如"价格分""技术方案"）；
2. detail：该评分项的评分标准**原文**——逐字摘录自窗口（可跨句），严禁改写缩写；
3. score：分值或分值范围的原文（如 "20"、"0-5"、"2分"，无则空串）；
4. score_max：该评分项的最高分值数字（阿拉伯数字，如 20、5；无法确定用 null）；
5. evidence_material：投标时须提供的证明材料原文（窗口中有则摘录，无则空串）；
6. consequence：veto / deduction / bonus / none。

注意：窗口可能不含完整评分表（如表格式评分表不在此窗口），此时输出 []。
只输出 JSON 数组，不要任何其他文字。"""


def _merge_spans(spans: list, gap: int = 800) -> list:
    """区间并集：间距小于 gap 的相邻跨度合并（同一评分表只落进一个窗，
    从源头消灭跨窗重复提取）。"""
    if not spans:
        return []
    spans = sorted(spans)
    merged = [list(spans[0])]
    for lo, hi in spans[1:]:
        if lo <= merged[-1][1] + gap:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [tuple(s) for s in merged]


_OPEN_DATE_LLM_CLUE_RE = re.compile(
    r'开标|截标|截止时间|截止日期|递交|响应文件|磋商|谈判|招标公告|采购公告|投标须知|更正|延期'
)
_OPEN_DATE_LLM_HINT_RE = re.compile(
    r'开标|截标|截止时间|截止日期|递交.{0,10}(?:时间|截止)|更正|延期|变更公告|磋商时间|谈判时间|开启'
)
_SNIPPET_ARABIC_DATE_RE = re.compile(
    r'(?<!\d)(\d{4})\s*[年\-/\.]\s*(\d{1,2})\s*[月\-/\.]\s*(\d{1,2})\s*日?(?!\d)'
)
_SNIPPET_CN_DATE_RE = re.compile(
    r'([〇零O一二三四五六七八九]{4})\s*年\s*'
    r'([一二三四五六七八九十]{1,2})\s*月\s*'
    r'([一二三四五六七八九十廿]{1,3})\s*日?'
)

_OPEN_DATE_PROMPT = """你是招标文件审查专家。正则规则未能从文档中确定性提取到【开标/截标日期】，下面给出从招标文件局部截取的关键片段窗口（含前文概览与时间锚点周边）。请从中提取真正的【开标日期】（或提交投标/响应文件的截止日期）。

【招标文件局部原文窗口】
{window}

要求（严格遵守）：
1. 必须区分“开标/截标/提交响应文件截止时间”与“公告发布时间、获取采购文件时间、质疑期限、免费质保期、投标有效期”；
2. open_date：规范格式 YYYY-MM-DD（如 "2026-10-16"）；若窗口中无明确开标/截标年月日，输出 null；
3. quote：包含该日期及锚点词的原文片段，必须从上方窗口【逐字连续摘录】（保持中文大写数字或阿拉伯数字原样，严禁改写或补写），长度不少于 6 字；若 open_date 为 null，输出空串。

只输出 JSON 对象，不要任何其他文字：
{{"open_date": "YYYY-MM-DD 或 null", "quote": "逐字原文摘录或空串"}}"""


def _extract_iso_dates_in_snippet(snippet: str) -> set[str]:
    """从逐字摘录片段中确定性解析所有有效 YYYY-MM-DD 日期，用于反幻觉交叉校验。"""
    found: set[str] = set()
    if not snippet:
        return found

    for m in _SNIPPET_ARABIC_DATE_RE.finditer(snippet):
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if 2020 <= d.year <= 2035:
                found.add(d.strftime('%Y-%m-%d'))
        except ValueError:
            continue

    for m in _SNIPPET_CN_DATE_RE.finditer(snippet):
        y_raw = m.group(1).replace('O', '〇')
        y = _parse_cn_numeral_int(y_raw, is_year=True)
        mo = _parse_cn_numeral_int(m.group(2))
        da = _parse_cn_numeral_int(m.group(3))
        if y is None or mo is None or da is None:
            continue
        try:
            d = date(y, mo, da)
            if 2020 <= d.year <= 2035:
                found.add(d.strftime('%Y-%m-%d'))
        except ValueError:
            continue

    return found


def extract_open_date_fallback_with_llm(text: str, client) -> Optional[dict]:
    """E23 规则正则未命中时的局部窗 LLM 开标日期兜底（严格过 verify_quote + 日期一致性校验）。

    约束：
    - 绝不发送全文：仅拼接前文窗口与关键词锚点 ±350/+700 合并窗（最多 3 窗，总计 ≤ 5000 字）；
    - 无招标时间线索或 client 为 None 时零调用直接返回 None；
    - quote 未过 verify_quote() 连续子串核验，或 quote 内不含对应有效日历日期时，一律拒收。
    """
    if not text or client is None:
        return None
    if not _OPEN_DATE_LLM_CLUE_RE.search(text):
        return None

    spans: list[tuple[int, int]] = [(0, min(len(text), 2200))]
    for m in _OPEN_DATE_LLM_HINT_RE.finditer(text):
        spans.append((max(0, m.start() - 350), min(len(text), m.end() + 700)))
        if len(spans) >= 12:
            break

    merged = _merge_spans(spans, gap=300)
    windows: list[str] = []
    total_chars = 0
    for lo, hi in merged[:3]:
        win = text[lo:min(hi, lo + 2200)]
        if total_chars + len(win) > 5000:
            win = win[:max(0, 5000 - total_chars)]
        if len(win) < 20:
            continue
        windows.append(win)
        total_chars += len(win)
        if total_chars >= 5000:
            break

    if not windows:
        return None

    combined = '\n---\n'.join(windows)
    prompt = _OPEN_DATE_PROMPT.format(window=combined)
    try:
        resp = client.chat([{'role': 'user', 'content': prompt}], model_tier='flash')
        obj = _parse_llm_json(getattr(resp, 'content', '') or '')
    except Exception:
        return None

    if not obj:
        return None

    raw_date = str(obj.get('open_date') or '').strip()
    if not raw_date or raw_date.lower() in ('null', 'none') or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw_date):
        return None
    try:
        parsed_d = date.fromisoformat(raw_date)
    except ValueError:
        return None
    if not (2020 <= parsed_d.year <= 2035):
        return None

    quote = str(obj.get('quote') or '').strip()
    if len(quote) < 6:
        return None
    if not any(verify_quote(quote, w) for w in windows):
        return None

    # 铁律双锁：quote 必须真实通过原文核验，且其中包含确定性可解析出的同一个日期
    if raw_date not in _extract_iso_dates_in_snippet(quote):
        return None

    pos = text.find(quote)
    return {
        'open_date': raw_date,
        'quote': quote,
        'quote_verified': True,
        'source': 'tender_llm',
        'offset': pos if pos >= 0 else None,
        'llm_calls': 1,
    }


def _scoring_windows(text: str, eval_chapter: Optional[dict],
                     scoring_hits: list) -> list[str]:
    """评分区段开窗：章节区健康用章节；失效用关键词锚点 ±开窗半径。
    所有跨度先并集合并再切块（≤6000 字/窗），杜绝跨窗重复提取。"""
    spans = []
    if eval_chapter:
        start = eval_chapter['start']
        nxt = re.search(r'\n第[一二三四五六七八九十百零两]+章', text[start + 10:])
        end = start + 10 + nxt.start() if nxt else len(text)
        if end - start >= _SCORE_REGION_MIN:
            spans.append((start, end))
    if not spans:
        # 锚点词除评分细则/标准/办法外，加"综合评分法"等评分区段特征词
        # （语料实证：徐州/芜湖/滁州物业的评标章定位被假章节头截断）
        for h in locate_keyword_sections(text, (
                '评分细则', '评分标准', '评分办法', '综合评分法')):
            spans.append((max(0, h['start'] - _SCORE_WINDOW),
                          min(len(text), h['start'] + _SCORE_WINDOW)))
    windows = []
    for lo, hi in _merge_spans(spans)[:_SCORE_MAX_WINDOWS]:
        for a in range(lo, hi, _SCORE_CHUNK):
            windows.append(text[a:min(a + _SCORE_CHUNK, hi)])
    return windows


def _parse_llm_array(content: str) -> list:
    """宽松解析 LLM 输出中的 JSON 数组。"""
    if not content:
        return []
    m = re.search(r'\[.*\]', content, re.DOTALL)
    if not m:
        return []
    try:
        arr = json.loads(m.group())
        return arr if isinstance(arr, list) else []
    except json.JSONDecodeError:
        return []


def extract_scoring_with_llm(file_path: str, client=None, env: Optional[str] = None) -> dict:
    """F1 第四张清单：评分细则结构化（表格型 0 Token + 段落型 LLM 兜底）。

    Returns:
        {'scoring_tables': 表格型解析结果（0 Token，docx 专享）,
         'scoring_prose': LLM 段落式评分项（含 quote_verified 逐项标记）,
         'scoring_llm_usage': {'calls': int, 'usage': dict}}
    """
    from core.parser import parse_document
    from core.tender_extractor import locate_chapters as _lc

    tables = extract_scoring_tables(file_path)
    text = parse_document(file_path).text
    chapters = _lc(text)
    eval_real = [c for c in chapters
                 if '评' in c['title'] and '…' not in c['title']
                 and not re.search(r'[.·\t]*\d+\s*$', c['title'])]
    eval_chapter = eval_real[-1] if eval_real else None
    hits = locate_keyword_sections(text, ('评分细则', '评分标准', '评分办法'))
    windows = _scoring_windows(text, eval_chapter, hits)

    prose = []
    calls = 0
    if windows:
        if client is None:
            client = get_llm_client(env)
        for win in windows:
            resp = client.chat(
                [{'role': 'user', 'content': _SCORING_PROMPT.format(window=win)}],
                model_tier='flash',
            )
            calls += 1
            for item in _parse_llm_array(getattr(resp, 'content', '') or ''):
                if not isinstance(item, dict):
                    continue
                detail = str(item.get('detail', '')).strip()
                verified = bool(detail) and verify_quote(detail, win)
                item['quote_verified'] = verified
                if not verified:
                    item['detail_raw'] = detail
                    item['detail'] = ''            # 溯源不过不采信，清空待人工
                prose.append(item)

        # 评分项跨窗去重：多个锚点窗可能覆盖同一张评分表（滁州物业实测同一
        # 明细被相邻窗重复提取，295 分虚高），按 detail 归一化前缀去重
        deduped = []
        seen_keys = set()
        for item in prose:
            key = re.sub(r'\s', '', str(item.get('detail', '')))[:30] \
                or str(item.get('factor', ''))[:20]
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped.append(item)
        prose = deduped

    usage = client.total_usage() if hasattr(client, 'total_usage') and calls else {}
    return {
        'scoring_tables': tables,
        'scoring_prose': prose,
        'scoring_llm_usage': {'calls': calls, 'usage': usage},
    }
