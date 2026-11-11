# -*- coding: utf-8 -*-
"""F7 标书专家 ReAct 追问引擎（★大模型③）—— 跨双书工具检索与溯源问答（E2 混合检索 + E4 第6工具 cite_regulation + E18 双轨页码统一 + E21 多轮上下文与流式思考过程）。

模块定位（对应 PRD-02 §7 F7、E2 任务卡 §2.4、E4 任务卡 §1、E18 双轨页码大一统与 E21 多轮流式卡）：
1. 工具层（共 6 组确定性/混合检索工具函数，既有 5 工具签名一字不变）：
   - search_tender(keyword, ...) / search_bid(keyword, ...)：双书检索；
   - locate_chapter(text, chapter_title)：章节定位；
   - recalc_price(items, ...)：调用 PriceSandbox 复算报价；
   - check_cert_expiry(cert_text, open_date, ...)：调用 F3 scan_qualifications 核验效期；
   - cite_regulation(keyword, article_no, ...)：E4 新增第 6 工具，检索法规库返回条文原文+法律名+条号（库空时诚实报"法规库为空"）。
2. ReAct 循环与流式输出（E21）：
   - 支持注入会话多轮上下文 history（自动过滤空消息、保留最近 N 轮并截断超长单条文本）；
   - 智谱 glm-5.3-flash 三挡推理深度实测合法值映射：low->'low'(3轮) / medium->'high'(4轮) / high->'max'(6轮)，严禁向网关直接透传 'medium' 触发 400-1210 拒绝；
   - ask_bid_expert_stream 实时产出 step -> answer_delta -> done 事件帧，final 输出 quotes 100% 强制走 verify_quote 溯源。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterator, Optional

from core.hybrid_retriever import expand_query_synonyms, hybrid_retrieve_windows
from core.parser import parse_document
from core.price_sandbox import PriceSandbox
from core.qualification_scanner import scan_qualifications
from core.quote_verifier import verify_quote
from core.regulation_library import cite_regulation as _lib_cite_regulation
from core.response_checker import build_unified_page_context, resolve_unified_page
from core.tender_extractor import locate_chapters, locate_keyword_sections

_SEARCH_WINDOW = 200
_MAX_HITS_PER_SEARCH = 5
_JSON_BLOCK_RE = re.compile(r'\{.*\}', re.DOTALL)

# E4：F7 允许调用的 6 大工具名称白名单
ALLOWED_REACT_TOOLS = (
    'search_tender',
    'search_bid',
    'locate_chapter',
    'recalc_price',
    'check_cert_expiry',
    'cite_regulation',
)

_QUESTION_STOPWORDS = (
    '请问', '招标文件', '招标书', '投标文件', '投标书', '我们', '我方', '甲方',
    '要求', '响应', '在哪里', '在哪', '哪一页', '哪页', '是多少', '多少', '什么',
    '几年', '几个月', '是否', '有没有', '如何', '怎么', '的', '了', '吗', '呢',
)

# E21 §2.1：指代与承接上文特征词（供 0-Token 离线降级结合上一轮用户问题补充关键词）
_FOLLOWUP_PRONOUN_KEYS = (
    '刚才', '刚刚', '上一轮', '上轮', '上面', '前面', '之前', '这两个', '那两个',
    '它们', '这些', '那些', '这个', '那个', '怎么规定', '为什么', '怎么不', '还有呢', '问了什么', '它',
)

# E21 §1.3 & §2.1：前端 UI 三挡（low/medium/high）映射为智谱 glm-5.3-flash 实测合法上游值（low/high/max）与轮次上限
_EFFORT_UPSTREAM_MAP: dict[str, tuple[str, int]] = {
    'low': ('low', 3),
    'medium': ('high', 4),
    'high': ('max', 6),
    'max': ('max', 6),
}

_REACT_SYSTEM_PROMPT = """你是招投标双书合规审查专家（ReAct Agent）。你可以调用以下 6 个确定性工具来回答用户关于《招标文件》、《投标文件》与招投标法律法规的问题：

可用工具列表：
1. "search_tender"：在招标文件中检索关键词，action_input 为关键词字符串（如 "质保期"）；
2. "search_bid"：在投标文件中检索关键词，action_input 为关键词字符串（如 "质保期"）；
3. "locate_chapter"：定位章节标题，action_input 为章节关键词字符串（如 "评标办法"）；
4. "recalc_price"：调用报价算术沙盒复算分项报价，action_input 为 {"items": [...], "bid_total": ...}；
5. "check_cert_expiry"：调用证照效期核验器，action_input 为 {"cert_text": "...", "open_date": "YYYY-MM-DD"}；
6. "cite_regulation"：在招投标法规库中检索法条原文，action_input 为 {"keyword": "...", "article_no": "第X条"}；
7. "final"：当你已通过工具拿到原文证据准备给出最终回答时使用，action_input 为：
   {"answer": "最终解答正文", "quotes": ["逐字摘自原文的引用句1", "逐字摘自原文的引用句2"]}

每轮你必须只输出一个 JSON 对象，格式如下：
{
  "thought": "思考过程",
  "action": "search_tender" | "search_bid" | "locate_chapter" | "recalc_price" | "check_cert_expiry" | "cite_regulation" | "final",
  "action_input": ...
}
⚠️ 铁律：final 中的 quotes 必须 100% 逐字抄录自工具返回的原文窗口，严禁改写或编造！只输出 JSON。"""


def _parse_llm_json(content: str) -> Optional[dict]:
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
    """E21 §2.1：从会话历史提取 role in ('user','assistant') 的有效消息，
    过滤空内容与 loading 占位，按时间正序保留最近 max_turns*2 条，单条超出 max_chars_per_msg 截断加 '…'。
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
        content = str(item.get('content') or '').strip()
        if not content:
            continue
        if len(content) > max_chars_per_msg:
            content = content[:max_chars_per_msg] + '…'
        cleaned.append({'role': role, 'content': content})

    max_msgs = max(2, int(max_turns) * 2)
    return cleaned[-max_msgs:]


def _resolve_effort_and_rounds(
    reasoning_effort: Optional[str],
    max_rounds: int,
) -> tuple[Optional[str], int]:
    """E21 §2.1：将前端三挡（low/medium/high）映射为智谱实测合法上游值（low/high/max）并联动轮次上限。"""
    base_limit = max(1, int(max_rounds or 6))
    raw_key = str(reasoning_effort or '').strip().lower()
    if raw_key in _EFFORT_UPSTREAM_MAP:
        mapped_effort, gear_round_cap = _EFFORT_UPSTREAM_MAP[raw_key]
        return mapped_effort, min(base_limit, gear_round_cap)
    return None, base_limit


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


def _search_text_windows(
    text: str,
    keyword: str,
    source_label: str,
    window: int = _SEARCH_WINDOW,
    page_spans: Optional[list[tuple[int, int, int]] | dict[str, Any]] = None,
    client: Any = None,
) -> list[dict[str, Any]]:
    """在文本中检索关键词，返回 ±200 字符窗口、字符偏移与页码（自适应兼容 unified_ctx 字典与旧版 page_spans 列表）。"""
    if not text or not keyword:
        return []

    kw = str(keyword).strip()
    syn_terms = expand_query_synonyms(kw)
    if kw in text:
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
        start = 0
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
                    'page': _resolve_page_adaptive(idx, ctx_or_spans),
                })
            start = idx + len(term)
        if hits:
            break

    if not hits:
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
                'page': _resolve_page_adaptive(off, ctx_or_spans),
            })

    return hits


# ── 工具层：6 组确定性工具函数（支持独立调用与 ReAct 挂载）────────────────

def search_tender(
    keyword: str,
    tender_text: str = '',
    window: int = _SEARCH_WINDOW,
    page_spans: Optional[list[tuple[int, int, int]] | dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """工具 1a：在招标文件中检索关键词，返回 ±200 字符窗口 + 偏移 + 页码。"""
    return _search_text_windows(tender_text, keyword, 'tender', window=window, page_spans=page_spans)


def search_bid(
    keyword: str,
    bid_text: str = '',
    window: int = _SEARCH_WINDOW,
    page_spans: Optional[list[tuple[int, int, int]] | dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """工具 1b：在投标文件中检索关键词，返回 ±200 字符窗口 + 偏移 + 页码。"""
    return _search_text_windows(bid_text, keyword, 'bid', window=window, page_spans=page_spans)


def locate_chapter(text: str, chapter_title: str) -> list[dict[str, Any]]:
    """工具 2：章节定位（复用 tender_extractor.locate_chapters 与 locate_keyword_sections）。"""
    if not text or not chapter_title:
        return []
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
        if kw in ch['title'] or kw in ch['index']
    ]
    if matched:
        return matched
    return locate_keyword_sections(text, (kw,), context_after=400)


def recalc_price(
    items: list[dict[str, Any]],
    table_total=None,
    bid_total=None,
    bid_total_upper: Optional[str] = None,
    price_cap=None,
) -> dict[str, Any]:
    """工具 3：调用 F2 PriceSandbox 复算报价。"""
    sandbox = PriceSandbox(
        items=items,
        table_total=table_total,
        bid_total=bid_total,
        bid_total_upper=bid_total_upper,
        price_cap=price_cap,
    )
    findings = sandbox.run()
    return {'passed': len(findings) == 0, 'findings': findings}


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


def _verify_and_locate_quotes(
    raw_quotes: list[Any],
    tender_text: str,
    bid_text: str,
    tender_pages: Optional[list[tuple[int, int, int]] | dict[str, Any]],
    bid_pages: Optional[list[tuple[int, int, int]] | dict[str, Any]],
) -> list[dict[str, Any]]:
    """对 LLM final 输出的每条 quote 强制执行 verify_quote 溯源。"""
    verified_list: list[dict[str, Any]] = []
    for item in raw_quotes or []:
        if isinstance(item, dict):
            q_str = str(item.get('quote') or item.get('text') or '').strip()
            hint_src = item.get('source')
        else:
            q_str = str(item or '').strip()
            hint_src = None

        if not q_str:
            continue

        in_tender = verify_quote(q_str, tender_text) if tender_text else False
        in_bid = verify_quote(q_str, bid_text) if bid_text else False

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


def ask_bid_expert_stream(
    tender_path: str | Path,
    bid_path: str | Path,
    question: str,
    client=None,
    max_rounds: int = 6,
    reasoning_effort: Optional[str] = None,
    history: Optional[list[dict[str, Any]]] = None,
) -> Iterator[dict[str, Any]]:
    """E21 §2.1：F7 标书专家流式生成器主入口（依次 yield step -> answer_delta -> done 事件帧）。"""
    tender_doc = parse_document(str(tender_path))
    bid_doc = parse_document(str(bid_path))
    tender_text = tender_doc.text or ''
    bid_text = bid_doc.text or ''

    tender_ctx = build_unified_page_context(tender_path)
    bid_ctx = build_unified_page_context(bid_path)
    formatted_history = _format_history_messages(history)

    # ── 0-Token 离线降级分支（支持结合历史多轮提问补充上下文关键词）──
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
        for kw in keywords:
            t_hits.extend(search_tender(kw, tender_text=tender_text, page_spans=tender_ctx))
            b_hits.extend(search_bid(kw, bid_text=bid_text, page_spans=bid_ctx))

        if not t_hits and tender_text:
            t_hits.extend(_search_text_windows(tender_text, question, 'tender', page_spans=tender_ctx))
        if not b_hits and bid_text:
            b_hits.extend(_search_text_windows(bid_text, question, 'bid', page_spans=bid_ctx))

        quotes = []
        for h in (t_hits[:2] + b_hits[:2]):
            quotes.append({
                'source': h['source'],
                'quote': h['quote'],
                'offset': h['offset'],
                'page': h['page'],
                'quote_verified': True,
            })

        summary_parts = []
        ctx_ref_q = context_user_question or last_user_question
        if ctx_ref_q and has_pronoun:
            summary_parts.append(f"【结合上一轮提问「{ctx_ref_q[:40]}」上下文】")
        if t_hits:
            summary_parts.append(f"【招标文件命中】{t_hits[0]['quote']}（@字符{t_hits[0]['offset']}）")
        if b_hits:
            summary_parts.append(f"【投标文件命中】{b_hits[0]['quote']}（@字符{b_hits[0]['offset']}）")
        if not t_hits and not b_hits:
            summary_parts.append('未在招标文件或投标文件中检索到对应关键词内容。')
        answer = '\n'.join(summary_parts)

        fallback_step = {
            'round': 1,
            'thought': '0 Token 关键词与混合同义词检索降级模式',
            'action': 'keyword_search',
            'action_input': keywords,
            'observation': {'tender_hits': len(t_hits), 'bid_hits': len(b_hits)},
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
            },
        }
        return

    # ── 在线 ReAct 多步工具调用分支（注入历史上下文 + 挡位映射）──
    messages: list[dict[str, str]] = [
        {'role': 'system', 'content': _REACT_SYSTEM_PROMPT},
        *formatted_history,
        {'role': 'user', 'content': f"用户问题：{question}"},
    ]
    steps: list[dict[str, Any]] = []
    collected_tool_quotes: list[dict[str, Any]] = []

    norm_effort, limit = _resolve_effort_and_rounds(reasoning_effort, max_rounds)

    for r_idx in range(1, limit + 1):
        if norm_effort is not None:
            try:
                resp = client.chat(messages, model_tier='flash', reasoning_effort=norm_effort)
            except TypeError:
                resp = client.chat(messages, model_tier='flash')
        else:
            resp = client.chat(messages, model_tier='flash')
        raw_content = getattr(resp, 'content', '') or ''
        parsed = _parse_llm_json(raw_content)

        if not parsed:
            end_step = {
                'round': r_idx,
                'thought': '模型未返回有效 JSON，直接结束',
                'action': 'final',
                'action_input': {},
                'observation': None,
            }
            steps.append(end_step)
            yield {'type': 'step', 'data': end_step}
            ans_fallback = raw_content.strip() or '未能生成有效回答。'
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
                },
            }
            return

        thought = str(parsed.get('thought') or '').strip()
        action = str(parsed.get('action') or 'final').strip()
        action_input = parsed.get('action_input')

        # E4 第 474 行区：6 工具白名单检查
        if action == 'final' or ('answer' in parsed and action not in ALLOWED_REACT_TOOLS):
            final_payload = action_input if isinstance(action_input, dict) else parsed
            ans_text = str(final_payload.get('answer') or parsed.get('answer') or '').strip()
            raw_quotes = final_payload.get('quotes') if 'quotes' in final_payload else parsed.get('quotes', [])
            if not isinstance(raw_quotes, list):
                raw_quotes = [raw_quotes] if raw_quotes else []

            verified_quotes = _verify_and_locate_quotes(
                raw_quotes=raw_quotes,
                tender_text=tender_text,
                bid_text=bid_text,
                tender_pages=tender_ctx,
                bid_pages=bid_ctx,
            )

            valid_locs = []
            for vq in verified_quotes:
                if vq.get('quote_verified') and vq.get('quote'):
                    src_zh = '招标书' if vq['source'] == 'tender' else '投标书'
                    if vq.get('page') is not None:
                        valid_locs.append(f"{src_zh}第 {vq['page']} 页(@字符{vq['offset']})")
                    else:
                        valid_locs.append(f"{src_zh}@字符{vq['offset']}")
            if valid_locs and '字符' not in ans_text and '页' not in ans_text:
                ans_text = f"{ans_text}（出处定位：{'；'.join(valid_locs)}）"

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
                },
            }
            return

        observation: Any = None
        if action == 'search_tender':
            kw = action_input.get('keyword', '') if isinstance(action_input, dict) else str(action_input or '')
            observation = _search_text_windows(
                tender_text, kw, 'tender', page_spans=tender_ctx, client=client
            )
            for h in observation[:2]:
                collected_tool_quotes.append({
                    'source': 'tender', 'quote': h['quote'],
                    'offset': h['offset'], 'page': h['page'], 'quote_verified': True,
                })
        elif action == 'search_bid':
            kw = action_input.get('keyword', '') if isinstance(action_input, dict) else str(action_input or '')
            observation = _search_text_windows(
                bid_text, kw, 'bid', page_spans=bid_ctx, client=client
            )
            for h in observation[:2]:
                collected_tool_quotes.append({
                    'source': 'bid', 'quote': h['quote'],
                    'offset': h['offset'], 'page': h['page'], 'quote_verified': True,
                })
        elif action == 'locate_chapter':
            if isinstance(action_input, dict):
                target_doc = bid_text if action_input.get('doc') == 'bid' else tender_text
                ch_title = str(action_input.get('chapter_title') or action_input.get('keyword') or '')
            else:
                target_doc = tender_text
                ch_title = str(action_input or '')
            observation = locate_chapter(target_doc, ch_title)
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
                observation = {'error': 'recalc_price 需要 items 列表'}
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

        step_item = {
            'round': r_idx,
            'thought': thought,
            'action': action,
            'action_input': action_input,
            'observation': observation,
        }
        steps.append(step_item)
        yield {'type': 'step', 'data': step_item}

        messages.append({'role': 'assistant', 'content': raw_content})
        messages.append({
            'role': 'user',
            'content': f"工具 {action} 返回结果：{json.dumps(observation, ensure_ascii=False)}\n请继续下一步或输出 action='final'。",
        })

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
        },
    }


def ask_bid_expert(
    tender_path: str | Path,
    bid_path: str | Path,
    question: str,
    client=None,
    max_rounds: int = 6,
    reasoning_effort: Optional[str] = None,
    history: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """F7 标书专家 ReAct 追问同步主入口（消费 ask_bid_expert_stream 的 done 帧，公共签名与返回结构保持一字不变）。"""
    final_data: Optional[dict[str, Any]] = None
    for event in ask_bid_expert_stream(
        tender_path=tender_path,
        bid_path=bid_path,
        question=question,
        client=client,
        max_rounds=max_rounds,
        reasoning_effort=reasoning_effort,
        history=history,
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
