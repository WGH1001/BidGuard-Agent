# -*- coding: utf-8 -*-
"""E40 · 全局隐私合规脱敏引擎（身份证号 + 银行卡/对公账号，0 Token 确定性等长掩码）。

设计铁律：
1. 绝不落地改写原始 `.docx/.pdf` 物理文件（原文件仅供本地 0-Token 引擎只读消费）；
2. 仅识别两类个人/资金敏感编号：
   - 18 位中国居民身份证号（强制通过 GB 11643-1999 校验位与有效出生日期验证，防误伤长编号）；
   - 12~25 位银行卡/对公银行账号（允许 4 位空格/连字符分组，必须命中银行/账号等语境且排除项目编号/标准号/报价金额）；
3. 等长掩码且严格幂等：保留前 3 后 4 位（身份证）或前 4 后 4 位（银行账号）及原分隔符，中间数字等长替换为 `*`；
4. 上传阶段轻量化扫码计数一律设 `ocr_images=False`，严禁在大标书同步上传时触发阻塞性全图 OCR。
"""
from __future__ import annotations

import datetime
from pathlib import Path
import re
from typing import Any

from core.parser import parse_document

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK_MAP = '10X98765432'
_VALID_PROVINCE_PREFIXES = frozenset({
    '11', '12', '13', '14', '15',
    '21', '22', '23',
    '31', '32', '33', '34', '35', '36', '37',
    '41', '42', '43', '44', '45', '46',
    '50', '51', '52', '53', '54',
    '61', '62', '63', '64', '65',
    '71', '81', '82', '91',
})

# 18 位居民身份证候选串（前后不粘连字母或数字）
_ID_CARD_CANDIDATE_RE = re.compile(
    r'(?<![A-Za-z0-9*])([1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx])(?![A-Za-z0-9*])'
)

# 12~25 位银行卡/对公账号候选串（连续 12~25 位数字，或 3~6 位空格/短横线分组；防吞并相邻年份与金额小数点）
_BANK_CANDIDATE_RE = re.compile(
    r'(?<![A-Za-z0-9.*\-])((?:\d{3,6}[ \-]){2,5}\d{1,6}|\d{12,25})(?![A-Za-z0-9.*\-])'
)

_BANK_CONTEXT_RE = re.compile(
    r'(?:银行|卡号|账号|账户|开户行|开户银行|基本户|一般户|对公|储蓄卡|借记卡|信用卡|收款账户|付款账户|结算账户|银联|联行号|行号)'
)

_NON_BANK_ADJACENT_RE = re.compile(
    r'(?:项目编号|招标编号|采购编号|合同编号|统一社会信用代码|社会信用代码|信用代码|纳税人识别号|税号|'
    r'营业执照号?|登记号|证书编号|专利号|标准号|流水号|凭证号|发票号|订单号|运单号|快递单号|'
    r'GB/T|GB|ISO|RFC|ZC|HT)\s*[：:.\- ]*$',
    re.IGNORECASE,
)

_MONEY_OR_UNIT_SUFFIX_RE = re.compile(r'^\s*(?:万元|亿元|元|%|％)')


def is_valid_cn_id_card(raw: str) -> bool:
    """按 GB 11643-1999 校验 18 位居民身份证号省份、合法公历出生日期与末位校验码。"""
    s = (raw or '').strip().upper()
    if len(s) != 18 or not s[:17].isdigit():
        return False
    if s[:2] not in _VALID_PROVINCE_PREFIXES:
        return False
    try:
        year = int(s[6:10])
        month = int(s[10:12])
        day = int(s[12:14])
        born = datetime.date(year, month, day)
        max_year = datetime.date.today().year + 1
        if year < 1900 or year > max_year or born > datetime.date.today() + datetime.timedelta(days=366):
            return False
    except ValueError:
        return False

    total = sum(int(s[i]) * _ID_WEIGHTS[i] for i in range(17))
    expected_check = _ID_CHECK_MAP[total % 11]
    return s[17] == expected_check


def _mask_id_equal_len(raw: str) -> str:
    if len(raw) != 18:
        return raw
    return f"{raw[:3]}{'*' * 11}{raw[-4:]}"


def _mask_bank_equal_len(raw: str) -> str:
    """保留前 4 后 4 位数字与原有空格/连字符分隔符，中间数字位等长替换为 `*`。"""
    digit_indices = [idx for idx, ch in enumerate(raw) if ch.isdigit()]
    if len(digit_indices) < 12 or len(digit_indices) > 25:
        return raw
    mask_positions = set(digit_indices[4:-4])
    return ''.join('*' if idx in mask_positions else ch for idx, ch in enumerate(raw))


def _is_bank_account_in_context(full_text: str, start: int, end: int, raw: str) -> bool:
    if '*' in raw:
        return False
    digits_only = ''.join(ch for ch in raw if ch.isdigit())
    if len(digits_only) < 12 or len(digits_only) > 25:
        return False
    # 拒绝跨越多重不规则单个短隔断或明显带日期特征的编码
    if '--' in raw or '  ' in raw:
        return False
    # 同行上下文（不跨越换行符，防上一行项目编号/银行污染下一行）
    line_start = full_text.rfind('\n', 0, start) + 1
    line_end = full_text.find('\n', end)
    if line_end == -1:
        line_end = len(full_text)

    win_left = max(line_start, start - 50)
    win_right = min(line_end, end + 24)
    left_near = full_text[max(line_start, start - 28):start]
    right_near = full_text[end:min(line_end, end + 12)]

    if _NON_BANK_ADJACENT_RE.search(left_near):
        return False
    if _MONEY_OR_UNIT_SUFFIX_RE.match(right_near):
        return False
    m_lbl = re.search(r'([^\s，。,；;、\n：:]{2,20})\s*[：:]\s*$', left_near)
    if m_lbl and not _BANK_CONTEXT_RE.search(m_lbl.group(1)) and not _BANK_CONTEXT_RE.search(right_near):
        return False

    context_window = full_text[win_left:win_right]
    return bool(_BANK_CONTEXT_RE.search(context_window))


def sanitize_text(text: str) -> tuple[str, dict[str, int]]:
    """对文本执行身份证号（GB 11643 校验）与银行卡/对公账号（语境+等长掩码）脱敏。

    Returns:
        (sanitized_text, {'id_masked': int, 'bank_masked': int})
    """
    if not text or not isinstance(text, str):
        return (text or ''), {'id_masked': 0, 'bank_masked': 0}

    id_masked = 0
    bank_masked = 0

    def _replace_id(m: re.Match) -> str:
        nonlocal id_masked
        cand = m.group(1)
        if is_valid_cn_id_card(cand):
            id_masked += 1
            return _mask_id_equal_len(cand)
        return cand

    step1 = _ID_CARD_CANDIDATE_RE.sub(_replace_id, text)

    def _replace_bank(m: re.Match) -> str:
        nonlocal bank_masked
        cand = m.group(1)
        if _is_bank_account_in_context(step1, m.start(1), m.end(1), cand):
            bank_masked += 1
            return _mask_bank_equal_len(cand)
        return cand

    step2 = _BANK_CANDIDATE_RE.sub(_replace_bank, step1)
    return step2, {'id_masked': id_masked, 'bank_masked': bank_masked}


def scan_document_text(text: str) -> dict[str, int]:
    """轻量级只读扫描统计（不改动原文本），返回 `{'id_masked': x, 'bank_masked': y}`。"""
    if not text or not isinstance(text, str):
        return {'id_masked': 0, 'bank_masked': 0}
    _, stats = sanitize_text(text)
    return stats


def scan_document_file(file_path: str | Path) -> dict[str, int]:
    """上传受理阶段零 OCR 快速扫描统计（强制 `ocr_images=False`，绝不触发耗时 OCR）。"""
    path = Path(file_path)
    if not path.is_file():
        return {'id_masked': 0, 'bank_masked': 0}
    try:
        parsed = parse_document(str(path), ocr_images=False)
        return scan_document_text(getattr(parsed, 'text', '') or '')
    except Exception:
        return {'id_masked': 0, 'bank_masked': 0}


def merge_privacy_stats(*stats_list: dict[str, int] | None) -> dict[str, int]:
    """合并多份文档的脱敏计数统计。"""
    id_total = 0
    bank_total = 0
    for st in stats_list:
        if isinstance(st, dict):
            id_total += int(st.get('id_masked') or 0)
            bank_total += int(st.get('bank_masked') or 0)
    return {'id_masked': id_total, 'bank_masked': bank_total}


def sanitize_chat_messages(messages: list[Any]) -> tuple[list[Any], dict[str, int]]:
    """对 OpenAI 格式消息列表做深拷贝脱敏（仅处理文本段，完整保留 E24 `image_url` 等结构）。"""
    if not messages:
        return [], {'id_masked': 0, 'bank_masked': 0}

    out_msgs: list[Any] = []
    id_total = 0
    bank_total = 0

    for msg in messages:
        if not isinstance(msg, dict):
            out_msgs.append(msg)
            continue
        new_msg = dict(msg)
        content = msg.get('content')
        if isinstance(content, str):
            clean_c, st = sanitize_text(content)
            new_msg['content'] = clean_c
            id_total += st['id_masked']
            bank_total += st['bank_masked']
        elif isinstance(content, list):
            new_blocks: list[Any] = []
            for blk in content:
                if isinstance(blk, dict) and blk.get('type') == 'text' and isinstance(blk.get('text'), str):
                    clean_t, st = sanitize_text(blk['text'])
                    nb = dict(blk)
                    nb['text'] = clean_t
                    new_blocks.append(nb)
                    id_total += st['id_masked']
                    bank_total += st['bank_masked']
                else:
                    new_blocks.append(blk)
            new_msg['content'] = new_blocks

        # 若消息带 tool_calls / function 参数串，一并脱敏保护
        if isinstance(msg.get('tool_calls'), list):
            new_tcs = []
            for tc in msg['tool_calls']:
                if isinstance(tc, dict) and isinstance(tc.get('function'), dict):
                    fn_d = dict(tc['function'])
                    args_v = fn_d.get('arguments')
                    if isinstance(args_v, str):
                        clean_a, st = sanitize_text(args_v)
                        fn_d['arguments'] = clean_a
                        id_total += st['id_masked']
                        bank_total += st['bank_masked']
                    tc_copy = dict(tc)
                    tc_copy['function'] = fn_d
                    new_tcs.append(tc_copy)
                else:
                    new_tcs.append(tc)
            new_msg['tool_calls'] = new_tcs

        out_msgs.append(new_msg)

    return out_msgs, {'id_masked': id_total, 'bank_masked': bank_total}
