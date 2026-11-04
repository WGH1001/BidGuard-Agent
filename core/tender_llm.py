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
import json
import os
import re
from typing import Optional

from core.llm_gateway import get_llm_client
from core.quote_verifier import verify_quote
from core.tender_extractor import build_review_checklist

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
2. clause_type：star_param（★强制参数）/ triangle_param（▲重要参数）/
   qualification（资格审查项）/ compliance（符合性审查项）/ scoring（评分项）/ other；
3. clause_no：条款编号（如 "★1)" "（十四）" "2.1"，窗口中有则逐字抄录，无则空串）；
4. full_text：该条款在窗口中的**完整原文**——逐字摘录，含标记符号，从条款开头
   到条款结束（窗口截断则以窗口末尾为界），严禁改写、缩写、翻译或补写；
5. reason：30 字以内判定理由。

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


def extract_tender_with_llm(file_path: str, client=None, env: Optional[str] = None) -> dict:
    """F1 主入口：确定性层圈选 → LLM 逐段结构化。

    Args:
        file_path: 招标文件路径（docx/pdf/txt）。
        client: LLMClient 实例；None 时自动 get_llm_client(env)。
        env: 模型环境名（demo=智谱 / prod=内网网关），client 传入时忽略。

    Returns:
        build_review_checklist 的结果 + 'llm_structured'（结构化后的 requirement
        列表，含 usage_note/table_marker 原样保留不送 LLM）+ 'llm_usage'（成本）。
        client 为 None 时仅返回确定性层结果（llm_structured 缺省）。
    """
    result = build_review_checklist(file_path)

    reqs = [c for c in result['marked_clauses'] if c.get('kind') == 'requirement']
    # 模式 A/B 措辞型候选同样送结构化（无标记语料的实质要求全靠它们）
    wording = list(result['invalid_bid_clauses'])
    if not reqs and not wording:
        result['llm_structured'] = []
        result['llm_usage'] = {'calls': 0}
        return result

    if client is None:
        client = get_llm_client(env)

    full_text = result['text'] if 'text' in result else None
    if full_text is None:
        # build_review_checklist 不回传全文，按记录偏移直接从文件解析一次
        from core.parser import parse_document
        full_text = parse_document(file_path).text

    structured = []
    for cand in reqs + wording:
        window = _expand_window(full_text, cand['start'], cand['end'])
        structured.append(structure_candidate(window, cand, client))

    usage = client.total_usage() if hasattr(client, 'total_usage') else {}
    result['llm_structured'] = structured
    result['llm_usage'] = {
        'calls': len(structured),
        'usage': usage,
    }
    return result
