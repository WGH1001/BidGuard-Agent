# -*- coding: utf-8 -*-
"""双书条款对齐引擎（摘自 Contract-Review-Agent T57 contract_diff，剥离规则引擎耦合）。

100% 纯 Python 确定性核心，0 Token。本项目定位：招标书（base）↔ 投标书（target）
的条款级勾稽对齐心脏（F4 ★条款逐条响应核验 / F5 偏离表对齐的底层算法）。

设计目标：
1. 解决「插入/删除条款引发全文编号顺延错位」的条款对齐痛点（招标书条目序 vs 投标书响应序）；
2. 保留五类关键要素异动雷达（金额/管辖/主体/语气/期限，确定性正则）——
   本项目中用于「表面响应、暗改参数」型穿帮检测与标书前后承诺矛盾定位。

与原模块的差异（剥离记录）：
- 移除 compute_risk_delta（增量风险消长）及 _locate_finding_clause_key /
  _finding_to_delta_item —— 该段依赖旧项目 rule_engine 的 load_rules/run_rules，
  属合同审查业务耦合；本项目风险核验由 F2/F3/F4 各自的确定性引擎承担。
- 保留完整四阶段 + Stage 3.5 跨块回收对齐流水线与字符级 diff，算法零改动。

四阶段 + 跨块回收对齐流水线：
- Stage 0 序列级骨架对齐（最高优先）：将每条条款剥离条款号前缀后按 normalize_zh 正文计算稳定哈希 token，
  用 difflib.SequenceMatcher.get_opcodes() 划分 equal / replace / delete / insert 块级骨架。
- Stage 1 块内/全等锚定：normalize_zh 正文全等时，条款号一致记 unchanged，条款号不同记 renumbered
  （仅条款号顺延，正文一字未改，严禁计入 modified_count、严禁触发暗改告警）。
- Stage 2 块内细配对：严格限制在各 replace 块内部计算综合相似度
  S = 0.30 * 标题Sim + 0.55 * 正文Sim + 0.15 * 2gramJaccard（双方均无标题时改用 0.80 * 正文Sim + 0.20 * 2gramJaccard），
  达到 LOCAL_MATCH_THRESHOLD (0.42) 或「标题全等且正文 >= TITLE_EXACT_BODY_MIN_THRESHOLD (0.25)」记为 modified。
  Stage 2 严禁跨块配对。
- Stage 3 拆合检测：同一块内邻接条款拼接后相似度较单条最高值净提升 >= SPLIT_MERGE_GAIN_MIN (0.15)
  且绝对值 >= SPLIT_MERGE_THRESHOLD (0.60) 时，识别为 split（一拆多）或 merged（多合一）。
- Stage 3.5 跨块移位高阈值回收：对 Stage 0~3 结束后各块仍未配对的剩余 delete 与剩余 insert 条款，
  仅当归一化正文全等或综合相似度 >= CROSS_BLOCK_MOVE_THRESHOLD (0.75) 时才允许跨块回收
  （正文全等记 renumbered/unchanged，有改动记 modified）；低于 0.75 一律禁止跨块配对（防模板化相似条款错配）。
  【已知取舍（架构侧终审注记 B）】：跨块移位且大幅重写的条款（综合相似度落在 0.42~0.75 灰区）
  按 deleted + added 两条降级呈现而非 modified——这是双阈值设计的既定降级行为，两侧全文仍完整可见。
- Stage 4 剩余归类：剩余 V1 记 deleted，剩余 V2 记 added；输出按 V2 主序排列，deleted 按前驱锚点就近插回。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Optional

from core.interfaces import Clause
from core.quote_verifier import normalize_zh
from core.splitter import split_clauses

# ============================================================================
# 对齐算法阈值常量（硬约束 6：定义为模块级常量，便于基准语料校准）
# ============================================================================

# Stage 2 块内贪心配对综合相似度下限：
# 校准方式：基于合成与真实谈判修订语料网格搜索（0.35~0.55），0.42 兼顾局部大幅改写召回与块内异义条款隔离。
LOCAL_MATCH_THRESHOLD: float = 0.42

# Stage 2 块内当双方非空归一化标题完全相等时的正文相似度最低下限：
# 校准方式：同名条款（如“第八条 违约责任”）正文大幅改写时允许低至 0.25 仍配对为 modified。
TITLE_EXACT_BODY_MIN_THRESHOLD: float = 0.25

# Stage 3 拆合检测（一拆多 split / 多合一 merged）拼接后综合相似度最低下限：
# 校准方式：拼接后整体相似度须 >= 0.60 方可认定为同一条款拆合，防止无关短句拼接误判。
SPLIT_MERGE_THRESHOLD: float = 0.60

# Stage 3 拆合检测拼接后较单条最高相似度的最小净提升幅度：
# 校准方式：拼接比单条至少提升 0.15，确保第二/第三子句对整体匹配有实质贡献。
SPLIT_MERGE_GAIN_MIN: float = 0.15

# Stage 3.5 跨块移位回收高阈值下限：
# 校准方式：跨块脱离了位置上下文保护，阈值提升至 0.75（或正文全等），
# 既能回收跨位置移动并小幅润色的条款，又能阻断保密/通知等高相似模板化条款远距离错配。
# 注：跨块移位且大幅重写（0.42 <= S < 0.75 灰区）按既定设计降级为 deleted + added 呈现。
CROSS_BLOCK_MOVE_THRESHOLD: float = 0.75

# 字符级 diff 长条款分句优化阈值：
# 校准方式：单条超过 3000 字符时先做句子级 SequenceMatcher，仅对变化句做字符级 diff，避免 O(N^2) 卡顿。
LONG_CLAUSE_CHAR_LIMIT: int = 3000


# ============================================================================
# 正则与领域特征词库
# ============================================================================

# 剥离行首条款号前缀（如“第一条 ”、“第12条：”、“第三章 ”、“段落1”）
_CLAUSE_NO_PREFIX_RE = re.compile(
    r'^[ \t\u3000]*(?:第\s*[零一二三四五六七八九十百千万0-9]{1,7}\s*[条章节]|段落\s*\d+)[ \t\u3000:：、.．]*'
)

# 标题通用后缀（用于剥离“保密条款”vs“通知条款”中的通用词“条款/义务/责任”，避免共享后缀抬高标题相似度）
_GENERIC_TITLE_SUFFIX_RE = re.compile(
    r'(?:条款|义务|责任|约定|事项|规定|制度|管理|办法|要求|方式|标准|原则|内容|范围)+$'
)

# 模板化条款互斥领域原型（当两条条款分别独占不同领域核心词且无交集时，防止因套话壳子相似而跨条误配）
_TEMPLATE_ARCHETYPES: dict[str, re.Pattern[str]] = {
    'confidentiality': re.compile(r'保密|商业秘密|泄密|机密信息|披露方|接收方|秘密信息'),
    'notice': re.compile(r'通知与送达|送达地址|通讯地址|书面通知|挂号信|特快专递|电子邮箱|传真|收件人|邮寄'),
    'force_majeure': re.compile(r'不可抗力|自然灾害|地震|台风|洪水|战争|罢工|政府行为'),
    'ip': re.compile(r'知识产权|著作权|专利权|商标权|商业标识|专有技术|侵权索赔'),
    'anti_bribery': re.compile(r'廉洁|反商业贿赂|回扣|不正当利益|行贿|受贿|索贿'),
    'dispute': re.compile(r'争议解决|管辖法院|申请仲裁|仲裁委员会|提起诉讼|人民法院'),
}

# 句子切分正则（保留句末标点）
_SENTENCE_SPLIT_RE = re.compile(r'([^。！？；\n]*[。！？；\n]+|[^。！？；\n]+$)')


# ============================================================================
# 核心二：五类关键要素暗改雷达探针（确定性正则，0 Token）
# ============================================================================

# 1. 金额/比例异动（%、元、万、亿、千分之、万分之、大写金额数字等）
_RADAR_AMOUNT_RE = re.compile(
    r'(?:'
    r'\d+(?:\.\d+)?\s*[%％]'
    r'|(?:百分之|千分之|万分之)[零一二三四五六七八九十百千万0-9.]+'
    r'|[¥￥]\s*[0-9,]+(?:\.\d+)?'
    r'|[零一二三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟萬億0-9,]+(?:\.\d+)?\s*(?:万元|亿元|元|角|分|美金|美元)'
    r'|[壹贰叁肆伍陆柒捌玖拾佰仟萬億]+(?:[元圆角分整正])?'
    r')'
)

# 2. 管辖与争议方式（法院、仲裁、管辖、诉讼、所在地等）
_RADAR_JURISDICTION_RE = re.compile(
    r'(?:人民法院|法院|仲裁委员会|仲裁委|仲裁|管辖|诉讼|争议解决|原告所在地|被告所在地|甲方所在地|乙方所在地|合同签订地|合同履行地|住所地)'
)

# 3. 主体反转（甲乙方、委托受托、发包承包、买卖方、披露接收方及首部主体名称替换）
_RADAR_PARTY_RE = re.compile(
    r'(?:甲方|乙方|丙方|委托方|受托方|委托人|受托人|发包人|承包人|发包方|承包方|采购方|供应方|买方|卖方|买受人|出卖人|出租方|承租方|出租人|承租人|披露方|接收方|守约方|违约方)'
)
# 首部主体名称行（如“甲方：XXX公司”）
_HEADER_PARTY_LINE_RE = re.compile(
    r'(?:甲方|乙方|丙方|委托方|受托方|发包人|承包人|买方|卖方)[（(]?[^）)\n]{0,12}[）)]?\s*[:：]\s*([^\n\r，。；;]{2,40})'
)

# 4. 否定与强弱语气（不得/可以/有权/无权/免除/概不/不承担/上限/仅限/连带等）
_RADAR_MODALITY_RE = re.compile(
    r'(?:不得|不可|禁止|严禁|可以|有权|无权|应当|必须|无需|无须|免除|不承担|概不负责|概不承担|概不|不予|仅限|上限|下限|不超过|不低于|全部损失|一切损失|连带责任|连带)'
)

# 5. 期限口径（工作日/自然日/日历日/自动顺延/自动续期/天数/月数/年数）
_RADAR_DEADLINE_RE = re.compile(
    r'(?:'
    r'工作日|自然日|日历日|自动顺延|自动续期|自动展期|有效期|质保期|保证期|履行期限'
    r'|[零一二三四五六七八九十百千万0-9]+\s*(?:个?\s*工作日|个?\s*自然日|个?\s*日历日|日|天|个?\s*月|年|周|小时)'
    r')'
)

_RADAR_PROBES: list[tuple[str, re.Pattern[str]]] = [
    ('金额/比例异动', _RADAR_AMOUNT_RE),
    ('管辖与争议方式', _RADAR_JURISDICTION_RE),
    ('主体反转', _RADAR_PARTY_RE),
    ('否定与强弱语气', _RADAR_MODALITY_RE),
    ('期限口径', _RADAR_DEADLINE_RE),
]


# ============================================================================
# 内部数据结构
# ============================================================================

@dataclass
class _ClauseUnit:
    """预处理后的单条条款内部表示。"""
    idx: int
    no: str
    raw_title: str
    raw_text: str
    start: int
    end: int
    no_norm: str
    title_norm: str
    core_title_norm: str
    body_norm: str          # 剥离条款号后的全文归一化文本（用于 Stage 0 token 与 Stage 1 全等判定）
    pure_body_norm: str     # 进一步剥离标题后的纯正文归一化文本（用于 Stage 2 正文相似度）
    token: str              # Stage 0 骨架对齐稳定哈希 token
    bigrams: frozenset[str] = field(default_factory=frozenset)
    archetypes: frozenset[str] = field(default_factory=frozenset)


def _extract_heading_and_body(clause_no: str, raw_text: str) -> tuple[str, str, str]:
    """从条款原文中分离条款号前缀、真实短标题与纯正文。

    Returns:
        (heading_str, body_without_no, pure_body_without_heading)
    """
    if clause_no in ('首部', '签署栏', '全文') or clause_no.startswith('段落'):
        return '', raw_text, raw_text

    body_without_no = _CLAUSE_NO_PREFIX_RE.sub('', raw_text, count=1).strip()
    if not body_without_no:
        return '', '', ''

    lines = body_without_no.split('\n', 1)
    first_line = lines[0].strip()
    rest_lines = lines[1].strip() if len(lines) > 1 else ''

    # 形态 1：多行条款，首行是短标题（<=24 字且不含句号/分号/感叹号/问号）
    if rest_lines and 1 <= len(first_line) <= 24 and not any(p in first_line for p in '。；！？'):
        heading = first_line.rstrip('：:、.． \t\u3000')
        return heading, body_without_no, rest_lines

    # 形态 2：同行冒号分隔“标题：正文”（如“第一条 保密义务：任何一方...”）
    for sep in ('：', ':'):
        if sep in first_line:
            prefix, after = first_line.split(sep, 1)
            prefix = prefix.strip()
            if (
                1 <= len(prefix) <= 16
                and not any(p in prefix for p in '。；，！？')
                and prefix not in ('甲方', '乙方', '丙方', '委托方', '受托方', '买方', '卖方')
            ):
                pure_body = (after.strip() + ('\n' + rest_lines if rest_lines else '')).strip()
                return prefix, body_without_no, (pure_body or body_without_no)

    # 形态 3：同行空格分隔“标题 正文”（如“第一条 保密义务 任何一方...”）
    m_space = re.match(r'^([^\s:：，。；！？]{2,12})[\s\u3000]+(.+)$', first_line)
    if m_space:
        prefix = m_space.group(1).strip()
        after = m_space.group(2).strip()
        if not any( prefix.startswith(p) for p in ('甲方', '乙方', '丙方', '任何一方', '双方', '本合同') ):
            pure_body = (after + ('\n' + rest_lines if rest_lines else '')).strip()
            return prefix, body_without_no, (pure_body or body_without_no)

    # 形态 4：无独立标题的单段条款（如“第一条 甲方应于30日内付款。”）
    return '', body_without_no, body_without_no


def _make_bigrams(norm_text: str) -> frozenset[str]:
    """生成归一化文本的 2-gram 集合。"""
    if not norm_text:
        return frozenset()
    if len(norm_text) == 1:
        return frozenset([norm_text])
    return frozenset(norm_text[i:i + 2] for i in range(len(norm_text) - 1))


def _detect_archetypes(text: str) -> frozenset[str]:
    """检测条款命中的模板化领域原型集合。"""
    hits = set()
    for name, pat in _TEMPLATE_ARCHETYPES.items():
        if pat.search(text):
            hits.add(name)
    return frozenset(hits)


def _build_clause_units(text: str) -> tuple[list[Clause], list[_ClauseUnit]]:
    """切分合同文本并构造预处理后的 _ClauseUnit 序列。"""
    if not text or not text.strip():
        return [], []
    raw_clauses = split_clauses(text)
    units: list[_ClauseUnit] = []
    for idx, c in enumerate(raw_clauses):
        raw_slice = text[c.start:c.end].strip()
        if not raw_slice and c.no == '全文':
            continue
        heading, body_without_no, pure_body = _extract_heading_and_body(c.no, raw_slice)
        no_norm = normalize_zh(c.no)
        title_norm = normalize_zh(heading)
        core_title_norm = normalize_zh(_GENERIC_TITLE_SUFFIX_RE.sub('', heading)) if heading else ''
        body_norm = normalize_zh(body_without_no)
        pure_body_norm = normalize_zh(pure_body) or body_norm

        # 首部块使用专属前缀避免与正文短句哈希碰撞，其余条款仅按剥离条款号后的归一化正文哈希
        token_seed = f"HEADER:{body_norm}" if c.no == '首部' else f"CLAUSE:{body_norm}"
        token = hashlib.md5(token_seed.encode('utf-8')).hexdigest()

        units.append(
            _ClauseUnit(
                idx=idx,
                no=c.no,
                raw_title=heading or c.title,
                raw_text=raw_slice,
                start=c.start,
                end=c.end,
                no_norm=no_norm,
                title_norm=title_norm,
                core_title_norm=core_title_norm,
                body_norm=body_norm,
                pure_body_norm=pure_body_norm,
                token=token,
                bigrams=_make_bigrams(body_norm),
                archetypes=_detect_archetypes(raw_slice),
            )
        )
    return raw_clauses, units


def _bigram_jaccard(b1: frozenset[str], b2: frozenset[str]) -> float:
    """计算两个 2-gram 集合的 Jaccard 相似度。"""
    if not b1 and not b2:
        return 1.0
    if not b1 or not b2:
        return 0.0
    inter = len(b1 & b2)
    union = len(b1 | b2)
    return inter / union if union > 0 else 0.0


def _seq_ratio(s1: str, s2: str) -> float:
    """计算两个字符串的 SequenceMatcher 相似度（超长文本自动分块防 O(N*M) 单字链爆 CPU）。"""
    if not s1 and not s2:
        return 1.0
    if not s1 or not s2:
        return 0.0
    if s1 == s2:
        return 1.0
    if max(len(s1), len(s2)) > 2000:
        step = 8
        c1 = [s1[i:i + step] for i in range(0, len(s1), step)]
        c2 = [s2[i:i + step] for i in range(0, len(s2), step)]
        return SequenceMatcher(None, c1, c2, autojunk=False).ratio()
    return SequenceMatcher(None, s1, s2, autojunk=False).ratio()


def _is_conflicting_template_clause(u: _ClauseUnit, v: _ClauseUnit, body_sim: float) -> bool:
    """判断两条条款是否属于互斥主题的模板化相似条款（防跨条误配保护）。"""
    # 首部与非首部绝不配对
    if (u.no == '首部') != (v.no == '首部'):
        return True
    # 签署栏与非签署栏绝不配对
    if (u.no == '签署栏') != (v.no == '签署栏'):
        return True

    # 1. 若双方均有明确核心标题，且核心标题完全不相似（如“保密” vs “通知”），除非正文几乎一字不差，否则禁止配对
    if u.core_title_norm and v.core_title_norm:
        core_sim = _seq_ratio(u.core_title_norm, v.core_title_norm)
        if core_sim < 0.25 and body_sim < 0.85:
            return True

    # 2. 若双方各自命中了不同的模板领域原型且无任何交集（如一方纯保密、另一方纯通知送达），除非正文 >=0.85，禁止误配
    if u.archetypes and v.archetypes and u.archetypes.isdisjoint(v.archetypes):
        if body_sim < 0.85:
            return True

    return False


def _compute_clause_similarity(u: _ClauseUnit, v: _ClauseUnit) -> tuple[float, float, bool]:
    """计算两条条款的综合相似度 S。

    公式：
    - 任一方含标题时：S = 0.30 * 标题Sim + 0.55 * 正文Sim + 0.15 * 2gramJaccard
    - 双方均无标题时：S = 0.80 * 正文Sim + 0.20 * 2gramJaccard

    Returns:
        (S, body_sim, is_title_exact_and_body_ok)
    """
    jaccard = _bigram_jaccard(u.bigrams, v.bigrams)
    if (
        max(len(u.pure_body_norm), len(v.pure_body_norm)) > 800
        and jaccard < 0.04
        and (not u.title_norm or u.title_norm != v.title_norm)
    ):
        return 0.0, 0.0, False

    body_sim = _seq_ratio(u.pure_body_norm, v.pure_body_norm)
    if _is_conflicting_template_clause(u, v, body_sim):
        return 0.0, body_sim, False

    if not u.title_norm and not v.title_norm:
        s = 0.80 * body_sim + 0.20 * jaccard
        # 首部与首部同位置块给予保底锚定支持
        if u.no == '首部' and v.no == '首部' and s >= 0.18:
            s = max(s, LOCAL_MATCH_THRESHOLD)
        return s, body_sim, False

    # 使用剥离通用后缀后的核心标题计算相似度，避免“保密条款”与“通知条款”因共享“条款”二字虚高
    if u.core_title_norm and v.core_title_norm:
        title_sim = _seq_ratio(u.core_title_norm, v.core_title_norm)
    else:
        title_sim = _seq_ratio(u.title_norm, v.title_norm)

    s = 0.30 * title_sim + 0.55 * body_sim + 0.15 * jaccard
    title_exact_ok = bool(
        u.title_norm
        and u.title_norm == v.title_norm
        and body_sim >= TITLE_EXACT_BODY_MIN_THRESHOLD
    )
    return s, body_sim, title_exact_ok


def _combine_units(units: list[_ClauseUnit]) -> _ClauseUnit:
    """将相邻多条条款拼接为一个虚拟 _ClauseUnit（供 Stage 3 拆合检测计算整体相似度）。"""
    first = units[0]
    last = units[-1]
    combined_no = ' / '.join(u.no for u in units)
    combined_raw = '\n\n'.join(u.raw_text for u in units)
    combined_title = ''.join(u.title_norm for u in units if u.title_norm)
    combined_core_title = ''.join(u.core_title_norm for u in units if u.core_title_norm)
    combined_body_norm = ''.join(u.body_norm for u in units)
    combined_pure_body_norm = ''.join(u.pure_body_norm for u in units)
    boundary_bgs = {
        units[k].body_norm[-1] + units[k + 1].body_norm[0]
        for k in range(len(units) - 1)
        if units[k].body_norm and units[k + 1].body_norm
    }
    return _ClauseUnit(
        idx=first.idx,
        no=combined_no,
        raw_title=first.raw_title,
        raw_text=combined_raw,
        start=first.start,
        end=last.end,
        no_norm=normalize_zh(combined_no),
        title_norm=combined_title,
        core_title_norm=combined_core_title,
        body_norm=combined_body_norm,
        pure_body_norm=combined_pure_body_norm,
        token='',
        bigrams=frozenset(boundary_bgs).union(*(u.bigrams for u in units)),
        archetypes=frozenset().union(*(u.archetypes for u in units)),
    )


# ============================================================================
# 字符级 Diff 与长条款分句优化
# ============================================================================

def _merge_diff_ops(ops: list[dict[str, str]]) -> list[dict[str, str]]:
    """合并相邻同类型的 diff_op 片段。"""
    merged: list[dict[str, str]] = []
    for item in ops:
        text = item.get('text', '')
        if not text:
            continue
        op = item['op']
        if merged and merged[-1]['op'] == op:
            merged[-1]['text'] += text
        else:
            merged.append({'op': op, 'text': text})
    return merged


def _char_level_diff_short(v1_text: str, v2_text: str) -> list[dict[str, str]]:
    """对短文本直接执行字符级 SequenceMatcher diff。"""
    if v1_text == v2_text:
        return [{'op': 'equal', 'text': v2_text}] if v2_text else []
    matcher = SequenceMatcher(None, v1_text, v2_text, autojunk=False)
    ops: list[dict[str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            ops.append({'op': 'equal', 'text': v2_text[j1:j2]})
        elif tag == 'delete':
            ops.append({'op': 'delete', 'text': v1_text[i1:i2]})
        elif tag == 'insert':
            ops.append({'op': 'insert', 'text': v2_text[j1:j2]})
        elif tag == 'replace':
            ops.append({'op': 'delete', 'text': v1_text[i1:i2]})
            ops.append({'op': 'insert', 'text': v2_text[j1:j2]})
    return _merge_diff_ops(ops)


def compute_char_diff(v1_text: str, v2_text: str) -> list[dict[str, str]]:
    """计算 V1 与 V2 条款文本的字符级 diff_ops。

    当单条长度 > LONG_CLAUSE_CHAR_LIMIT (3000) 时，先按句切分做句子级 diff，
    仅对变化句做字符级 diff，防止超长条款引发界面或 CPU 卡顿。
    """
    if not v1_text and not v2_text:
        return []
    if not v1_text:
        return [{'op': 'insert', 'text': v2_text}]
    if not v2_text:
        return [{'op': 'delete', 'text': v1_text}]
    if v1_text == v2_text:
        return [{'op': 'equal', 'text': v2_text}]

    if max(len(v1_text), len(v2_text)) <= LONG_CLAUSE_CHAR_LIMIT:
        return _char_level_diff_short(v1_text, v2_text)

    # 超长条款：先按句切分，按归一化句子对齐后再对 replace 句块做字符级 diff
    sents1 = [s for s in _SENTENCE_SPLIT_RE.findall(v1_text) if s]
    sents2 = [s for s in _SENTENCE_SPLIT_RE.findall(v2_text) if s]
    norm1 = [normalize_zh(s) for s in sents1]
    norm2 = [normalize_zh(s) for s in sents2]

    matcher = SequenceMatcher(None, norm1, norm2, autojunk=False)
    ops: list[dict[str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            ops.append({'op': 'equal', 'text': ''.join(sents2[j1:j2])})
        elif tag == 'delete':
            ops.append({'op': 'delete', 'text': ''.join(sents1[i1:i2])})
        elif tag == 'insert':
            ops.append({'op': 'insert', 'text': ''.join(sents2[j1:j2])})
        elif tag == 'replace':
            sub1 = ''.join(sents1[i1:i2])
            sub2 = ''.join(sents2[j1:j2])
            if max(len(sub1), len(sub2)) <= LONG_CLAUSE_CHAR_LIMIT:
                ops.extend(_char_level_diff_short(sub1, sub2))
            else:
                ops.append({'op': 'delete', 'text': sub1})
                ops.append({'op': 'insert', 'text': sub2})
    return _merge_diff_ops(ops)


# ============================================================================
# 五类关键要素暗改雷达实现
# ============================================================================

def _extract_changed_windows(diff_ops: list[dict[str, str]], context_chars: int = 10) -> str:
    """从 diff_ops 中提取所有 delete / insert 差异子串及其前后紧邻上下文窗口。

    之所以提取前后约 10 字符上下文，是因为当“20%”改为“50%”或“30日”改为“60日”时，
    diff_ops 中纯 delete/insert 往往只有数字“2”->“5”，单位“%”或“日”落在紧邻的 equal 片段中。
    结合局部窗口即可精准触发金额/比例/期限等探针，同时避免扫描整条未改动部分。
    """
    pieces: list[str] = []
    n = len(diff_ops)
    for i, op_item in enumerate(diff_ops):
        op = op_item.get('op')
        if op not in ('delete', 'insert'):
            continue
        prev_ctx = ''
        next_ctx = ''
        if i > 0 and diff_ops[i - 1].get('op') == 'equal':
            prev_ctx = diff_ops[i - 1].get('text', '')[-context_chars:]
        if i + 1 < n and diff_ops[i + 1].get('op') == 'equal':
            next_ctx = diff_ops[i + 1].get('text', '')[:context_chars]
        pieces.append(f"{prev_ctx}{op_item.get('text', '')}{next_ctx}")
    return '\n'.join(pieces)


def detect_critical_changes(
    diff_type: str,
    v1_text: str,
    v2_text: str,
    diff_ops: list[dict[str, str]],
    clause_no: Optional[str] = None,
) -> tuple[bool, list[str]]:
    """对变更条款运行 5 类确定性暗改探针。

    硬约束 4：unchanged 与 renumbered 严禁触发暗改告警。
    """
    if diff_type in ('unchanged', 'renumbered'):
        return False, []

    if diff_type == 'added':
        probe_text = v2_text
    elif diff_type == 'deleted':
        probe_text = v1_text
    else:
        probe_text = _extract_changed_windows(diff_ops, context_chars=12)

    if not probe_text.strip():
        return False, []

    tags: list[str] = []
    for tag_name, pattern in _RADAR_PROBES:
        if pattern.search(probe_text):
            tags.append(tag_name)

    # 首部专项增强：检测“甲方：A公司 -> 甲方：B公司”这类主体名称替换（即便“甲方：”三字本身在 equal 中）
    if '主体反转' not in tags and (clause_no == '首部' or _HEADER_PARTY_LINE_RE.search(v1_text) or _HEADER_PARTY_LINE_RE.search(v2_text)):
        parties_v1 = [normalize_zh(m.group(1)) for m in _HEADER_PARTY_LINE_RE.finditer(v1_text)]
        parties_v2 = [normalize_zh(m.group(1)) for m in _HEADER_PARTY_LINE_RE.finditer(v2_text)]
        if parties_v1 != parties_v2 and (parties_v1 or parties_v2):
            tags.append('主体反转')

    return bool(tags), tags


# ============================================================================
# 核心一：四阶段 + Stage 3.5 合同条款级对齐主函数
# ============================================================================

def compare_contracts(base_text: str, target_text: str) -> dict[str, Any]:
    """执行 V1 (base_text) 与 V2 (target_text) 四阶段条款级对齐与暗改雷达扫描。

    Returns:
        dict: {
            "summary": {...},
            "items": [ClauseDiffItemDict, ...],
            "alignment_map": dict[str, list[str]],       # "v1:{idx}" -> ["v2:{idx}", ...]
            "clause_no_map": dict[str, list[str]],       # v1_no -> [v2_no, ...]
        }
    """
    _, v1_units = _build_clause_units(base_text or '')
    _, v2_units = _build_clause_units(target_text or '')

    # 记录每条 V1/V2 条款是否已配对
    v1_matched: set[int] = set()
    v2_matched: set[int] = set()

    # 收集所有已配对结果项（在 Stage 4 统一排序并补齐 deleted/added）
    # 结构：dict 包含 v1_indices: list[int], v2_indices: list[int], diff_type: str, similarity: float
    paired_records: list[dict[str, Any]] = []

    # ------------------------------------------------------------------------
    # Stage 0: 序列级骨架对齐（以剥离条款号后的归一化正文哈希作为 token）
    # ------------------------------------------------------------------------
    v1_tokens = [u.token for u in v1_units]
    v2_tokens = [v.token for v in v2_units]
    skeleton_matcher = SequenceMatcher(None, v1_tokens, v2_tokens, autojunk=False)
    opcodes = skeleton_matcher.get_opcodes()

    replace_blocks: list[tuple[int, int, int, int]] = []

    # ------------------------------------------------------------------------
    # Stage 1: 块内/全等锚定（equal 块直接一对一锚定为 unchanged 或 renumbered）
    # ------------------------------------------------------------------------
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == 'equal':
            for offset in range(i2 - i1):
                u = v1_units[i1 + offset]
                v = v2_units[j1 + offset]
                v1_matched.add(u.idx)
                v2_matched.add(v.idx)
                dtype = 'unchanged' if u.no_norm == v.no_norm else 'renumbered'
                paired_records.append({
                    'v1_indices': [u.idx],
                    'v2_indices': [v.idx],
                    'diff_type': dtype,
                    'similarity': 1.0,
                })
        elif tag == 'replace':
            # 在 replace 块内先做一次单调顺序的正文全等子锚定（防止块内夹杂未改动顺延条款）
            sub_i_start = i1
            sub_j_start = j1
            for ui in range(i1, i2):
                u = v1_units[ui]
                if not u.body_norm:
                    continue
                for vj in range(sub_j_start, j2):
                    v = v2_units[vj]
                    if u.body_norm == v.body_norm and (u.no == '首部') == (v.no == '首部'):
                        if ui > sub_i_start or vj > sub_j_start:
                            replace_blocks.append((sub_i_start, ui, sub_j_start, vj))
                        v1_matched.add(u.idx)
                        v2_matched.add(v.idx)
                        dtype = 'unchanged' if u.no_norm == v.no_norm else 'renumbered'
                        paired_records.append({
                            'v1_indices': [u.idx],
                            'v2_indices': [v.idx],
                            'diff_type': dtype,
                            'similarity': 1.0,
                        })
                        sub_i_start = ui + 1
                        sub_j_start = vj + 1
                        break
            if sub_i_start < i2 or sub_j_start < j2:
                replace_blocks.append((sub_i_start, i2, sub_j_start, j2))
        else:
            # delete 或 insert 块留待 Stage 3.5 跨块高阈值回收与 Stage 4 归类
            pass

    # ------------------------------------------------------------------------
    # Stage 2 & Stage 3: 严格在各 replace 子块内部执行细配对与拆合检测（严禁跨块）
    # ------------------------------------------------------------------------
    for bi1, bi2, bj1, bj2 in replace_blocks:
        block_v1 = [v1_units[i] for i in range(bi1, bi2) if i not in v1_matched]
        block_v2 = [v2_units[j] for j in range(bj1, bj2) if j not in v2_matched]
        if not block_v1 or not block_v2:
            continue

        # 计算块内候选对相似度矩阵
        sim_matrix: dict[tuple[int, int], float] = {}
        qualifies_matrix: dict[tuple[int, int], bool] = {}
        candidates: list[tuple[float, int, int]] = []

        for u in block_v1:
            for v in block_v2:
                s, _body_sim, title_exact_ok = _compute_clause_similarity(u, v)
                sim_matrix[(u.idx, v.idx)] = s
                qualifies = (s >= LOCAL_MATCH_THRESHOLD) or title_exact_ok
                qualifies_matrix[(u.idx, v.idx)] = qualifies
                if qualifies:
                    # 排序优先级：综合相似度高优先；同分时相对位置接近优先
                    pos_penalty = abs((u.idx - bi1) - (v.idx - bj1)) * 1e-4
                    candidates.append((s - pos_penalty, u.idx, v.idx))

        candidates.sort(key=lambda x: x[0], reverse=True)

        # Stage 2 贪心一对一配对（记录在 block_pairs 中，以便 Stage 3 检查邻接拆合）
        block_pairs: list[dict[str, Any]] = []
        for _score, u_idx, v_idx in candidates:
            if u_idx in v1_matched or v_idx in v2_matched:
                continue
            v1_matched.add(u_idx)
            v2_matched.add(v_idx)
            block_pairs.append({
                'v1_indices': [u_idx],
                'v2_indices': [v_idx],
                'diff_type': 'modified',
                'similarity': round(sim_matrix[(u_idx, v_idx)], 4),
            })

        # Stage 3: 块内拆合检测（split 一拆多 / merged 多合一）
        # 3a) 先尝试对 Stage 2 已配对的 (u, v_a) 吸收紧邻未配对的 v_b（一拆二/三）或 u_b（二/三合一）
        for pair in block_pairs:
            if pair['diff_type'] != 'modified':
                continue
            u_idx = pair['v1_indices'][0]
            v_idx = pair['v2_indices'][0]
            u_unit = v1_units[u_idx]
            v_unit = v2_units[v_idx]
            base_single_sim = sim_matrix.get((u_idx, v_idx), 0.0)

            # 检查一拆多（u 对应 v_idx 及其后/前紧邻未匹配 V2 条款）
            best_split_indices: Optional[list[int]] = None
            best_split_sim = base_single_sim
            for neighbor_indices in (
                [v_idx, v_idx + 1],
                [v_idx - 1, v_idx],
                [v_idx, v_idx + 1, v_idx + 2],
            ):
                if any(idx < bj1 or idx >= bj2 for idx in neighbor_indices):
                    continue
                extra_indices = [idx for idx in neighbor_indices if idx != v_idx]
                if any(idx in v2_matched for idx in extra_indices):
                    continue
                combo_v = _combine_units([v2_units[idx] for idx in neighbor_indices])
                combo_sim, _, _ = _compute_clause_similarity(u_unit, combo_v)
                max_single = max(sim_matrix.get((u_idx, idx), 0.0) for idx in neighbor_indices)
                if (
                    combo_sim >= SPLIT_MERGE_THRESHOLD
                    and (combo_sim - max_single) >= SPLIT_MERGE_GAIN_MIN
                    and combo_sim > best_split_sim
                ):
                    best_split_sim = combo_sim
                    best_split_indices = neighbor_indices

            if best_split_indices is not None:
                for idx in best_split_indices:
                    v2_matched.add(idx)
                pair['v2_indices'] = best_split_indices
                pair['diff_type'] = 'split'
                pair['similarity'] = round(best_split_sim, 4)
                continue

            # 检查多合一（u_idx 及其后/前紧邻未匹配 V1 条款 合并对应 v_unit）
            best_merge_indices: Optional[list[int]] = None
            best_merge_sim = base_single_sim
            for neighbor_u_indices in (
                [u_idx, u_idx + 1],
                [u_idx - 1, u_idx],
                [u_idx, u_idx + 1, u_idx + 2],
            ):
                if any(idx < bi1 or idx >= bi2 for idx in neighbor_u_indices):
                    continue
                extra_u = [idx for idx in neighbor_u_indices if idx != u_idx]
                if any(idx in v1_matched for idx in extra_u):
                    continue
                combo_u = _combine_units([v1_units[idx] for idx in neighbor_u_indices])
                combo_sim, _, _ = _compute_clause_similarity(combo_u, v_unit)
                max_single = max(sim_matrix.get((idx, v_idx), 0.0) for idx in neighbor_u_indices)
                if (
                    combo_sim >= SPLIT_MERGE_THRESHOLD
                    and (combo_sim - max_single) >= SPLIT_MERGE_GAIN_MIN
                    and combo_sim > best_merge_sim
                ):
                    best_merge_sim = combo_sim
                    best_merge_indices = neighbor_u_indices

            if best_merge_indices is not None:
                for idx in best_merge_indices:
                    v1_matched.add(idx)
                pair['v1_indices'] = best_merge_indices
                pair['diff_type'] = 'merged'
                pair['similarity'] = round(best_merge_sim, 4)

        # 3b) 对块内完全未配对的邻接条款继续检查 split / merged
        unmatched_u = [i for i in range(bi1, bi2) if i not in v1_matched]
        unmatched_v = [j for j in range(bj1, bj2) if j not in v2_matched]

        # 未匹配 u 一拆二
        for u_idx in list(unmatched_u):
            if u_idx in v1_matched:
                continue
            u_unit = v1_units[u_idx]
            for j in range(bj1, bj2 - 1):
                if j in v2_matched or (j + 1) in v2_matched:
                    continue
                combo_v = _combine_units([v2_units[j], v2_units[j + 1]])
                combo_sim, _, _ = _compute_clause_similarity(u_unit, combo_v)
                max_single = max(
                    sim_matrix.get((u_idx, j), 0.0),
                    sim_matrix.get((u_idx, j + 1), 0.0),
                )
                if combo_sim >= SPLIT_MERGE_THRESHOLD and (combo_sim - max_single) >= SPLIT_MERGE_GAIN_MIN:
                    v1_matched.add(u_idx)
                    v2_matched.add(j)
                    v2_matched.add(j + 1)
                    block_pairs.append({
                        'v1_indices': [u_idx],
                        'v2_indices': [j, j + 1],
                        'diff_type': 'split',
                        'similarity': round(combo_sim, 4),
                    })
                    break

        # 未匹配 u 二合一
        for v_idx in list(unmatched_v):
            if v_idx in v2_matched:
                continue
            v_unit = v2_units[v_idx]
            for i in range(bi1, bi2 - 1):
                if i in v1_matched or (i + 1) in v1_matched:
                    continue
                combo_u = _combine_units([v1_units[i], v1_units[i + 1]])
                combo_sim, _, _ = _compute_clause_similarity(combo_u, v_unit)
                max_single = max(
                    sim_matrix.get((i, v_idx), 0.0),
                    sim_matrix.get((i + 1, v_idx), 0.0),
                )
                if combo_sim >= SPLIT_MERGE_THRESHOLD and (combo_sim - max_single) >= SPLIT_MERGE_GAIN_MIN:
                    v1_matched.add(i)
                    v1_matched.add(i + 1)
                    v2_matched.add(v_idx)
                    block_pairs.append({
                        'v1_indices': [i, i + 1],
                        'v2_indices': [v_idx],
                        'diff_type': 'merged',
                        'similarity': round(combo_sim, 4),
                    })
                    break

        paired_records.extend(block_pairs)

    # ------------------------------------------------------------------------
    # Stage 3.5: 跨块移位高阈值回收（仅对剩余未配对的 delete 与 insert 项）
    # 仅当归一化正文完全相等 或 综合相似度 >= CROSS_BLOCK_MOVE_THRESHOLD (0.75) 时回收。
    # 已知取舍（终审注记 B）：跨块移位且大幅重写（0.42~0.75 灰区）不在此处回收，
    # 由 Stage 4 降级为 deleted + added 呈现，两侧全文完整可见。
    # ------------------------------------------------------------------------
    rem_v1 = [u for u in v1_units if u.idx not in v1_matched]
    rem_v2 = [v for v in v2_units if v.idx not in v2_matched]

    if rem_v1 and rem_v2:
        cross_candidates: list[tuple[float, bool, int, int]] = []
        for u in rem_v1:
            for v in rem_v2:
                if (u.no == '首部') != (v.no == '首部'):
                    continue
                if u.body_norm and u.body_norm == v.body_norm:
                    cross_candidates.append((1.0, True, u.idx, v.idx))
                else:
                    s, _, _ = _compute_clause_similarity(u, v)
                    if s >= CROSS_BLOCK_MOVE_THRESHOLD:
                        cross_candidates.append((s, False, u.idx, v.idx))

        cross_candidates.sort(key=lambda x: x[0], reverse=True)
        for sim_val, is_exact, u_idx, v_idx in cross_candidates:
            if u_idx in v1_matched or v_idx in v2_matched:
                continue
            v1_matched.add(u_idx)
            v2_matched.add(v_idx)
            u_unit = v1_units[u_idx]
            v_unit = v2_units[v_idx]
            if is_exact:
                dtype = 'unchanged' if u_unit.no_norm == v_unit.no_norm else 'renumbered'
            else:
                dtype = 'modified'
            paired_records.append({
                'v1_indices': [u_idx],
                'v2_indices': [v_idx],
                'diff_type': dtype,
                'similarity': round(sim_val, 4),
            })

    # ------------------------------------------------------------------------
    # Stage 4: 剩余归类（仅 V1 -> deleted，仅 V2 -> added）与 V2 主序排列
    # ------------------------------------------------------------------------
    # 建立 v2 主序索引桶：先按每个含有 v2_indices 的记录的最小 v2_idx 排序
    v2_anchored_records: list[dict[str, Any]] = list(paired_records)
    for v in v2_units:
        if v.idx not in v2_matched:
            v2_anchored_records.append({
                'v1_indices': [],
                'v2_indices': [v.idx],
                'diff_type': 'added',
                'similarity': 0.0,
            })

    v2_anchored_records.sort(key=lambda r: r['v2_indices'][0])

    # 建立 v1_idx -> 所在 v2_anchored_records 的位置映射，供 deleted 按前驱锚点就近插回
    v1_to_record_pos: dict[int, int] = {}
    for pos, rec in enumerate(v2_anchored_records):
        for u_idx in rec['v1_indices']:
            v1_to_record_pos[u_idx] = pos

    # 收集 deleted 记录并挂靠到前驱锚点位置之后（无前驱则挂在 -1 即最顶部）
    deleted_WaitBuckets: dict[int, list[dict[str, Any]]] = {}
    for u in v1_units:
        if u.idx not in v1_matched:
            anchor_pos = -1
            for prev_i in range(u.idx - 1, -1, -1):
                if prev_i in v1_to_record_pos:
                    anchor_pos = v1_to_record_pos[prev_i]
                    break
            deleted_WaitBuckets.setdefault(anchor_pos, []).append({
                'v1_indices': [u.idx],
                'v2_indices': [],
                'diff_type': 'deleted',
                'similarity': 0.0,
            })

    ordered_records: list[dict[str, Any]] = []
    if -1 in deleted_WaitBuckets:
        ordered_records.extend(deleted_WaitBuckets[-1])
    for pos, rec in enumerate(v2_anchored_records):
        ordered_records.append(rec)
        if pos in deleted_WaitBuckets:
            ordered_records.extend(deleted_WaitBuckets[pos])

    # ------------------------------------------------------------------------
    # 生成输出 items、字符级 diff_ops、暗改雷达标签与索引对齐映射表
    # ------------------------------------------------------------------------
    items: list[dict[str, Any]] = []
    alignment_map: dict[str, list[str]] = {f"v1:{u.idx}": [] for u in v1_units}
    clause_no_map: dict[str, list[str]] = {}

    unchanged_count = 0
    renumbered_count = 0
    modified_count = 0
    split_count = 0
    merged_count = 0
    added_count = 0
    deleted_count = 0
    critical_change_count = 0

    for item_id, rec in enumerate(ordered_records, start=1):
        u_idxs: list[int] = rec['v1_indices']
        v_idxs: list[int] = rec['v2_indices']
        dtype: str = rec['diff_type']
        sim_val: float = rec['similarity']

        v1_no = ' / '.join(v1_units[i].no for i in u_idxs) if u_idxs else None
        v2_no = ' / '.join(v2_units[j].no for j in v_idxs) if v_idxs else None
        v1_title = ' / '.join(v1_units[i].raw_title for i in u_idxs if v1_units[i].raw_title) if u_idxs else ''
        v2_title = ' / '.join(v2_units[j].raw_title for j in v_idxs if v2_units[j].raw_title) if v_idxs else ''
        v1_text = '\n\n'.join(v1_units[i].raw_text for i in u_idxs) if u_idxs else ''
        v2_text = '\n\n'.join(v2_units[j].raw_text for j in v_idxs) if v_idxs else ''

        # 维护序列索引对齐映射（终审注记 A："v1:3" -> ["v2:5"]）与条款号映射
        for ui in u_idxs:
            v1_key = f"v1:{ui}"
            for vj in v_idxs:
                v2_key = f"v2:{vj}"
                if v2_key not in alignment_map[v1_key]:
                    alignment_map[v1_key].append(v2_key)
            u_no_str = v1_units[ui].no
            clause_no_map.setdefault(u_no_str, [])
            for vj in v_idxs:
                v_no_str = v2_units[vj].no
                if v_no_str not in clause_no_map[u_no_str]:
                    clause_no_map[u_no_str].append(v_no_str)

        # 计算字符级 diff_ops
        if dtype in ('unchanged', 'renumbered'):
            diff_ops = [{'op': 'equal', 'text': v2_text}] if v2_text else []
        elif dtype == 'added':
            diff_ops = [{'op': 'insert', 'text': v2_text}] if v2_text else []
        elif dtype == 'deleted':
            diff_ops = [{'op': 'delete', 'text': v1_text}] if v1_text else []
        else:
            diff_ops = compute_char_diff(v1_text, v2_text)

        # 运行 5 类暗改探针（unchanged / renumbered 恒为 False, []）
        is_crit, crit_tags = detect_critical_changes(
            diff_type=dtype,
            v1_text=v1_text,
            v2_text=v2_text,
            diff_ops=diff_ops,
            clause_no=v2_no or v1_no,
        )

        # 统计计数（硬约束 4：renumbered 绝不计入 modified_count、绝不触发暗改）
        if dtype == 'unchanged':
            unchanged_count += 1
        elif dtype == 'renumbered':
            renumbered_count += 1
        elif dtype == 'modified':
            modified_count += 1
        elif dtype == 'split':
            split_count += 1
            modified_count += 1
        elif dtype == 'merged':
            merged_count += 1
            modified_count += 1
        elif dtype == 'added':
            added_count += 1
        elif dtype == 'deleted':
            deleted_count += 1

        if is_crit:
            critical_change_count += 1

        items.append({
            'item_id': item_id,
            'diff_type': dtype,
            'v1_clause_no': v1_no,
            'v2_clause_no': v2_no,
            'v1_title': v1_title,
            'v2_title': v2_title,
            'v1_text': v1_text,
            'v2_text': v2_text,
            'v1_indices': [f"v1:{i}" for i in u_idxs],
            'v2_indices': [f"v2:{j}" for j in v_idxs],
            'similarity': sim_val,
            'diff_ops': diff_ops,
            'is_critical_change': is_crit,
            'critical_tags': crit_tags,
        })

    # 整体文本重合度（基于全文归一化文本 SequenceMatcher）
    norm_full_v1 = normalize_zh(base_text or '')
    norm_full_v2 = normalize_zh(target_text or '')
    if not norm_full_v1 and not norm_full_v2:
        overall_similarity = 1.0
    elif not norm_full_v1 or not norm_full_v2:
        overall_similarity = 0.0
    else:
        overall_similarity = round(_seq_ratio(norm_full_v1, norm_full_v2), 4)

    summary = {
        'total_v1_clauses': len(v1_units),
        'total_v2_clauses': len(v2_units),
        'total_items': len(items),
        'overall_similarity': overall_similarity,
        'unchanged_count': unchanged_count,
        'renumbered_count': renumbered_count,
        'modified_count': modified_count,
        'split_count': split_count,
        'merged_count': merged_count,
        'added_count': added_count,
        'deleted_count': deleted_count,
        'critical_change_count': critical_change_count,
    }

    return {
        'summary': summary,
        'items': items,
        'alignment_map': alignment_map,
        'clause_no_map': clause_no_map,
    }
