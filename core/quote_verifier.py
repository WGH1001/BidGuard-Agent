# -*- coding: utf-8 -*-
"""T6 中文归一化 + Quote 溯源校验 [Gemini-Pro] —— 0 token 防幻觉核心

实现规格（签名不得修改）：
    normalize_zh(text) -> str
    verify_quote(quote, source_text, threshold=0.90) -> bool

行为要求：
    1. normalize_zh(text) 归一化（顺序执行）：
       a) 去除所有空白字符（空格、\t、\n、\r、\u3000 全角空格）；
       b) 全角转半角：ASCII 范围全角字符 -0xFEE0 偏移（'：'->':', '（）'->'()' 等）；
       c) 中文标点归一映射：'"' -> '"'，''' -> '\''，'，'->',',
          '。'->'.'，'；'->';'，'？'->'?'，'！'->'!'，'——'->'-'，'…'->'.'，'·'->'.'；
       d) 中文数字保留原样（不做数值转换）；
       e) 返回处理后字符串。
    2. verify_quote(quote, source_text, threshold)：
       a) quote 或 source 归一化后为空 -> False；
       b) 第一级：normalize_zh(quote) in normalize_zh(source_text) -> True；
       c) 第二级（子串失败时的兜底）：quote 先剥掉首尾包裹性引号（“”「」等），
          然后用 difflib.SequenceMatcher.find_longest_match 求归一化后的
          最长连续公共块，块长 / len(quote_norm) >= threshold -> True；
          —— 语义是"quote 至少 threshold 比例是原文中一段连续逐字文本"，
          能拦截"中间篡改数字"型编造（相似度比值拦不住）；
          source 过长（>100000 字符）时跳过本级直接 False（性能保护）；
       d) 两级都失败 -> False。
    3. 纯标准库实现（re / difflib / unicodedata 可用），禁止第三方依赖。

设计动机（给 Gemini 的背景）：LLM 引用原文时经常改动全/半角标点、空格、换行，
不做归一化会让"严格防幻觉"变成"无差别拒绝"。本模块保证：真实引用必通过，
编造内容必被拦截。
"""
import re
import difflib


def normalize_zh(text: str) -> str:
    """中文文本归一化：去空白、全角转半角、标点归一。"""
    if not text:
        return ""

    # a) 去除所有空白字符
    text = re.sub(r'[\s\u3000]+', '', text)

    # b) 全角转半角
    chars = []
    for char in text:
        code = ord(char)
        if 0xFF01 <= code <= 0xFF5E:
            chars.append(chr(code - 0xFEE0))
        else:
            chars.append(char)
    text = ''.join(chars)

    # c) 中文标点归一映射
    mapping = {
        '“': '"', '”': '"',
        '‘': "'", '’': "'",
        '，': ',',
        '。': '.',
        '；': ';',
        '？': '?',
        '！': '!',
        '…': '.',
        '·': '.'
    }
    trans = str.maketrans(mapping)
    text = text.translate(trans)

    # 处理多个字符映射为单个或多个的情况
    text = text.replace('——', '-')

    return text


def verify_quote(quote: str, source_text: str, threshold: float = 0.90) -> bool:
    """校验 quote 是否真实来源于 source_text。

    Args:
        quote: LLM 给出的原文摘录。
        source_text: 合同全文。
        threshold: 模糊匹配相似度阈值。
    """
    if not quote or not source_text:
        return False

    # 0) 剥掉包裹性引号（LLM 常给摘录套 “”「」 等，引号是定界符不是内容）
    quote = _strip_wrapping_marks(quote.strip())

    quote_norm = normalize_zh(quote)
    source_norm = normalize_zh(source_text)

    # a) quote 或 source 归一化后为空 -> False
    if not quote_norm or not source_norm:
        return False

    # b) 第一级：精确包含
    if quote_norm in source_norm:
        return True

    # c) 第二级：最长连续逐字段兜底
    #    语义：quote 归一化后，必须有一段"连续逐字匹配原文"的长段，其长度占比 >= threshold。
    #    真实摘录（哪怕头尾多包了引号/漏了标点）必有一大段连续匹配；
    #    篡改数字等"中间打断"型编造会把连续段截短，从而被拦截——
    #    这是相似度比值做不到的（改 1 个字的相似度可达 0.95）。
    if len(source_norm) > 100000:
        return False

    q_len = len(quote_norm)
    sm = difflib.SequenceMatcher(None, quote_norm, source_norm, autojunk=False)
    anchor = sm.find_longest_match(0, q_len, 0, len(source_norm))
    if anchor.size == 0:
        return False
    return (anchor.size / q_len) >= threshold


_WRAP_PAIRS = "“”「」『』‘’\"'"


def _strip_wrapping_marks(s: str) -> str:
    """递归剥掉成对包裹在首尾的引号/书名号类定界符。"""
    while len(s) >= 2 and s[0] in _WRAP_PAIRS and s[-1] in _WRAP_PAIRS:
        s = s[1:-1].strip()
    return s