# -*- coding: utf-8 -*-
"""共享数据结构 —— 全项目接口约定的代码化。
所有模块 import 这里的数据类；字段名与类型不得修改。
"""


class ParsedDoc:
    """T1 文档解析产出。

    Attributes:
        text: 全文纯文本（含表格文字），段落以 \\n 连接。
        source_type: 'docx' | 'pdf' | 'txt'
        pages: 页数；txt/md 恒为 1。
    """

    def __init__(self, text: str, source_type: str, pages: int):
        self.text = text
        self.source_type = source_type
        self.pages = pages

    def __repr__(self):
        return (f"ParsedDoc(source_type={self.source_type!r}, pages={self.pages}, "
                f"chars={len(self.text)})")


class Clause:
    """T2 条款切分产出。

    Attributes:
        no: 条款编号原文，如 '第五条'；兜底分段时为 '段落1'/'段落2'；首部内容为 '首部'。
        title: 条款标题（该行 match 之后的文字，截取前 24 字），可为空串。
        start: 在全文中的起始字符偏移（int）。
        end: 结束偏移（不含）。
    """

    def __init__(self, no: str, title: str, start: int, end: int):
        self.no = no
        self.title = title
        self.start = start
        self.end = end

    def __repr__(self):
        return f"Clause({self.no}, {self.title!r}, {self.start}-{self.end})"


class Finding:
    """规则引擎 / LLM 审查的单条风险发现。

    Attributes:
        rule_id: 规则 ID，对应 rules.yaml 中的 id；LLM 发现以 'llm_' 前缀。
        rule_name: 风险名称，如 '违约金比例过高'。
        severity: 'high' | 'medium' | 'low'。
        category: 风险类别，如 '违约责任'。
        legal_basis: 法律依据（《民法典》第X条等）；无依据时为空串，输出端将屏蔽该条。
        clause_no: 定位条款，如 '第五条'；全文性问题为 '全篇'。
        quote: 原文摘录（必须能在原文定位，见 quote_verifier）。
        message: 风险说明。
        suggestion: 修改建议。
        pos: 在全文中的字符偏移（用于排序），无定位时为 -1。
        verified: 溯源校验是否通过；未通过的不进入报告。
    """

    def __init__(self, rule_id, rule_name, severity, category, legal_basis,
                 clause_no, quote, message, suggestion, pos=-1, verified=True):
        self.rule_id = rule_id
        self.rule_name = rule_name
        self.severity = severity
        self.category = category
        self.legal_basis = legal_basis
        self.clause_no = clause_no
        self.quote = quote
        self.message = message
        self.suggestion = suggestion
        self.pos = pos
        self.verified = verified

    def __repr__(self):
        return f"Finding([{self.severity}] {self.rule_id}@{self.clause_no})"


class LLMResponse:
    """T5 模型网关返回。

    Attributes:
        content: 模型输出文本。
        model: 实际使用的模型名。
        prompt_tokens / completion_tokens: token 用量。
        cost_estimate: 估算成本（元），按 models.yaml 的 pricing 计算。
    """

    def __init__(self, content, model, prompt_tokens=0, completion_tokens=0, cost_estimate=0.0):
        self.content = content
        self.model = model
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.cost_estimate = cost_estimate


class ReviewResult:
    """T4 主循环最终产出。

    Attributes:
        findings: 通过校验的风险清单（按 severity 与 pos 排序）。
        clauses: 条款切分结果。
        stats: 统计 {'pages': int, 'clause_count': int, 'chars': int,
                     'high': int, 'medium': int, 'low': int,
                     'prompt_tokens': int, 'completion_tokens': int, 'cost': float,
                     'elapsed_sec': float}
        trace: 执行轨迹 [{'step': 状态名, 'detail': str, 'elapsed': float}, ...]
    """

    def __init__(self, findings, clauses, stats, trace):
        self.findings = findings
        self.clauses = clauses
        self.stats = stats
        self.trace = trace
