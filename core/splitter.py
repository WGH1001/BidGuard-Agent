# -*- coding: utf-8 -*-
r"""T2 条款切分器 [Gemini-Pro] —— 本项目的分块基础（替代通用分块器）

实现规格（签名不得修改）：
    split_clauses(text) -> list[Clause]

行为要求：
    1. 主策略——按条款切分：
       - 正则在 MULTILINE 模式下匹配行首条款号：
         pattern = r'^[ \t\u3000]*第\s*([零一二三四五六七八九十百0-9]{1,7})\s*([条章节])'
       - 命中数 >= 2 时启用：
         a) 每个 Clause：no = 匹配文本 strip 去空格（如 '第五条'）；
            title = 匹配行内 match 之后到行尾的文字，strip 后截取前 24 字；
            start = match.start()；end = 下一个 match 的 start（最后一条到 len(text)）。
         b) 若第一个 match 前还有内容，插入一个 Clause(no='首部', title='', start=0, end=第一个match.start())。
         c) 尾部签署栏独立成块：最后一个条款内若出现签署行（甲方（签字盖章）/签订日期 等模式），
            自该行起切出 Clause(no='签署栏')，不再挂靠最后一个条款（T13）。
    2. 兜底策略——命中 < 2 条（无条款号的合同）：
       - 按段落（text.split('\n'）顺序聚合，每累计 >= 400 字符切一块；
       - Clause(no=f'段落{i}', title='', start=块起始偏移, end=块结束偏移)，i 从 1 开始；
       - 偏移按原文字符数累计（段落长度 + 1 个换行符）。
    3. 空 text：返回 [Clause(no='全文', title='', start=0, end=0)]。
    4. 偏移量必须与 text 完全对齐（用 text[start:end] 切片应得到该条款原文）——
       这是后续 quote 溯源定位的根基，验收时会用随机条款抽样校验。

禁止事项：
    - 不得引入第三方库（re/标准库随便用）；
    - 不得修改签名；不得 print。
"""
import re

from core.interfaces import Clause

# 签署行模式：行首"甲方（签字盖章）：" / "乙方（签章）：" / "签订日期：__年__月__日" 等
SIG_RE = re.compile(
    r'^[ \t　]*(?:(?:甲方|乙方|丙方|发包人|承包人|出卖人|买受人)[（(](?:签字|盖章|签章)[^）)]{0,4}[）)]|签订日期)',
    re.MULTILINE)


def split_clauses(text: str) -> list[Clause]:
    """将合同全文切分为条款列表（含字符偏移量）。

    Args:
        text: 全文纯文本。

    Returns:
        list[Clause]: 按出现顺序排列；详见模块 docstring 行为要求。
    """
    if not text:
        return [Clause(no='全文', title='', start=0, end=0)]

    pattern = re.compile(
        r'^[ \t\u3000]*第\s*([零一二三四五六七八九十百0-9]{1,7})\s*([条章节])',
        re.MULTILINE
    )
    matches = list(pattern.finditer(text))

    if len(matches) >= 2:
        clauses = []
        first_match_start = matches[0].start()

        if first_match_start > 0:
            clauses.append(Clause(no='首部', title='', start=0, end=first_match_start))

        for i, match in enumerate(matches):
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)

            no_text = re.sub(r'\s+', '', match.group())

            line_end = text.find('\n', match.end())
            if line_end == -1:
                line_end = len(text)

            title_text = text[match.end():line_end].strip()
            title = title_text[:24]

            clauses.append(Clause(no=no_text, title=title, start=start, end=end))

        # T13：签署栏从最后一个条款中独立出来（偏移量保持无缝覆盖）
        last = clauses[-1]
        m_sig = SIG_RE.search(text, last.start)
        if m_sig and m_sig.start() > last.start:
            last.end = m_sig.start()
            clauses.append(Clause(no='签署栏', title='', start=m_sig.start(), end=len(text)))

        return clauses

    else:
        # 兜底策略：命中 < 2 条，按"软单元"聚合切块。
        # 软单元 = 段落；单段超过 400 字符时按句末标点（。！？；）再切（应对 PDF 解析出的无换行长文本）。
        units = []  # (start, end) 绝对偏移，不含换行符
        pos = 0
        for p in text.split('\n'):
            if len(p) > 400:
                for m in re.finditer(r'[^。！？；]*[。！？；]|[^。！？；]+$', p):
                    units.append((pos + m.start(), pos + m.end()))
            elif p:
                units.append((pos, pos + len(p)))
            pos += len(p) + 1  # 跳过换行符

        # 聚合：从块首累计到 >= 400 字符即切块
        blocks = []  # [start, end]
        i = 0
        while i < len(units):
            start, end = units[i]
            i += 1
            while i < len(units) and end - start < 400:
                end = units[i][1]
                i += 1
            blocks.append([start, end])

        # 无缝对齐：每块 end = 下一块 start（换行符归前块），末块到全文结尾
        for k in range(len(blocks) - 1):
            blocks[k][1] = blocks[k + 1][0]
        if blocks:
            blocks[-1][1] = len(text)

        clauses = [Clause(no=f'段落{k + 1}', title='', start=s, end=e)
                   for k, (s, e) in enumerate(blocks) if e > s]
        if not clauses:
            clauses = [Clause(no='段落1', title='', start=0, end=len(text))]
        return clauses