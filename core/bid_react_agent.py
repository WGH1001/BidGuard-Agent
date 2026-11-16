# -*- coding: utf-8 -*-
"""F7 标书专家 ReAct 追问引擎（★大模型③）—— 跨双书工具检索与溯源问答（E2 混合检索 + E4 第6工具 cite_regulation + E18 双轨页码 + E21 多轮流式 + E24 第7工具 search_attachment 与原生 VLM 双轨问答）。

模块定位（对应 PRD-02 §7 F7、E2/E4/E18/E21/E24 任务卡）：
1. 工具层（共 7 组确定性/混合检索工具函数，既有 6 工具签名保持向后兼容）：
   - search_tender(keyword, ..., after_offset=0) / search_bid(keyword, ..., after_offset=0)：双书检索（支持 after_offset 续检）；
   - locate_chapter(text, chapter_title, after_offset=0)：章节定位；
   - recalc_price(items, ...)：调用 PriceSandbox 复算报价；
   - check_cert_expiry(cert_text, open_date, ...)：调用 F3 scan_qualifications 核验效期；
   - cite_regulation(keyword, article_no, ...)：E4 第 6 工具，检索法规库返回条文原文+法律名+条号；
   - search_attachment(keyword, attachment_id='', ...)：E24 新增第 7 工具，检索会话补充文档与图片 OCR 文本池。
2. ReAct 循环与多模态双轨（E21 / E24）：
   - 支持注入会话多轮上下文 history（仅注入纯文本问答，严禁在历史消息重复发图）；
   - 当本轮携带图片附件且 client.supports_vision=True 时，仅在当轮末尾 user 消息附带内存缩图后的 image_url；
   - _parse_llm_json 支持 strict=False 容错未转义换行，且在解析失败时严禁将含 "action"/"thought" 的残缺工具 JSON 直接吐给用户；
   - final 输出 quotes（含 source='attachment'）100% 强制走 verify_quote 溯源，纯视觉结论显式标 quote_verified=False。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
from pathlib import Path
import re
from typing import Any, Iterator, Optional

from core.hybrid_retriever import expand_query_synonyms, hybrid_retrieve_windows
from core.llm_gateway import encode_image_for_vlm
from core.parser import parse_document, parse_qa_attachment
from core.price_sandbox import (
    CENT,
    PriceSandbox,
    _parse_sandbox_amount,
    _parse_sandbox_qty,
)
from core.qualification_scanner import scan_qualifications
from core.quote_verifier import verify_quote
from core.regulation_library import cite_regulation as _lib_cite_regulation
from core.response_checker import build_unified_page_context, resolve_unified_page
from core.tender_extractor import locate_chapters, locate_keyword_sections

_SEARCH_WINDOW = 200
_MAX_HITS_PER_SEARCH = 5
MAX_PARALLEL_ACTIONS_PER_ROUND = 4
MAX_UNGROUNDED_BOUNCE_PER_TURN = 1
_JSON_BLOCK_RE = re.compile(r'\{.*\}', re.DOTALL)

# E34：识别涉及本项目核验的查证信号（用于首轮未检索即直答时的确定性回弹拦截）
_PROJECT_VERIFY_SIGNAL_RE = re.compile(
    r'金额|限价|报价|单价|合价|总价|预算|质保|保修|维保|售后|工期|交货|交付|'
    r'资质|营业执照|证书|社保|缴存|业绩|评分|偏离|★|▲|开标|'
    r'投标文件|招标文件|投标书|招标书|违约|废标|无效投标|第[一二三四五六七八九十百\d]+条'
)
_REORG_META_RE = re.compile(
    r'整理|上一轮|上轮|刚才|刚刚|上述|前述|总结|摘要|汇总|转成表格|表格形式|对比表|清单|列出来|排版|改写'
)
_UNGROUNDED_BOUNCE_PROMPT = (
    '检测到您的问题涉及具体招投标条款或文件内容核验，但当前尚未执行任何工具检索。'
    '请至少调用 search_tender / search_bid / locate_chapter / cite_regulation 获取原文窗口后再作答，严禁未查正文直接回答。'
)

# E4 / E24：F7 允许调用的 7 大工具名称白名单
ALLOWED_REACT_TOOLS = (
    'search_tender',
    'search_bid',
    'locate_chapter',
    'recalc_price',
    'check_cert_expiry',
    'cite_regulation',
    'search_attachment',
)

_WINDOW_SEARCH_TOOLS = frozenset({
    'search_tender',
    'search_bid',
    'locate_chapter',
    'search_attachment',
})

_QUESTION_STOPWORDS = (
    '请问', '招标文件', '招标书', '投标文件', '投标书', '我们', '我方', '甲方',
    '要求', '响应', '在哪里', '在哪', '哪一页', '哪页', '是多少', '多少', '什么',
    '几年', '几个月', '是否', '有没有', '如何', '怎么', '的', '了', '吗', '呢',
    '附件', '图片', '截图', '补充文件', '补遗', '看看', '里', '中',
)

# E21 §2.1：指代与承接上文特征词（供 0-Token 离线降级结合上一轮用户问题补充关键词）
_FOLLOWUP_PRONOUN_KEYS = (
    '刚才', '刚刚', '上一轮', '上轮', '上面', '前面', '之前', '这两个', '那两个',
    '它们', '这些', '那些', '这个', '那个', '怎么规定', '为什么', '怎么不', '还有呢', '问了什么', '它',
)

# E21 / E33：前端 UI 三挡（low/medium/high）映射为上游推理深度（low/high/max）与思考回合上限：
# - 保留 low -> 7 轮兼容 E21 既有门禁契约，同时向上放开 medium -> 8 轮、high -> 12 轮；
# - 配合单回合最多 4 路工具并行调度与早停自律规则，避免机械烧满轮次。
_EFFORT_UPSTREAM_MAP: dict[str, tuple[str, int]] = {
    'low': ('low', 7),
    'medium': ('high', 8),
    'high': ('max', 12),
    'max': ('max', 12),
}

_REACT_SYSTEM_PROMPT = """你是招投标双书合规审查专家（ReAct Agent）。你可以调用以下 7 个确定性工具来回答用户关于《招标文件》、《投标文件》、会话补充附件（文档/图片）与招投标法律法规的问题：

可用工具列表：
1. "search_tender"：在招标文件中检索关键词，action_input 为关键词字符串或 {"keyword": "...", "after_offset": 0}（若前5处命中仅覆盖总则，可传 after_offset 续检后文）；
2. "search_bid"：在投标文件中检索关键词，action_input 为关键词字符串或 {"keyword": "...", "after_offset": 0}；
3. "locate_chapter"：定位章节标题，action_input 为章节关键词字符串或 {"chapter_title": "...", "doc": "tender"|"bid", "after_offset": 0}；
4. "recalc_price"：调用报价算术沙盒复算分项报价，action_input 为对象 {"items": [{"name": "分项名", "qty": "数量", "unit_price": "单价", "total_price": "合价"}], "table_total": "合计(可选)", "bid_total": "总报价(可选)"}（注意 items 每项必须包含 qty、unit_price、total_price）；
5. "check_cert_expiry"：调用证照效期核验器，action_input 为 {"cert_text": "...", "open_date": "YYYY-MM-DD"}；
6. "cite_regulation"：在招投标法规库中检索法条原文，action_input 为 {"keyword": "...", "article_no": "第X条"}；
7. "search_attachment"：在用户上传的会话补充附件（含文档与图片 OCR 文本）中检索关键词，action_input 为 {"keyword": "...", "attachment_id": "可选指定附件ID", "after_offset": 0}；
8. "final"：当你已拿到充分原文证据（或已确认双书中确实未检出）准备给出最终回答时使用，action_input 为：
   {"answer": "最终解答正文", "quotes": [{"source": "tender"|"bid"|"attachment", "attachment_id": "当source为attachment时填附件ID", "quote": "逐字摘自原文的引用句"}]}

   【快速通道例外（第 1 轮可零工具直出 final，需在 action_input 中额外声明 "route": "direct_meta" 且 quotes 留空）】仅限以下三类无需翻查本项目标书正文的问题：
   (a) 系统能力与交互咨询（如“你能查什么”“这个标签什么含义”）；
   (b) 通用招投标法规与流程概念题（不涉及本项目具体文件条款，如“质疑期限一般是几天”）；
   (c) 仅对上方已有对话历史结果做重排/整理/格式转换（如“把上一轮结果整理成对比表格”）。
   ⚠️ 明令禁止直答：凡涉及本项目金额/限价/报价、质保期/工期/交货期、资质/社保/证照、★/▲/偏离、开标时间或我方响应情况的问题，严禁第 1 轮直答，必须先调用检索工具核实原文！

每轮你必须只输出一个合法的 JSON 对象（字符串内部严禁出现未转义的裸换行），支持【单动作】或【同轮并行多工具（最多 4 个）】：
- 单动作格式（收口 final 或单工具调用）：
  {"thought": "思考过程", "action": "tool_name_or_final", "action_input": ...}
- 并行多工具格式（例如同轮同时查招标要求与投标响应、或一次多组同义词检索，最多 4 项）：
  {"thought": "同时核对招标与投标条款", "actions": [{"action": "search_tender", "action_input": "质保期"}, {"action": "search_bid", "action_input": "质保期"}]}

⚠️ 自律与铁律：
1. 溯源铁律：final 中的 quotes 必须 100% 逐字抄录自工具返回的原文窗口，严禁改写或编造！
2. 早停纪律：一旦关键条款的招标要求与投标响应原文已取得，立即在下一轮输出 action="final" 结束，无需凑满轮次；若连续 2 轮仅命中无关历史业绩合同（如其他项目合同扫描件）或返回空结果，应停止无效换词并如实调用 action="final" 说明未检出及命中片段归属。只输出 JSON。"""


def _parse_llm_json(content: str) -> Optional[dict]:
    """E24 §2.5.4：解析 LLM 返回的 JSON 对象；当严格 json.loads 因字符串内含未转义真实换行/制表符失败时，
    自动降级使用 json.loads(..., strict=False) 容错解析。
    """
    if not content:
        return None
    m = _JSON_BLOCK_RE.search(content)
    if not m:
        return None
    raw_block = m.group()
    for is_strict in (True, False):
        try:
            obj = json.loads(raw_block, strict=is_strict)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def _looks_like_unparsed_tool_json(raw_content: str) -> bool:
    """E24 §2.5.4：判断无法解析的原始模型输出是否属于残缺的工具调用 JSON（含 "action"/"actions" 与 "thought"/"action_input"），
    严禁将此类原始 JSON 源码直接当作自然语言回答吐给用户。
    """
    s = (raw_content or '').strip()
    if not s:
        return False
    if ('"action"' in s or '"actions"' in s) and (
        '"thought"' in s or '"action_input"' in s or any(t in s for t in ALLOWED_REACT_TOOLS)
    ):
        return True
    if s.startswith('{') and ('action' in s and ('action_input' in s or 'actions' in s)):
        return True
    return False


def _coerce_action_input(action_input: Any) -> Any:
    """E23 §2.4.3 / E24：若 LLM 将 action_input 输出为字符串化 JSON（如 '{\"items\": [...]}'），
    自动尝试 json.loads(strict=False) 解包为 dict/list。
    """
    if not isinstance(action_input, str):
        return action_input
    s = action_input.strip()
    if s.startswith('```'):
        s = re.sub(r'^```(?:json)?\s*|\s*```$', '', s, flags=re.IGNORECASE).strip()
    if (s.startswith('{') and s.endswith('}')) or (s.startswith('[') and s.endswith(']')):
        try:
            parsed = json.loads(s, strict=False)
            if isinstance(parsed, (dict, list)):
                return parsed
        except Exception:
            pass
    return action_input


def _offset_to_page(offset: Optional[int], page_spans: list[tuple[int, int, int]]) -> Optional[int]:
    """向后兼容辅助函数：当调用方直接传入旧版 [(start, end, page_no)] 列表时换算页码。"""
    if offset is None or offset < 0 or not page_spans:
        return None
    for lo, hi, pno in page_spans:
        if lo <= offset <= hi:
            return pno
    return page_spans[-1][2] if offset >= page_spans[-1][0] else None


def _resolve_page_adaptive(
    offset: Optional[int],
    ctx_or_spans: Optional[dict[str, Any] | list[tuple[int, int, int]]],
) -> Optional[int]:
    """E18 自适应页码解析：
    - 若传入 unified page context (dict)，调用 resolve_unified_page(offset, ctx)[0]（支持 PDF + docx 双轨页码）；
    - 若传入旧版 page_spans (list)，调用 _offset_to_page(offset, spans) 确保老单测 100% 零回归。
    """
    if isinstance(ctx_or_spans, dict):
        return resolve_unified_page(offset, ctx_or_spans)[0]
    return _offset_to_page(offset, ctx_or_spans or [])


def _format_history_messages(
    history: Optional[list[dict[str, Any]]],
    max_turns: int = 6,
    max_chars_per_msg: int = 800,
) -> list[dict[str, str]]:
    """E21 §2.1 / E24 §1.2：从会话历史提取 role in ('user','assistant') 的纯文本消息，
    历史消息严禁重复携带 base64 图片（仅保留纯文本内容，过滤空内容与 loading 占位）。
    """
    if not history or not isinstance(history, list):
        return []

    cleaned: list[dict[str, str]] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        if item.get('loading'):
            continue
        role = str(item.get('role') or '').strip().lower()
        if role not in ('user', 'assistant'):
            continue
        raw_content = item.get('content')
        if isinstance(raw_content, list):
            text_pieces = [
                str(b.get('text') or '')
                for b in raw_content
                if isinstance(b, dict) and b.get('type') == 'text'
            ]
            content = '\n'.join(p for p in text_pieces if p).strip()
        else:
            content = str(raw_content or '').strip()
        if not content:
            continue
        if len(content) > max_chars_per_msg:
            content = content[:max_chars_per_msg] + '…'
        cleaned.append({'role': role, 'content': content})

    max_msgs = max(2, int(max_turns) * 2)
    return cleaned[-max_msgs:]


def _resolve_effort_and_rounds(
    reasoning_effort: Optional[str],
    max_rounds: Optional[int] = None,
    client: Any = None,
) -> tuple[Optional[str], int]:
    """E21 / E33：将前端三挡（low/medium/high）映射为上游推理深度与思考回合上限。

    - 未传 max_rounds（None）时：low=7, medium=8, high=12；未传挡位时默认 7。
    - 显式传入整数 max_rounds（如测试传 6 或 7）时：保持 min(base_limit, gear_round_cap) 向后兼容。
    """
    explicit_cap = max(1, int(max_rounds)) if max_rounds is not None else None
    raw_key = str(reasoning_effort or '').strip().lower()
    if raw_key in _EFFORT_UPSTREAM_MAP:
        default_effort, gear_round_cap = _EFFORT_UPSTREAM_MAP[raw_key]
        mapped_effort = default_effort
        translate_fn = getattr(client, 'translate_reasoning_effort', None) if client is not None else None
        if callable(translate_fn):
            custom_val = translate_fn(raw_key)
            if isinstance(custom_val, str) and custom_val.strip():
                mapped_effort = custom_val.strip()
        resolved_rounds = min(explicit_cap, gear_round_cap) if explicit_cap is not None else gear_round_cap
        return mapped_effort, resolved_rounds
    return None, (explicit_cap if explicit_cap is not None else 7)


def _iter_answer_chunks(answer_text: str, chunk_size: int = 24) -> Iterator[str]:
    """将最终答案按自然语义块切分为流式 delta 片段，供前端呈现平滑打字机效果。"""
    if not answer_text:
        return
    step = max(8, int(chunk_size))
    for i in range(0, len(answer_text), step):
        yield answer_text[i:i + step]


def _extract_domain_tokens(text: str) -> list[str]:
    """从文本中剥离指代承接词与停用词，提取真实业务关键词（不回退默认值）。"""
    cleaned = re.sub(r'[？?！!，,。；;：:“”"\'（）()\s]+', ' ', text or '').strip()
    for pk in _FOLLOWUP_PRONOUN_KEYS:
        cleaned = cleaned.replace(pk, ' ')
    for sw in _QUESTION_STOPWORDS:
        cleaned = cleaned.replace(sw, ' ')
    return [t.strip() for t in cleaned.split() if len(t.strip()) >= 2]


def _extract_keywords_from_question(question: str) -> list[str]:
    """从自然语言问题中提取检索关键词（供 0 Token 降级与多词回退检索使用）。"""
    q = re.sub(r'[？?！!，,。；;：:“”"\'（）()\s]+', ' ', question or '').strip()
    for sw in _QUESTION_STOPWORDS:
        q = q.replace(sw, ' ')
    tokens = [t.strip() for t in q.split() if len(t.strip()) >= 2]
    if not tokens and question:
        clean = re.sub(r'[^\u4e00-\u9fa5A-Za-z0-9]', '', question)
        if len(clean) >= 2:
            tokens = [clean[:4]]
    return tokens or ['质保']


def _safe_int_offset(val: Any) -> int:
    """安全将 after_offset 转为非负整数，避免大模型传入非法偏移字符串时炸毁工具链。"""
    if val is None or isinstance(val, bool):
        return 0
    if isinstance(val, int):
        return max(0, val)
    s = str(val).strip()
    if not s:
        return 0
    m = re.search(r'-?\d+', s)
    if not m:
        return 0
    try:
        return max(0, int(m.group(0)))
    except ValueError:
        return 0


def _search_text_windows(
    text: str,
    keyword: str,
    source_label: str,
    window: int = _SEARCH_WINDOW,
    page_spans: Optional[list[tuple[int, int, int]] | dict[str, Any]] = None,
    client: Any = None,
    after_offset: int = 0,
) -> list[dict[str, Any]]:
    """在文本中检索关键词，返回 ±200 字符窗口、字符偏移与页码（E24：支持 after_offset 续检后文）。"""
    if not text or not keyword:
        return []

    start_pos = _safe_int_offset(after_offset)
    if start_pos >= len(text):
        return []

    kw = str(keyword).strip()
    syn_terms = expand_query_synonyms(kw)
    if kw in text[start_pos:]:
        search_terms = [kw]
    else:
        extracted = _extract_keywords_from_question(kw)
        search_terms = list(dict.fromkeys([kw] + syn_terms + extracted))

    hits: list[dict[str, Any]] = []
    seen_offsets: list[int] = []
    ctx_or_spans = page_spans

    for term in search_terms:
        if not term:
            continue
        start = start_pos
        while len(hits) < _MAX_HITS_PER_SEARCH:
            idx = text.find(term, start)
            if idx < 0:
                break
            if not any(abs(idx - prev) < 60 for prev in seen_offsets):
                seen_offsets.append(idx)
                lo = max(0, idx - window)
                hi = min(len(text), idx + len(term) + window)
                snippet = text[lo:hi].strip()
                real_off = text.find(snippet, lo)
                if real_off < 0:
                    real_off = idx
                hits.append({
                    'source': source_label,
                    'keyword': term,
                    'quote': snippet,
                    'offset': real_off,
                    'next_after_offset': idx + len(term),
                    'page': _resolve_page_adaptive(idx, ctx_or_spans),
                })
            start = idx + len(term)
        if hits:
            break

    if not hits and start_pos == 0:
        hybrid_res = hybrid_retrieve_windows(
            query=kw,
            doc_text=text,
            client=client,
            top_k=_MAX_HITS_PER_SEARCH,
            min_score=0.28,
            window_pad=window,
        )
        for h in hybrid_res:
            off = int(h['offset'])
            hits.append({
                'source': source_label,
                'keyword': kw,
                'quote': h['window'].strip() or h['quote'],
                'offset': off,
                'next_after_offset': off + len(h['quote']),
                'page': _resolve_page_adaptive(off, ctx_or_spans),
            })

    return hits


# ── 工具层：7 组确定性工具函数（支持独立调用与 ReAct 挂载）────────────────

def search_tender(
    keyword: str,
    tender_text: str = '',
    window: int = _SEARCH_WINDOW,
    page_spans: Optional[list[tuple[int, int, int]] | dict[str, Any]] = None,
    after_offset: int = 0,
) -> list[dict[str, Any]]:
    """工具 1a：在招标文件中检索关键词，返回 ±200 字符窗口 + 偏移 + 页码（支持 after_offset 续检）。"""
    return _search_text_windows(
        tender_text, keyword, 'tender', window=window, page_spans=page_spans, after_offset=after_offset
    )


def search_bid(
    keyword: str,
    bid_text: str = '',
    window: int = _SEARCH_WINDOW,
    page_spans: Optional[list[tuple[int, int, int]] | dict[str, Any]] = None,
    after_offset: int = 0,
) -> list[dict[str, Any]]:
    """工具 1b：在投标文件中检索关键词，返回 ±200 字符窗口 + 偏移 + 页码（支持 after_offset 续检）。"""
    return _search_text_windows(
        bid_text, keyword, 'bid', window=window, page_spans=page_spans, after_offset=after_offset
    )


def locate_chapter(
    text: str,
    chapter_title: str,
    after_offset: int = 0,
) -> list[dict[str, Any]]:
    """工具 2：章节定位（复用 tender_extractor.locate_chapters 与 locate_keyword_sections，支持 after_offset）。"""
    if not text or not chapter_title:
        return []
    start_pos = _safe_int_offset(after_offset)
    kw = str(chapter_title).strip()
    chapters = locate_chapters(text)
    matched = [
        {
            'index': ch['index'],
            'title': ch['title'],
            'start': ch['start'],
            'excerpt': text[ch['start']:min(len(text), ch['start'] + 400)],
        }
        for ch in chapters
        if ch['start'] >= start_pos and (kw in ch['title'] or kw in ch['index'])
    ]
    if matched:
        return matched
    sec_hits = locate_keyword_sections(text, (kw,), context_after=400)
    return [h for h in sec_hits if int(h.get('start', 0)) >= start_pos]


def recalc_price(
    items: list[dict[str, Any]],
    table_total=None,
    bid_total=None,
    bid_total_upper: Optional[str] = None,
    price_cap=None,
) -> dict[str, Any]:
    """工具 3：调用 F2 PriceSandbox 复算报价（E23 校验 missing total_price；E31 别名容错 + qty=1 derived 标注）。"""
    if not isinstance(items, list) or len(items) == 0:
        return {
            'passed': False,
            'error': 'recalc_price 需要非空 items 列表，每项须包含 name、qty、unit_price、total_price 字段',
            'findings': [],
        }

    def _first_present(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
        for k in keys:
            val = d.get(k)
            if val is not None and str(val).strip() != '':
                return val
        return None

    normalized_items: list[dict[str, Any]] = []
    derived_fields: list[dict[str, Any]] = []
    sum_of_items = Decimal('0.00')

    for idx, it in enumerate(items, start=1):
        if not isinstance(it, dict):
            return {
                'passed': False,
                'error': f'recalc_price 第 {idx} 项格式错误，须为包含 name、qty、unit_price、total_price 的字典',
                'findings': [],
            }

        item_name = str(_first_present(it, ('name', 'item_name', '名称', '品目名称')) or f"分项{idx}").strip()
        raw_qty = _first_present(it, ('qty', 'quantity', 'count', '数量'))
        if raw_qty is None:
            raw_qty = '1'
        parsed_qty = _parse_sandbox_qty(raw_qty)

        raw_unit = _first_present(it, ('unit_price', 'price', '单价'))
        raw_tp = _first_present(it, ('total_price', 'amount', 'subtotal', '合价', '小计', '金额'))

        if raw_tp is None:
            parsed_unit = _parse_sandbox_amount(raw_unit) if raw_unit is not None else None
            if parsed_qty == Decimal('1') and parsed_unit is not None:
                raw_tp = raw_unit
                derived_fields.append({
                    'index': idx,
                    'name': item_name,
                    'derived_field': 'total_price',
                    'source_field': 'unit_price',
                    'value': str(parsed_unit),
                    'note': '因数量=1，合价由单价自动推算（derived，非标书逐字原文）',
                })
            else:
                return {
                    'passed': False,
                    'error': (
                        f"recalc_price 第 {idx} 项（「{item_name}」）缺少 total_price（合价）字段；"
                        f"请提供完整 {'{name, qty, unit_price, total_price}'} 后再复算"
                    ),
                    'findings': [],
                }

        if raw_unit is None:
            parsed_tp_for_unit = _parse_sandbox_amount(raw_tp)
            if parsed_qty == Decimal('1') and parsed_tp_for_unit is not None:
                raw_unit = raw_tp
                derived_fields.append({
                    'index': idx,
                    'name': item_name,
                    'derived_field': 'unit_price',
                    'source_field': 'total_price',
                    'value': str(parsed_tp_for_unit),
                    'note': '因数量=1，单价由合价自动推算（derived，非标书逐字原文）',
                })

        parsed_tp = _parse_sandbox_amount(raw_tp)
        if parsed_tp is not None:
            sum_of_items += parsed_tp

        normalized_items.append({
            **it,
            'name': item_name,
            'qty': raw_qty,
            'unit_price': raw_unit,
            'total_price': raw_tp,
        })

    sandbox = PriceSandbox(
        items=normalized_items,
        table_total=table_total,
        bid_total=bid_total,
        bid_total_upper=bid_total_upper,
        price_cap=price_cap,
    )
    findings = sandbox.run()
    out: dict[str, Any] = {
        'passed': len(findings) == 0,
        'findings': findings,
        'item_count': len(normalized_items),
        'sum_of_items': str(sum_of_items.quantize(CENT)),
    }
    if derived_fields:
        out['derived'] = True
        out['derived_fields'] = derived_fields
        out['derived_warning'] = (
            '注意：部分分项的 unit_price 或 total_price 系依据数量=1 自动推算（derived），'
            '向用户作答时必须明确标注为推算值，严禁当作标书原文写明的值直接引用。'
        )
    return out


def check_cert_expiry(
    cert_text: str,
    open_date: str,
    required_social_months: Optional[list[str]] = None,
    forbidden_names: Optional[list[str]] = None,
    target_buyer: str = '',
) -> dict[str, Any]:
    """工具 4：调用 F3 scan_qualifications 核验证照有效期与社保/串名。"""
    findings = scan_qualifications(
        text=cert_text,
        open_date=open_date,
        required_social_months=required_social_months,
        forbidden_names=forbidden_names,
        target_buyer=target_buyer,
    )
    return {'passed': len(findings) == 0, 'findings': findings}


def cite_regulation(
    keyword: str = '',
    article_no: str = '',
    law_name: str = '',
    session=None,
) -> dict[str, Any]:
    """工具 6（E4 新增）：检索招投标法规库返回条文原文 + 法律名 + 条号；库空时诚实报"法规库为空"。"""
    return _lib_cite_regulation(
        keyword=keyword,
        article_no=article_no,
        law_name=law_name,
        session=session,
    )


def _normalize_runtime_attachments(
    attachments: Optional[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """E24 §2.5：规范化运行时附件列表，确保每项具备 text 与 page_ctx。"""
    norm_list: list[dict[str, Any]] = []
    for raw in attachments or []:
        if not isinstance(raw, dict):
            continue
        att_id = str(raw.get('attachment_id') or raw.get('id') or '').strip()
        filename = str(raw.get('filename') or raw.get('name') or att_id or '未命名附件').strip()
        file_path = raw.get('file_path') or raw.get('abs_path') or raw.get('path')
        text = raw.get('text')
        is_image = bool(raw.get('is_image', False))
        ocr_chars = int(raw.get('ocr_chars', 0) or 0)
        page_ctx = raw.get('page_ctx')

        if text is None and file_path and Path(str(file_path)).is_file():
            try:
                parsed_att = parse_qa_attachment(file_path)
                text = parsed_att['text']
                is_image = bool(parsed_att['is_image'])
                ocr_chars = int(parsed_att['ocr_chars'])
            except Exception:
                text = ''

        if page_ctx is None and file_path and Path(str(file_path)).is_file() and not is_image:
            page_ctx = build_unified_page_context(file_path)

        text_str = str(text or '')
        if is_image and ocr_chars == 0 and text_str.strip():
            ocr_chars = len(text_str.strip())

        norm_list.append({
            'attachment_id': att_id or filename,
            'filename': filename,
            'file_path': str(file_path) if file_path else None,
            'text': text_str,
            'is_image': is_image,
            'ocr_chars': ocr_chars,
            'has_text': bool(text_str.strip()),
            'page_ctx': page_ctx or {'kind': 'none', 'pdf_spans': [], 'docx_spans': []},
        })
    return norm_list


def search_attachment(
    keyword: str = '',
    attachment_id: str = '',
    attachments: Optional[list[dict[str, Any]]] = None,
    window: int = _SEARCH_WINDOW,
    after_offset: int = 0,
    client: Any = None,
) -> list[dict[str, Any]]:
    """工具 7（E24 新增）：在会话补充附件（含文档与图片 OCR 文本池）中检索关键词。

    - 支持指定 attachment_id 或在全部附件中检索；
    - 当图片附件无 OCR 文字（ocr_chars=0）时，返回明确的元信息说明，不编造图内文字。
    """
    norm_atts = _normalize_runtime_attachments(attachments)
    if not norm_atts:
        return []

    target_id = str(attachment_id or '').strip()
    if target_id:
        candidates = [
            a for a in norm_atts
            if a['attachment_id'] == target_id or a['filename'] == target_id
        ]
    else:
        candidates = norm_atts

    if not candidates:
        return [{
            'source': 'attachment',
            'attachment_id': target_id,
            'attachment_name': '',
            'quote': '',
            'offset': None,
            'page': None,
            'is_image': False,
            'ocr_chars': 0,
            'note': f"未找到指定附件 ID：{target_id}",
        }]

    kw = str(keyword or '').strip()
    start_pos = _safe_int_offset(after_offset)
    all_hits: list[dict[str, Any]] = []

    for att in candidates:
        att_txt = att['text']
        if not att_txt.strip():
            if att['is_image']:
                all_hits.append({
                    'source': 'attachment',
                    'attachment_id': att['attachment_id'],
                    'attachment_name': att['filename'],
                    'quote': '',
                    'offset': None,
                    'page': None,
                    'is_image': True,
                    'ocr_chars': 0,
                    'note': f"图片附件「{att['filename']}」未识别出可检索文字（ocr_chars=0），请结合视觉观察或提示用户该图片无可提取文本",
                })
            continue

        if not kw:
            snippet = att_txt[start_pos:min(len(att_txt), start_pos + window * 2)].strip()
            if snippet:
                real_off = att_txt.find(snippet, start_pos)
                if real_off < 0:
                    real_off = start_pos
                all_hits.append({
                    'source': 'attachment',
                    'attachment_id': att['attachment_id'],
                    'attachment_name': att['filename'],
                    'keyword': '',
                    'quote': snippet,
                    'offset': real_off,
                    'page': _resolve_page_adaptive(real_off, att['page_ctx']),
                    'is_image': att['is_image'],
                    'ocr_chars': att['ocr_chars'],
                })
            continue

        win_hits = _search_text_windows(
            text=att_txt,
            keyword=kw,
            source_label='attachment',
            window=window,
            page_spans=att['page_ctx'],
            client=client,
            after_offset=start_pos,
        )
        for h in win_hits:
            all_hits.append({
                **h,
                'source': 'attachment',
                'attachment_id': att['attachment_id'],
                'attachment_name': att['filename'],
                'is_image': att['is_image'],
                'ocr_chars': att['ocr_chars'],
            })

        if not win_hits and att['is_image'] and len(att_txt.strip()) <= 400 and start_pos == 0:
            snippet = att_txt.strip()
            real_off = att_txt.find(snippet)
            all_hits.append({
                'source': 'attachment',
                'attachment_id': att['attachment_id'],
                'attachment_name': att['filename'],
                'keyword': kw,
                'quote': snippet,
                'offset': max(0, real_off),
                'page': 1,
                'is_image': True,
                'ocr_chars': att['ocr_chars'],
            })

    return all_hits[:_MAX_HITS_PER_SEARCH]


def _verify_and_locate_quotes(
    raw_quotes: list[Any],
    tender_text: str,
    bid_text: str,
    tender_pages: Optional[list[tuple[int, int, int]] | dict[str, Any]],
    bid_pages: Optional[list[tuple[int, int, int]] | dict[str, Any]],
    attachments: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """对 LLM final 输出的每条 quote 强制执行 verify_quote 溯源（E24 扩展支持 source='attachment' 与纯视觉结论降级标注）。"""
    norm_atts = _normalize_runtime_attachments(attachments)
    att_by_id = {a['attachment_id']: a for a in norm_atts}
    att_by_name = {a['filename']: a for a in norm_atts}

    verified_list: list[dict[str, Any]] = []
    for item in raw_quotes or []:
        if isinstance(item, dict):
            q_str = str(item.get('quote') or item.get('text') or '').strip()
            hint_src = str(item.get('source') or '').strip().lower() or None
            hint_att_id = str(item.get('attachment_id') or '').strip() or None
            hint_att_name = str(item.get('attachment_name') or '').strip() or None
        else:
            q_str = str(item or '').strip()
            hint_src = None
            hint_att_id = None
            hint_att_name = None

        if not q_str and hint_src != 'attachment':
            continue

        # ── 1. 显式指定了 attachment 来源或带 attachment_id/attachment_name ──
        if hint_src == 'attachment' or hint_att_id or hint_att_name:
            matched_att = None
            if hint_att_id and hint_att_id in att_by_id:
                matched_att = att_by_id[hint_att_id]
            elif hint_att_name and hint_att_name in att_by_name:
                matched_att = att_by_name[hint_att_name]
            elif hint_att_id and hint_att_id in att_by_name:
                matched_att = att_by_name[hint_att_id]
            elif not hint_att_id and not hint_att_name and len(norm_atts) == 1:
                matched_att = norm_atts[0]

            if matched_att is None:
                # 伪造或不存在的 attachment_id / attachment_name -> 严正拦截并清空
                verified_list.append({
                    'source': 'attachment',
                    'attachment_id': hint_att_id,
                    'attachment_name': hint_att_name,
                    'quote': '',
                    'quote_raw_rejected': q_str,
                    'offset': None,
                    'page': None,
                    'quote_verified': False,
                    'note': '附件不存在或未在本轮会话中挂载，引用已清空',
                })
                continue

            att_txt = matched_att['text']
            if q_str and att_txt and verify_quote(q_str, att_txt):
                off = att_txt.find(q_str)
                if off < 0:
                    head = re.sub(r'\s+', '', q_str)[:8]
                    off = att_txt.find(head) if head else 0
                    if off < 0:
                        off = 0
                verified_list.append({
                    'source': 'attachment',
                    'attachment_id': matched_att['attachment_id'],
                    'attachment_name': matched_att['filename'],
                    'quote': q_str,
                    'offset': off,
                    'page': _resolve_page_adaptive(off, matched_att['page_ctx']),
                    'quote_verified': True,
                })
            elif matched_att['is_image']:
                # 对纯看图结论或图片未精确命中 OCR 子串的情况：保留视觉说明但强制 quote_verified=False、offset=None
                verified_list.append({
                    'source': 'attachment',
                    'attachment_id': matched_att['attachment_id'],
                    'attachment_name': matched_att['filename'],
                    'quote': q_str if not matched_att['has_text'] else '',
                    'offset': None,
                    'page': None,
                    'quote_verified': False,
                    'note': '视觉识别结论（非文字全文精确子串）',
                })
            else:
                verified_list.append({
                    'source': 'attachment',
                    'attachment_id': matched_att['attachment_id'],
                    'attachment_name': matched_att['filename'],
                    'quote': '',
                    'quote_raw_rejected': q_str,
                    'offset': None,
                    'page': None,
                    'quote_verified': False,
                    'note': '引用未通过附件原文溯源校验，已清空',
                })
            continue

        if not q_str:
            continue

        # ── 2. 招标书 / 投标书 / 未注明来源自动反查 ──
        in_tender = verify_quote(q_str, tender_text) if tender_text else False
        in_bid = verify_quote(q_str, bid_text) if bid_text else False

        matched_att_auto = None
        if not in_tender and not in_bid and norm_atts:
            for att in norm_atts:
                if att['text'] and verify_quote(q_str, att['text']):
                    matched_att_auto = att
                    break

        if matched_att_auto is not None:
            att_txt = matched_att_auto['text']
            off = att_txt.find(q_str)
            if off < 0:
                head = re.sub(r'\s+', '', q_str)[:8]
                off = att_txt.find(head) if head else 0
                if off < 0:
                    off = 0
            verified_list.append({
                'source': 'attachment',
                'attachment_id': matched_att_auto['attachment_id'],
                'attachment_name': matched_att_auto['filename'],
                'quote': q_str,
                'offset': off,
                'page': _resolve_page_adaptive(off, matched_att_auto['page_ctx']),
                'quote_verified': True,
            })
            continue

        if hint_src == 'bid' and in_bid:
            src = 'bid'
        elif hint_src == 'tender' and in_tender:
            src = 'tender'
        elif in_tender:
            src = 'tender'
        elif in_bid:
            src = 'bid'
        else:
            verified_list.append({
                'source': hint_src or 'unknown',
                'quote': '',
                'quote_raw_rejected': q_str,
                'offset': None,
                'page': None,
                'quote_verified': False,
                'note': '引用未通过原文溯源校验，已清空',
            })
            continue

        doc_txt = tender_text if src == 'tender' else bid_text
        p_ctx_or_spans = tender_pages if src == 'tender' else bid_pages
        off = doc_txt.find(q_str)
        if off < 0:
            head = re.sub(r'\s+', '', q_str)[:8]
            off = doc_txt.find(head) if head else 0
            if off < 0:
                off = 0

        verified_list.append({
            'source': src,
            'quote': q_str,
            'offset': off,
            'page': _resolve_page_adaptive(off, p_ctx_or_spans),
            'quote_verified': True,
        })

    return verified_list


def _build_current_user_message(
    question: str,
    norm_atts: list[dict[str, Any]],
    client: Any,
) -> dict[str, Any]:
    """E24 §1.2 & §2.5.2：构建本轮末尾 user 消息。
    - 若携带附件，先在文本中列出已挂载附件清单（ID、文件名、类型、字数）；
    - 仅当存在图片附件且 getattr(client, 'supports_vision', False) 为 True 时，构造 OpenAI 多模态 content 数组传入缩图后的 data_url。
    """
    att_desc_lines: list[str] = []
    image_blocks: list[dict[str, Any]] = []
    vision_enabled = bool(client is not None and getattr(client, 'supports_vision', False))

    for att in norm_atts:
        kind_str = f"图片(OCR识别 {att['ocr_chars']} 字)" if att['is_image'] else f"文档({len(att['text'])} 字)"
        att_desc_lines.append(
            f"- 附件ID={att['attachment_id']} | 文件名={att['filename']} | 类型={kind_str}"
        )
        if att['is_image'] and vision_enabled and att.get('file_path'):
            try:
                encoded = encode_image_for_vlm(att['file_path'])
                image_blocks.append({
                    'type': 'image_url',
                    'image_url': {'url': encoded['data_url']},
                })
            except Exception:
                pass

    text_prompt = f"用户问题：{question}"
    if att_desc_lines:
        text_prompt += (
            "\n\n【本轮已挂载的补充附件（可调用 search_attachment 检索）】\n"
            + "\n".join(att_desc_lines)
        )

    if image_blocks:
        return {
            'role': 'user',
            'content': [
                {'type': 'text', 'text': text_prompt},
                *image_blocks,
            ],
        }
    return {'role': 'user', 'content': text_prompt}


def _call_react_chat(client: Any, messages: list[dict[str, Any]], norm_effort: Optional[str]) -> Any:
    """兼容调用 client.chat（透传 reasoning_effort 并在老接口不支持时降级重试）。"""
    if norm_effort is not None:
        try:
            return client.chat(messages, model_tier='flash', reasoning_effort=norm_effort)
        except TypeError:
            return client.chat(messages, model_tier='flash')
    return client.chat(messages, model_tier='flash')


def _finalize_answer_and_quotes(
    ans_text: str,
    raw_quotes: Any,
    *,
    tender_text: str,
    bid_text: str,
    tender_ctx: Any,
    bid_ctx: Any,
    norm_atts: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """对 final 答案附带的引用执行强制 verify_quote 溯源并在缺出处标注时补全定位。"""
    if not isinstance(raw_quotes, list):
        raw_quotes = [raw_quotes] if raw_quotes else []

    verified_quotes = _verify_and_locate_quotes(
        raw_quotes=raw_quotes,
        tender_text=tender_text,
        bid_text=bid_text,
        tender_pages=tender_ctx,
        bid_pages=bid_ctx,
        attachments=norm_atts,
    )
    valid_locs = []
    for vq in verified_quotes:
        if vq.get('quote_verified') and vq.get('quote'):
            if vq['source'] == 'attachment':
                src_zh = f"附件《{vq.get('attachment_name') or vq.get('attachment_id') or ''}》"
            elif vq['source'] == 'tender':
                src_zh = '招标书'
            else:
                src_zh = '投标书'
            if vq.get('page') is not None:
                valid_locs.append(f"{src_zh}第 {vq['page']} 页(@字符{vq['offset']})")
            else:
                valid_locs.append(f"{src_zh}@字符{vq['offset']}")
    out_ans = ans_text
    if valid_locs and '字符' not in out_ans and '页' not in out_ans:
        out_ans = f"{out_ans}（出处定位：{'；'.join(valid_locs)}）"
    return out_ans, verified_quotes


_FORCED_WRAPUP_PROMPT = (
    "【系统强制收口指令】已达到最大工具检索轮数上限，严禁再调用 search_tender / search_bid / "
    "locate_chapter 等任何工具！请立即严格基于上方已经检索到的工具 observation 原文窗口给出最终结论：\n"
    "1. 如已在招标文件/投标文件中检索到针对本项目的明确条款，直接引用并标明页码/字符偏移；\n"
    "2. 严禁张冠李戴：务必区分“我方针对本项目的投标响应”与“过往业绩/他人历史合同扫描件（如外部项目合同等）”。"
    "若投标文件中仅命中其他历史项目文本、未检出针对本项目的对应响应，必须如实向用户说明未检出本项目响应、"
    "点明命中片段的实际归属，并明确提示存在漏应答/负偏离风险，严禁把历史合同当成我方响应或编造内容；\n"
    "3. 只输出一个合法 JSON 对象：{\"thought\": \"汇总思考\", \"action\": \"final\", "
    "\"action_input\": {\"answer\": \"完整解答\", \"quotes\": [{\"source\": \"tender\"|\"bid\"|\"attachment\", \"quote\": \"逐字抄录自工具窗口的原文\"}]}}。"
)


def _extract_round_actions(
    parsed: dict[str, Any],
) -> tuple[str, bool, Any, list[tuple[str, Any]]]:
    """E33：归一化单轮单动作（action）或批量并行动作（actions，单轮上限 MAX_PARALLEL_ACTIONS_PER_ROUND）。"""
    thought = str(parsed.get('thought') or '').strip()
    raw_actions = parsed.get('actions')
    if isinstance(raw_actions, list) and len(raw_actions) > 0:
        tool_entries: list[tuple[str, Any]] = []
        final_entries: list[Any] = []
        for item in raw_actions:
            if not isinstance(item, dict):
                continue
            sub_act = str(item.get('action') or item.get('tool') or '').strip()
            if not sub_act:
                continue
            sub_in = _coerce_action_input(item.get('action_input'))
            is_sub_final = sub_act == 'final' or (
                sub_act not in ALLOWED_REACT_TOOLS
                and isinstance(sub_in, dict)
                and 'answer' in sub_in
            )
            if is_sub_final:
                final_entries.append(sub_in if isinstance(sub_in, dict) else item)
            else:
                tool_entries.append((sub_act, sub_in))
        if tool_entries:
            return thought, False, None, tool_entries[:MAX_PARALLEL_ACTIONS_PER_ROUND]
        if final_entries:
            return thought, True, final_entries[0], []

    action = str(parsed.get('action') or 'final').strip()
    action_input = _coerce_action_input(parsed.get('action_input'))
    if action == 'final' or ('answer' in parsed and action not in ALLOWED_REACT_TOOLS):
        final_payload = action_input if isinstance(action_input, dict) else parsed
        return thought, True, final_payload, []
    return thought, False, None, [(action, action_input)]


def _dispatch_single_tool(
    action: str,
    action_input: Any,
    *,
    tender_text: str,
    bid_text: str,
    tender_ctx: Any,
    bid_ctx: Any,
    client: Any,
    norm_atts: list[dict[str, Any]],
) -> tuple[Any, list[dict[str, Any]]]:
    """执行单条 ReAct 工具调用（复用 E31 异常转 observation.error 自愈，严格透传 BaseException）。"""
    local_quotes: list[dict[str, Any]] = []
    try:
        if action == 'search_tender':
            if isinstance(action_input, dict):
                kw = str(action_input.get('keyword') or '')
                after_off = _safe_int_offset(action_input.get('after_offset'))
            else:
                kw = str(action_input or '')
                after_off = 0
            observation = _search_text_windows(
                tender_text, kw, 'tender', page_spans=tender_ctx, client=client, after_offset=after_off
            )
            for h in observation[:2]:
                local_quotes.append({
                    'source': 'tender',
                    'quote': h['quote'],
                    'offset': h['offset'],
                    'page': h['page'],
                    'quote_verified': True,
                })
        elif action == 'search_bid':
            if isinstance(action_input, dict):
                kw = str(action_input.get('keyword') or '')
                after_off = _safe_int_offset(action_input.get('after_offset'))
            else:
                kw = str(action_input or '')
                after_off = 0
            observation = _search_text_windows(
                bid_text, kw, 'bid', page_spans=bid_ctx, client=client, after_offset=after_off
            )
            for h in observation[:2]:
                local_quotes.append({
                    'source': 'bid',
                    'quote': h['quote'],
                    'offset': h['offset'],
                    'page': h['page'],
                    'quote_verified': True,
                })
        elif action == 'search_attachment':
            if isinstance(action_input, dict):
                kw = str(action_input.get('keyword') or '')
                att_id_in = str(action_input.get('attachment_id') or '')
                after_off = _safe_int_offset(action_input.get('after_offset'))
            else:
                kw = str(action_input or '')
                att_id_in = ''
                after_off = 0
            observation = search_attachment(
                keyword=kw,
                attachment_id=att_id_in,
                attachments=norm_atts,
                after_offset=after_off,
                client=client,
            )
            for h in observation[:2]:
                if h.get('quote'):
                    local_quotes.append({
                        'source': 'attachment',
                        'attachment_id': h.get('attachment_id'),
                        'attachment_name': h.get('attachment_name'),
                        'quote': h['quote'],
                        'offset': h['offset'],
                        'page': h['page'],
                        'quote_verified': True,
                    })
        elif action == 'locate_chapter':
            if isinstance(action_input, dict):
                target_doc = bid_text if action_input.get('doc') == 'bid' else tender_text
                ch_title = str(action_input.get('chapter_title') or action_input.get('keyword') or '')
                after_off = _safe_int_offset(action_input.get('after_offset'))
            else:
                target_doc = tender_text
                ch_title = str(action_input or '')
                after_off = 0
            observation = locate_chapter(target_doc, ch_title, after_offset=after_off)
        elif action == 'recalc_price':
            if isinstance(action_input, dict):
                observation = recalc_price(
                    items=action_input.get('items', []),
                    table_total=action_input.get('table_total'),
                    bid_total=action_input.get('bid_total'),
                    bid_total_upper=action_input.get('bid_total_upper'),
                    price_cap=action_input.get('price_cap'),
                )
            elif isinstance(action_input, list):
                observation = recalc_price(items=action_input)
            else:
                observation = {
                    'error': 'recalc_price 需要包含 items 列表的对象，每项格式为 {"name", "qty", "unit_price", "total_price"}'
                }
        elif action == 'check_cert_expiry':
            if isinstance(action_input, dict):
                observation = check_cert_expiry(
                    cert_text=str(action_input.get('cert_text') or bid_text),
                    open_date=str(action_input.get('open_date') or ''),
                    required_social_months=action_input.get('required_social_months'),
                    forbidden_names=action_input.get('forbidden_names'),
                )
            else:
                observation = {'error': 'check_cert_expiry 需要包含 cert_text 与 open_date 的字典'}
        elif action == 'cite_regulation':
            if isinstance(action_input, dict):
                observation = cite_regulation(
                    keyword=str(action_input.get('keyword') or ''),
                    article_no=str(action_input.get('article_no') or ''),
                    law_name=str(action_input.get('law_name') or action_input.get('law') or ''),
                )
            else:
                observation = cite_regulation(keyword=str(action_input or ''))
        else:
            observation = {'error': f"未知工具: {action}"}
    except Exception as tool_exc:
        # E31：单工具异常绝不炸毁 ReAct SSE 流，转化为结构化 observation 交回模型自修正
        observation = {
            'passed': False,
            'error': (
                f"工具 {action} 执行异常（{type(tool_exc).__name__}: {tool_exc}），"
                f"请检查并修正输入参数格式后重试。"
            ),
        }
    return observation, local_quotes


def _update_stagnation_state(
    tool_results: list[tuple[str, Any, Any, list[dict[str, Any]]]],
    seen_hit_buckets: set[tuple[str, int | str]],
    stagnation_streak: int,
) -> int:
    """E33：基于确定性偏移桶（含 locate_chapter 的 start）统计窗口检索停滞轮数，命中新原文区间即归零。"""
    if not any(act in _WINDOW_SEARCH_TOOLS for act, _, _, _ in tool_results):
        return stagnation_streak

    found_new_bucket = False
    for act, act_in, obs, _ in tool_results:
        if act not in _WINDOW_SEARCH_TOOLS or not isinstance(obs, list):
            continue
        for hit in obs:
            if not isinstance(hit, dict):
                continue
            raw_off = hit.get('offset') if hit.get('offset') is not None else hit.get('start')
            if act == 'search_attachment' and hit.get('attachment_id'):
                src_key = f"att:{hit['attachment_id']}"
            elif act == 'locate_chapter' and isinstance(act_in, dict) and act_in.get('doc') in ('tender', 'bid'):
                src_key = f"chapter:{act_in['doc']}"
            else:
                src_key = str(hit.get('source') or act)
            if raw_off is not None:
                try:
                    bucket: tuple[str, int | str] = (src_key, int(raw_off) // 120)
                except (TypeError, ValueError):
                    bucket = (src_key, str(hit.get('quote') or hit.get('excerpt') or '')[:60])
            else:
                bucket = (src_key, str(hit.get('quote') or hit.get('excerpt') or '')[:60])
            if bucket not in seen_hit_buckets:
                seen_hit_buckets.add(bucket)
                found_new_bucket = True

    return 0 if found_new_bucket else (stagnation_streak + 1)


def _build_round_feedback_message(
    r_idx: int,
    limit: int,
    tool_results: list[tuple[str, Any, Any, list[dict[str, Any]]]],
    stagnation_streak: int,
) -> str:
    """组装本轮工具观察结果 + 零成本剩余回合预算与空转去重提示。"""
    if len(tool_results) == 1:
        act, _, obs, _ = tool_results[0]
        obs_body = f"工具 {act} 返回结果：{json.dumps(obs, ensure_ascii=False)}"
    else:
        lines = [
            f"[{idx}] {act} 返回结果：{json.dumps(obs, ensure_ascii=False)}"
            for idx, (act, _, obs, _) in enumerate(tool_results, start=1)
        ]
        obs_body = f"工具批量返回结果（本轮并行 {len(tool_results)} 项）：\n" + "\n".join(lines)

    remaining = max(0, limit - r_idx)
    parts = [
        obs_body,
        f"请继续下一步或输出 action='final'。\n【思考回合进度：第 {r_idx}/{limit} 轮（剩余 {remaining} 轮可用）；如关键原文证据已齐备，请立即 action='final' 收口，无需凑满轮次。】",
    ]
    if stagnation_streak >= 2:
        parts.append(
            f"【检索去重提示：已连续 {stagnation_streak} 轮空检或命中同一原文区间。若核心同义词仍仅命中无关历史合同/无新段落，请立即 action='final' 如实说明未检出及命中片段的实际归属，避免继续消耗轮次。】"
        )
    return "\n".join(parts)


def _has_image_attachments(norm_atts: list[dict[str, Any]]) -> bool:
    return any(bool(att.get('is_image')) for att in (norm_atts or []))


def _should_bounce_first_round_ungrounded(
    question: str,
    *,
    r_idx: int,
    limit: int,
    tool_steps_count: int,
    bounce_count: int,
    norm_atts: list[dict[str, Any]],
    formatted_history: list[dict[str, Any]],
    verified_quotes: list[dict[str, Any]],
) -> bool:
    """E34：Python 确定性防偷懒回弹护栏（单问回弹上限写死 MAX_UNGROUNDED_BOUNCE_PER_TURN=1）。"""
    if bounce_count >= MAX_UNGROUNDED_BOUNCE_PER_TURN:
        return False
    if r_idx != 1 or r_idx >= limit:
        return False
    if tool_steps_count > 0:
        return False
    if _has_image_attachments(norm_atts):
        return False
    if any(bool(q.get('quote_verified')) for q in (verified_quotes or [])):
        return False
    q_text = (question or '').strip()
    if not _PROJECT_VERIFY_SIGNAL_RE.search(q_text):
        return False
    if bool(formatted_history) and bool(_REORG_META_RE.search(q_text)):
        return False
    return True


def ask_bid_expert_stream(
    tender_path: str | Path,
    bid_path: str | Path,
    question: str,
    client=None,
    max_rounds: Optional[int] = None,
    reasoning_effort: Optional[str] = None,
    history: Optional[list[dict[str, Any]]] = None,
    attachments: Optional[list[dict[str, Any]]] = None,
) -> Iterator[dict[str, Any]]:
    """E21 / E24：F7 标书专家流式生成器主入口（依次 yield step -> answer_delta -> done 事件帧）。"""
    tender_doc = parse_document(str(tender_path))
    bid_doc = parse_document(str(bid_path))
    tender_text = tender_doc.text or ''
    bid_text = bid_doc.text or ''

    tender_ctx = build_unified_page_context(tender_path)
    bid_ctx = build_unified_page_context(bid_path)
    formatted_history = _format_history_messages(history)
    norm_atts = _normalize_runtime_attachments(attachments)

    # ── 0-Token 离线降级分支（同步支持检索补充文档与图片 OCR 文本池）──
    if client is None:
        has_pronoun = any(pk in (question or '') for pk in _FOLLOWUP_PRONOUN_KEYS)
        domain_tokens = _extract_domain_tokens(question)
        keywords = list(domain_tokens) if has_pronoun else _extract_keywords_from_question(question)

        prior_user_msgs = [
            hm['content'] for hm in reversed(formatted_history) if hm['role'] == 'user'
        ]
        last_user_question = prior_user_msgs[0] if prior_user_msgs else ''
        context_user_question = ''

        if prior_user_msgs and (has_pronoun or len((question or '').strip()) <= 12 or keywords == ['质保']):
            for prev_q in prior_user_msgs:
                prev_domain = _extract_domain_tokens(prev_q)
                if prev_domain:
                    if not context_user_question:
                        context_user_question = prev_q
                    for pk in prev_domain:
                        if pk not in keywords:
                            keywords.append(pk)
            if not keywords:
                for prev_q in prior_user_msgs:
                    for pk in _extract_keywords_from_question(prev_q):
                        if pk not in keywords:
                            keywords.append(pk)
        if not keywords:
            keywords = _extract_keywords_from_question(question)

        t_hits: list[dict[str, Any]] = []
        b_hits: list[dict[str, Any]] = []
        a_hits: list[dict[str, Any]] = []
        for kw in keywords:
            t_hits.extend(search_tender(kw, tender_text=tender_text, page_spans=tender_ctx))
            b_hits.extend(search_bid(kw, bid_text=bid_text, page_spans=bid_ctx))
            if norm_atts:
                a_hits.extend(search_attachment(kw, attachments=norm_atts))

        if not t_hits and tender_text:
            t_hits.extend(_search_text_windows(tender_text, question, 'tender', page_spans=tender_ctx))
        if not b_hits and bid_text:
            b_hits.extend(_search_text_windows(bid_text, question, 'bid', page_spans=bid_ctx))
        if not a_hits and norm_atts:
            a_hits.extend(search_attachment('', attachments=norm_atts))

        valid_a_hits = [h for h in a_hits if h.get('quote')]
        empty_img_atts = [a for a in norm_atts if a['is_image'] and not a['has_text']]

        quotes = []
        for h in (valid_a_hits[:2] + t_hits[:2] + b_hits[:2]):
            quotes.append({
                'source': h['source'],
                'attachment_id': h.get('attachment_id'),
                'attachment_name': h.get('attachment_name'),
                'quote': h['quote'],
                'offset': h['offset'],
                'page': h['page'],
                'quote_verified': True,
            })

        summary_parts = []
        ctx_ref_q = context_user_question or last_user_question
        if ctx_ref_q and has_pronoun:
            summary_parts.append(f"【结合上一轮提问「{ctx_ref_q[:40]}」上下文】")
        if valid_a_hits:
            top_a = valid_a_hits[0]
            summary_parts.append(
                f"【补充附件「{top_a.get('attachment_name')}」命中】{top_a['quote']}（@字符{top_a['offset']}）"
            )
        if empty_img_atts and not valid_a_hits:
            img_names = '、'.join(a['filename'] for a in empty_img_atts)
            summary_parts.append(
                f"【图片附件提示】图片「{img_names}」未识别出可检索文字（ocr_chars=0），当前为 0-Token 离线模式，请开启支持视觉的多模态模型或上传清晰文字版材料。"
            )
        if t_hits:
            summary_parts.append(f"【招标文件命中】{t_hits[0]['quote']}（@字符{t_hits[0]['offset']}）")
        if b_hits:
            summary_parts.append(f"【投标文件命中】{b_hits[0]['quote']}（@字符{b_hits[0]['offset']}）")
        if not summary_parts:
            summary_parts.append('未在招标文件、投标文件或补充附件中检索到对应关键词内容。')
        answer = '\n'.join(summary_parts)

        fallback_step = {
            'round': 1,
            'thought': '0 Token 关键词与混合同义词检索降级模式',
            'action': 'keyword_search',
            'action_input': keywords,
            'observation': {
                'tender_hits': len(t_hits),
                'bid_hits': len(b_hits),
                'attachment_hits': len(valid_a_hits),
            },
        }
        yield {'type': 'step', 'data': fallback_step}
        for piece in _iter_answer_chunks(answer):
            yield {'type': 'answer_delta', 'data': {'text': piece}}

        yield {
            'type': 'done',
            'data': {
                'mode': 'keyword_fallback',
                'question': question,
                'answer': answer,
                'quotes': quotes,
                'steps': [fallback_step],
                'rounds_used': 1,
                'terminated_by_max_rounds': False,
                'grounded': True,
                'fast_path': False,
                'route': None,
            },
        }
        return

    # ── 在线 ReAct 多步工具调用分支（注入历史上下文 + 当轮多模态附件 + 挡位映射）──
    user_msg = _build_current_user_message(question, norm_atts, client)
    messages: list[dict[str, Any]] = [
        {'role': 'system', 'content': _REACT_SYSTEM_PROMPT},
        *formatted_history,
        user_msg,
    ]
    steps: list[dict[str, Any]] = []
    collected_tool_quotes: list[dict[str, Any]] = []
    seen_hit_buckets: set[tuple[str, int | str]] = set()
    stagnation_streak = 0
    bounce_count = 0
    has_img_att = _has_image_attachments(norm_atts)

    norm_effort, limit = _resolve_effort_and_rounds(reasoning_effort, max_rounds, client=client)

    for r_idx in range(1, limit + 1):
        resp = _call_react_chat(client, messages, norm_effort)
        raw_content = getattr(resp, 'content', '') or ''
        parsed = _parse_llm_json(raw_content)

        if not parsed:
            # E24 §2.5.4：若模型输出了含 "action"/"thought" 的残缺工具 JSON，严禁直接作为最终答案吐给用户
            if _looks_like_unparsed_tool_json(raw_content):
                if r_idx < limit:
                    messages.append({'role': 'assistant', 'content': raw_content})
                    messages.append({
                        'role': 'user',
                        'content': (
                            '你上一轮输出的 JSON 格式存在语法错误（请勿在 JSON 字符串内直接包含未转义的换行符）。'
                            '请重新输出一个合法的单行 JSON 对象。'
                        ),
                    })
                    continue
                ans_fallback = '已完成标书条款检索，但模型输出格式异常，请参考下方已提取的原文溯源卡片或重试。'
            else:
                if _should_bounce_first_round_ungrounded(
                    question,
                    r_idx=r_idx,
                    limit=limit,
                    tool_steps_count=len(steps),
                    bounce_count=bounce_count,
                    norm_atts=norm_atts,
                    formatted_history=formatted_history,
                    verified_quotes=[],
                ):
                    bounce_count += 1
                    messages.append({'role': 'assistant', 'content': raw_content})
                    messages.append({'role': 'user', 'content': _UNGROUNDED_BOUNCE_PROMPT})
                    continue
                ans_fallback = raw_content.strip() or '未能生成有效回答。'

            end_step = {
                'round': r_idx,
                'thought': '模型未返回有效结构化 JSON，收口输出可读结论',
                'action': 'final',
                'action_input': {},
                'observation': None,
            }
            has_tool_steps = len(steps) > 0
            is_grounded = has_tool_steps or has_img_att
            is_fast_path = (not has_tool_steps) and (not is_grounded)
            steps.append(end_step)
            yield {'type': 'step', 'data': end_step}
            for piece in _iter_answer_chunks(ans_fallback):
                yield {'type': 'answer_delta', 'data': {'text': piece}}
            yield {
                'type': 'done',
                'data': {
                    'mode': 'react',
                    'question': question,
                    'answer': ans_fallback,
                    'quotes': collected_tool_quotes,
                    'steps': steps,
                    'rounds_used': r_idx,
                    'terminated_by_max_rounds': False,
                    'grounded': is_grounded,
                    'fast_path': is_fast_path,
                    'route': 'direct_meta' if is_fast_path else None,
                },
            }
            return

        thought, is_final_turn, final_payload, tool_calls = _extract_round_actions(parsed)

        # E4 / E24 / E34：7 工具白名单检查与 final 收口（含第 1 轮确定性回弹护栏）
        if is_final_turn:
            final_payload = final_payload if isinstance(final_payload, dict) else parsed
            ans_text = str(final_payload.get('answer') or parsed.get('answer') or '').strip()
            raw_route = str(final_payload.get('route') or parsed.get('route') or '').strip() or None
            raw_quotes = final_payload.get('quotes') if 'quotes' in final_payload else parsed.get('quotes', [])
            if raw_route == 'direct_meta' and len(steps) == 0 and not formatted_history:
                raw_quotes = []
            ans_text, verified_quotes = _finalize_answer_and_quotes(
                ans_text,
                raw_quotes,
                tender_text=tender_text,
                bid_text=bid_text,
                tender_ctx=tender_ctx,
                bid_ctx=bid_ctx,
                norm_atts=norm_atts,
            )

            if _should_bounce_first_round_ungrounded(
                question,
                r_idx=r_idx,
                limit=limit,
                tool_steps_count=len(steps),
                bounce_count=bounce_count,
                norm_atts=norm_atts,
                formatted_history=formatted_history,
                verified_quotes=verified_quotes,
            ):
                bounce_count += 1
                messages.append({'role': 'assistant', 'content': raw_content})
                messages.append({'role': 'user', 'content': _UNGROUNDED_BOUNCE_PROMPT})
                continue

            has_tool_steps = len(steps) > 0
            is_grounded = has_tool_steps or has_img_att or any(bool(q.get('quote_verified')) for q in verified_quotes)
            is_fast_path = (not has_tool_steps) and (not is_grounded)

            final_step = {
                'round': r_idx,
                'thought': thought,
                'action': 'final',
                'action_input': final_payload,
                'observation': None,
            }
            steps.append(final_step)
            yield {'type': 'step', 'data': final_step}
            for piece in _iter_answer_chunks(ans_text):
                yield {'type': 'answer_delta', 'data': {'text': piece}}

            yield {
                'type': 'done',
                'data': {
                    'mode': 'react',
                    'question': question,
                    'answer': ans_text,
                    'quotes': verified_quotes,
                    'steps': steps,
                    'rounds_used': r_idx,
                    'terminated_by_max_rounds': False,
                    'grounded': is_grounded,
                    'fast_path': is_fast_path,
                    'route': raw_route if raw_route is not None else ('direct_meta' if is_fast_path else None),
                },
            }
            return

        # E33：单条就地执行，2~4 条并发分发（严格按原始下标保序，BaseException 立即撤池透传）
        tool_results: list[tuple[str, Any, Any, list[dict[str, Any]]]] = []
        if len(tool_calls) == 1:
            act, act_in = tool_calls[0]
            obs, q_list = _dispatch_single_tool(
                act,
                act_in,
                tender_text=tender_text,
                bid_text=bid_text,
                tender_ctx=tender_ctx,
                bid_ctx=bid_ctx,
                client=client,
                norm_atts=norm_atts,
            )
            tool_results.append((act, act_in, obs, q_list))
        else:
            pool = ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_ACTIONS_PER_ROUND, len(tool_calls)))
            aborted = False
            try:
                futures = [
                    pool.submit(
                        _dispatch_single_tool,
                        act,
                        act_in,
                        tender_text=tender_text,
                        bid_text=bid_text,
                        tender_ctx=tender_ctx,
                        bid_ctx=bid_ctx,
                        client=client,
                        norm_atts=norm_atts,
                    )
                    for act, act_in in tool_calls
                ]
                for (act, act_in), fut in zip(tool_calls, futures):
                    obs, q_list = fut.result()
                    tool_results.append((act, act_in, obs, q_list))
            except BaseException:
                aborted = True
                pool.shutdown(wait=False, cancel_futures=True)
                raise
            finally:
                if not aborted:
                    pool.shutdown(wait=True)

        batch_size = len(tool_results)
        for batch_idx, (act, act_in, obs, q_list) in enumerate(tool_results, start=1):
            if q_list:
                collected_tool_quotes.extend(q_list)
            step_item: dict[str, Any] = {
                'round': r_idx,
                'thought': (
                    thought
                    if batch_idx == 1
                    else (f"{thought}（同轮并行 {batch_idx}/{batch_size}）" if thought else f"同轮并行 {batch_idx}/{batch_size}")
                ),
                'action': act,
                'action_input': act_in,
                'observation': obs,
            }
            if batch_size > 1:
                step_item['batch_index'] = batch_idx
                step_item['batch_size'] = batch_size
            steps.append(step_item)
            yield {'type': 'step', 'data': step_item}

        stagnation_streak = _update_stagnation_state(tool_results, seen_hit_buckets, stagnation_streak)
        feedback_msg = _build_round_feedback_message(r_idx, limit, tool_results, stagnation_streak)
        messages.append({'role': 'assistant', 'content': raw_content})
        messages.append({'role': 'user', 'content': feedback_msg})

    # E32：工具检索轮数耗尽后追加 1 次强制收口 LLM 调用（禁再调工具，基于已有观察作答）
    wrap_messages = list(messages)
    wrap_messages.append({'role': 'user', 'content': _FORCED_WRAPUP_PROMPT})
    try:
        wrap_resp = _call_react_chat(client, wrap_messages, norm_effort)
        raw_wrap = getattr(wrap_resp, 'content', '') or ''
    except Exception:
        raw_wrap = ''

    wrap_parsed = _parse_llm_json(raw_wrap)
    if isinstance(wrap_parsed, dict):
        w_thought = str(wrap_parsed.get('thought') or '').strip()
        w_action = str(wrap_parsed.get('action') or '').strip()
        w_input = _coerce_action_input(wrap_parsed.get('action_input'))
        w_payload = w_input if isinstance(w_input, dict) else wrap_parsed
        if w_action == 'final' or (
            w_action not in ALLOWED_REACT_TOOLS
            and ('answer' in w_payload or 'answer' in wrap_parsed)
        ):
            w_ans = str(w_payload.get('answer') or wrap_parsed.get('answer') or '').strip()
            if w_ans:
                w_raw_quotes = (
                    w_payload.get('quotes')
                    if 'quotes' in w_payload
                    else wrap_parsed.get('quotes', [])
                )
                w_ans, w_quotes = _finalize_answer_and_quotes(
                    w_ans,
                    w_raw_quotes,
                    tender_text=tender_text,
                    bid_text=bid_text,
                    tender_ctx=tender_ctx,
                    bid_ctx=bid_ctx,
                    norm_atts=norm_atts,
                )
                wrap_step = {
                    'round': limit + 1,
                    'thought': w_thought or '已达最大检索轮数，强制收口输出最终结论',
                    'action': 'final',
                    'action_input': w_payload,
                    'observation': None,
                }
                steps.append(wrap_step)
                yield {'type': 'step', 'data': wrap_step}
                for piece in _iter_answer_chunks(w_ans):
                    yield {'type': 'answer_delta', 'data': {'text': piece}}
                yield {
                    'type': 'done',
                    'data': {
                        'mode': 'react',
                        'question': question,
                        'answer': w_ans,
                        'quotes': w_quotes,
                        'steps': steps,
                        'rounds_used': limit,
                        'terminated_by_max_rounds': True,
                        'forced_final': True,
                        'grounded': len(steps) > 1 or has_img_att or any(bool(q.get('quote_verified')) for q in w_quotes),
                        'fast_path': False,
                        'route': None,
                    },
                }
                return
    elif raw_wrap.strip() and not _looks_like_unparsed_tool_json(raw_wrap):
        plain_ans = raw_wrap.strip()
        wrap_step = {
            'round': limit + 1,
            'thought': '已达最大检索轮数，模型以纯文本收口输出结论',
            'action': 'final',
            'action_input': {},
            'observation': None,
        }
        has_prior_tools = len(steps) > 0
        steps.append(wrap_step)
        yield {'type': 'step', 'data': wrap_step}
        for piece in _iter_answer_chunks(plain_ans):
            yield {'type': 'answer_delta', 'data': {'text': piece}}
        yield {
            'type': 'done',
            'data': {
                'mode': 'react',
                'question': question,
                'answer': plain_ans,
                'quotes': collected_tool_quotes,
                'steps': steps,
                'rounds_used': limit,
                'terminated_by_max_rounds': True,
                'forced_final': True,
                'grounded': has_prior_tools or has_img_att,
                'fast_path': False,
                'route': None,
            },
        }
        return

    fallback_ans = f'已达到最大工具检索轮数上限（{limit}轮），以下为已检索到的原文证据汇总。'
    for piece in _iter_answer_chunks(fallback_ans):
        yield {'type': 'answer_delta', 'data': {'text': piece}}
    yield {
        'type': 'done',
        'data': {
            'mode': 'react',
            'question': question,
            'answer': fallback_ans,
            'quotes': collected_tool_quotes,
            'steps': steps,
            'rounds_used': limit,
            'terminated_by_max_rounds': True,
            'forced_final': False,
            'grounded': len(steps) > 0 or has_img_att,
            'fast_path': False,
            'route': None,
        },
    }


def ask_bid_expert(
    tender_path: str | Path,
    bid_path: str | Path,
    question: str,
    client=None,
    max_rounds: Optional[int] = None,
    reasoning_effort: Optional[str] = None,
    history: Optional[list[dict[str, Any]]] = None,
    attachments: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """F7 标书专家 ReAct 追问同步主入口（消费 ask_bid_expert_stream 的 done 帧，公共签名与返回结构保持向后兼容）。"""
    final_data: Optional[dict[str, Any]] = None
    for event in ask_bid_expert_stream(
        tender_path=tender_path,
        bid_path=bid_path,
        question=question,
        client=client,
        max_rounds=max_rounds,
        reasoning_effort=reasoning_effort,
        history=history,
        attachments=attachments,
    ):
        if event.get('type') == 'done':
            final_data = event.get('data')
    if final_data is not None:
        return final_data
    return {
        'mode': 'keyword_fallback',
        'question': question,
        'answer': '未能生成有效回答。',
        'quotes': [],
        'steps': [],
        'rounds_used': 1,
        'terminated_by_max_rounds': False,
    }
