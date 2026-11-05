# -*- coding: utf-8 -*-
"""F7 标书专家 ReAct 追问引擎（★大模型③）—— 跨双书工具检索与溯源问答。

模块定位（对应 PRD-02 §7 F7 与 F6+F7 任务卡）：
1. 工具层（全部为 0 Token 确定性函数）：
   - search_tender(keyword, ...) / search_bid(keyword, ...)：双书关键词检索，返回 ±200 字符窗口+偏移+页码；
   - locate_chapter(text, chapter_title)：章节定位（复用 tender_extractor）；
   - recalc_price(items, ...)：调用 PriceSandbox 复算报价；
   - check_cert_expiry(cert_text, open_date, ...)：调用 F3 scan_qualifications 核验效期。
2. ReAct 循环：
   - LLM 每轮输出 JSON {'thought': ..., 'action': 'tool名'|'final', 'action_input': ...}；
   - 严格限制最多 max_rounds=6 轮防止死循环；
   - final 轮输出 {'answer', 'quotes': [...]}，每条 quote 强制走 verify_quote 溯源，
     失败的清空该条并标注 quote_verified=False；
   - 引用位置附带字符偏移（PDF 换算真实页码，docx 页码为 None 绝不编造）。
3. 0 Token 降级（client=None）：
   - 不做 LLM 推理，直接提取问题关键词跑 search_tender / search_bid 返回命中窗口，标注 mode='keyword_fallback'。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from core.parser import parse_document
from core.price_sandbox import PriceSandbox
from core.qualification_scanner import scan_qualifications
from core.quote_verifier import verify_quote
from core.tender_extractor import locate_chapters, locate_keyword_sections

_SEARCH_WINDOW = 200
_MAX_HITS_PER_SEARCH = 5
_JSON_BLOCK_RE = re.compile(r'\{.*\}', re.DOTALL)

_QUESTION_STOPWORDS = (
    '请问', '招标文件', '招标书', '投标文件', '投标书', '我们', '我方', '甲方',
    '要求', '响应', '在哪里', '在哪', '哪一页', '哪页', '是多少', '多少', '什么',
    '几年', '几个月', '是否', '有没有', '如何', '怎么', '的', '了', '吗', '呢',
)

_REACT_SYSTEM_PROMPT = """你是招投标双书合规审查专家（ReAct Agent）。你可以调用以下 5 个确定性工具来回答用户关于《招标文件》与《投标文件》的问题：

可用工具列表：
1. "search_tender"：在招标文件中检索关键词，action_input 为关键词字符串（如 "质保期"）；
2. "search_bid"：在投标文件中检索关键词，action_input 为关键词字符串（如 "质保期"）；
3. "locate_chapter"：定位章节标题，action_input 为章节关键词字符串（如 "评标办法"）；
4. "recalc_price"：调用报价算术沙盒复算分项报价，action_input 为 {"items": [...], "bid_total": ...}；
5. "check_cert_expiry"：调用证照效期核验器，action_input 为 {"cert_text": "...", "open_date": "YYYY-MM-DD"}；
6. "final"：当你已通过工具拿到原文证据准备给出最终回答时使用，action_input 为：
   {"answer": "最终解答正文", "quotes": ["逐字摘自原文的引用句1", "逐字摘自原文的引用句2"]}

每轮你必须只输出一个 JSON 对象，格式如下：
{
  "thought": "思考过程",
  "action": "search_tender" | "search_bid" | "locate_chapter" | "recalc_price" | "check_cert_expiry" | "final",
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


def _build_pdf_page_spans(file_path: Optional[str | Path]) -> list[tuple[int, int, int]]:
    """PDF 文件用 pymupdf 计算每页字符偏移区间 (start, end, page_no)；docx 返回空列表（页码置 None）。"""
    if not file_path:
        return []
    path = Path(file_path)
    if not path.exists() or path.suffix.lower() != '.pdf':
        return []
    try:
        import pymupdf
        spans: list[tuple[int, int, int]] = []
        cursor = 0
        with pymupdf.open(str(path)) as doc:
            for idx, page in enumerate(doc):
                ptxt = page.get_text()
                end = cursor + len(ptxt)
                spans.append((cursor, end, idx + 1))
                cursor = end + 1
        return spans
    except Exception:
        return []


def _offset_to_page(offset: Optional[int], page_spans: list[tuple[int, int, int]]) -> Optional[int]:
    if offset is None or offset < 0 or not page_spans:
        return None
    for lo, hi, pno in page_spans:
        if lo <= offset <= hi:
            return pno
    return page_spans[-1][2] if offset >= page_spans[-1][0] else None


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
    page_spans: Optional[list[tuple[int, int, int]]] = None,
) -> list[dict[str, Any]]:
    """在文本中检索关键词，返回 ±200 字符窗口、字符偏移与页码（PDF 附真实页码，docx 为 None）。"""
    if not text or not keyword:
        return []

    kw = str(keyword).strip()
    search_terms = [kw] if kw in text else _extract_keywords_from_question(kw)

    hits: list[dict[str, Any]] = []
    seen_offsets: list[int] = []
    spans = page_spans or []

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
                    'page': _offset_to_page(idx, spans),
                })
            start = idx + len(term)
        if hits:
            break

    return hits


# ── 工具层：4 组确定性工具函数（支持独立调用与 ReAct 挂载）────────────────

def search_tender(
    keyword: str,
    tender_text: str = '',
    window: int = _SEARCH_WINDOW,
    page_spans: Optional[list[tuple[int, int, int]]] = None,
) -> list[dict[str, Any]]:
    """工具 1a：在招标文件中检索关键词，返回 ±200 字符窗口 + 偏移 + 页码。"""
    return _search_text_windows(tender_text, keyword, 'tender', window=window, page_spans=page_spans)


def search_bid(
    keyword: str,
    bid_text: str = '',
    window: int = _SEARCH_WINDOW,
    page_spans: Optional[list[tuple[int, int, int]]] = None,
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


# ── 引用溯源核验与偏移/页码绑定 ───────────────────────────────────────────

def _verify_and_locate_quotes(
    raw_quotes: list[Any],
    tender_text: str,
    bid_text: str,
    tender_pages: list[tuple[int, int, int]],
    bid_pages: list[tuple[int, int, int]],
) -> list[dict[str, Any]]:
    """对 LLM final 输出的每条 quote 强制执行 verify_quote 溯源：
    - 通过：附带真实字符 offset 与 page（PDF 为页码，docx 为 None）；
    - 失败：清空 quote 为 ''，标 quote_verified=False 与 note='引用未通过原文溯源校验，已清空'。
    """
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
            # 溯源失败：清空该条并显式标注
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
        p_spans = tender_pages if src == 'tender' else bid_pages
        off = doc_txt.find(q_str)
        if off < 0:
            # 若因标点/空白归一化命中，定位首 8 字偏移
            head = re.sub(r'\s+', '', q_str)[:8]
            off = doc_txt.find(head) if head else 0
            if off < 0:
                off = 0

        verified_list.append({
            'source': src,
            'quote': q_str,
            'offset': off,
            'page': _offset_to_page(off, p_spans),
            'quote_verified': True,
        })

    return verified_list


# ── ReAct 主入口 ──────────────────────────────────────────────────────────

def ask_bid_expert(
    tender_path: str | Path,
    bid_path: str | Path,
    question: str,
    client=None,
    max_rounds: int = 6,
) -> dict[str, Any]:
    """F7 标书专家 ReAct 追问主入口。

    Args:
        tender_path: 招标文件路径（docx/pdf/txt）。
        bid_path: 投标文件路径（docx/pdf/txt）。
        question: 投标专员提问内容。
        client: LLM 客户端；为 None 时执行 0 Token 关键词检索降级（mode='keyword_fallback'）。
        max_rounds: ReAct 最大循环轮数（默认 6 轮，防死循环）。

    Returns:
        {
            'mode': 'react' | 'keyword_fallback',
            'question': str,
            'answer': str,
            'quotes': list[dict],          # 每条含 source/quote/offset/page/quote_verified
            'steps': list[dict],           # ReAct 推理与工具调用轨迹
            'rounds_used': int,
            'terminated_by_max_rounds': bool,
        }
    """
    tender_doc = parse_document(str(tender_path))
    bid_doc = parse_document(str(bid_path))
    tender_text = tender_doc.text or ''
    bid_text = bid_doc.text or ''

    tender_pages = _build_pdf_page_spans(tender_path)
    bid_pages = _build_pdf_page_spans(bid_path)

    # ── 1. 0 Token 降级模式（client is None）──
    if client is None:
        keywords = _extract_keywords_from_question(question)
        t_hits: list[dict[str, Any]] = []
        b_hits: list[dict[str, Any]] = []
        for kw in keywords:
            t_hits.extend(search_tender(kw, tender_text=tender_text, page_spans=tender_pages))
            b_hits.extend(search_bid(kw, bid_text=bid_text, page_spans=bid_pages))

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
        if t_hits:
            summary_parts.append(f"【招标文件命中】{t_hits[0]['quote']}（@字符{t_hits[0]['offset']}）")
        if b_hits:
            summary_parts.append(f"【投标文件命中】{b_hits[0]['quote']}（@字符{b_hits[0]['offset']}）")
        answer = '\n'.join(summary_parts) if summary_parts else '未在招标文件或投标文件中检索到对应关键词内容。'

        return {
            'mode': 'keyword_fallback',
            'question': question,
            'answer': answer,
            'quotes': quotes,
            'steps': [{
                'round': 1,
                'thought': '0 Token 关键词检索降级模式',
                'action': 'keyword_search',
                'action_input': keywords,
                'observation': {'tender_hits': len(t_hits), 'bid_hits': len(b_hits)},
            }],
            'rounds_used': 1,
            'terminated_by_max_rounds': False,
        }

    # ── 2. ReAct 多轮工具调用循环（最多 max_rounds 轮）──
    messages = [
        {'role': 'system', 'content': _REACT_SYSTEM_PROMPT},
        {'role': 'user', 'content': f"用户问题：{question}"},
    ]
    steps: list[dict[str, Any]] = []
    collected_tool_quotes: list[dict[str, Any]] = []

    limit = max(1, int(max_rounds))
    for r_idx in range(1, limit + 1):
        resp = client.chat(messages, model_tier='flash')
        raw_content = getattr(resp, 'content', '') or ''
        parsed = _parse_llm_json(raw_content)

        if not parsed:
            steps.append({
                'round': r_idx,
                'thought': '模型未返回有效 JSON，直接结束',
                'action': 'final',
                'action_input': {},
                'observation': None,
            })
            return {
                'mode': 'react',
                'question': question,
                'answer': raw_content.strip() or '未能生成有效回答。',
                'quotes': collected_tool_quotes,
                'steps': steps,
                'rounds_used': r_idx,
                'terminated_by_max_rounds': False,
            }

        thought = str(parsed.get('thought') or '').strip()
        action = str(parsed.get('action') or 'final').strip()
        action_input = parsed.get('action_input')

        # 若到达 final 动作（或顶层直接带 answer）
        if action == 'final' or ('answer' in parsed and action not in (
            'search_tender', 'search_bid', 'locate_chapter', 'recalc_price', 'check_cert_expiry'
        )):
            final_payload = action_input if isinstance(action_input, dict) else parsed
            ans_text = str(final_payload.get('answer') or parsed.get('answer') or '').strip()
            raw_quotes = final_payload.get('quotes') if 'quotes' in final_payload else parsed.get('quotes', [])
            if not isinstance(raw_quotes, list):
                raw_quotes = [raw_quotes] if raw_quotes else []

            verified_quotes = _verify_and_locate_quotes(
                raw_quotes=raw_quotes,
                tender_text=tender_text,
                bid_text=bid_text,
                tender_pages=tender_pages,
                bid_pages=bid_pages,
            )

            # 在回答末尾附上有效引用的位置坐标（PDF 附页码，docx 附字符偏移）
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

            steps.append({
                'round': r_idx,
                'thought': thought,
                'action': 'final',
                'action_input': final_payload,
                'observation': None,
            })
            return {
                'mode': 'react',
                'question': question,
                'answer': ans_text,
                'quotes': verified_quotes,
                'steps': steps,
                'rounds_used': r_idx,
                'terminated_by_max_rounds': False,
            }

        # 执行工具调用
        observation: Any = None
        if action == 'search_tender':
            kw = action_input.get('keyword', '') if isinstance(action_input, dict) else str(action_input or '')
            observation = search_tender(kw, tender_text=tender_text, page_spans=tender_pages)
            for h in observation[:2]:
                collected_tool_quotes.append({
                    'source': 'tender', 'quote': h['quote'],
                    'offset': h['offset'], 'page': h['page'], 'quote_verified': True,
                })
        elif action == 'search_bid':
            kw = action_input.get('keyword', '') if isinstance(action_input, dict) else str(action_input or '')
            observation = search_bid(kw, bid_text=bid_text, page_spans=bid_pages)
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
        else:
            observation = {'error': f"未知工具: {action}"}

        steps.append({
            'round': r_idx,
            'thought': thought,
            'action': action,
            'action_input': action_input,
            'observation': observation,
        })

        messages.append({'role': 'assistant', 'content': raw_content})
        messages.append({
            'role': 'user',
            'content': f"工具 {action} 返回结果：{json.dumps(observation, ensure_ascii=False)}\n请继续下一步或输出 action='final'。",
        })

    # ── 达到 max_rounds 上限强制终止保护 ──
    fallback_ans = '已达到最大工具检索轮数上限（6轮），以下为已检索到的原文证据汇总。'
    return {
        'mode': 'react',
        'question': question,
        'answer': fallback_ans,
        'quotes': collected_tool_quotes,
        'steps': steps,
        'rounds_used': limit,
        'terminated_by_max_rounds': True,
    }