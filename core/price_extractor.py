# -*- coding: utf-8 -*-
"""F2-2 报价表提取辅助层 —— 从招标/投标文档定位并结构化提取报价数据。

设计要点（只读复用 parser.py 与 price_sandbox.py）：
1. **输出契约严格对齐 PriceSandbox**：
   {
       'status': 'OK' | 'NEED_MANUAL',
       'reason': str,
       'items': [{'name': str, 'qty': str, 'unit_price': str, 'total_price': str}, ...],
       'table_total': str | None,      # 分项报价表合计（单位：元）
       'bid_total': str | None,        # 开标一览表总报价小写（单位：元）
       'bid_total_upper': str | None,  # 开标一览表总报价大写原文
       'price_cap': str | None,        # 招标文件最高限价/预算金额（单位：元）
       'has_price_table_skeleton': bool,
   }
2. **失败模式（宁缺毋滥）**：
   - 当文档为招标文件空白报价模板（有表头无有效报价数字）、合并单元格异形无法对齐列、
     或未定位到投标报价时，返回 status='NEED_MANUAL'，供上层 API 触发前端人工框选回填。
3. **单位自动归一（万元 → 元）**：
   - 自动识别正文及表头/单元格中的"万元"单位，统一按 Decimal * 10000 折算为"元"。
4. **vMerge 合并单元格去重与合计行隔离**：
   - 参考 parser.py 的 _tc 实体去重机制，按 (名称格_tc, 合价格_tc) 组合去重防止跨行合并虚增；
   - 严格隔离"合计/总计/小计/大写"汇总行，提取为 table_total，严禁混入 items 明细。
5. **E1 升级点 4b（江苏政采模板跨格与分包预算提取）**：
   - 依据《政府采购法实施条例》第 33 条，未单独设定最高限价时以采购预算作为法定上限；
   - 支持表格横向跨格（标签格与数值格中间隔分包格/空串）、纵向跨行（表头列为预算金额、数据行填数值）
     以及正文跨换行/分包小节的预算提取。
6. **E3 修复 1（docx 表格与 PDF 文本双路径空白格式表/行号序列防线）**：
   - 严格拦截数量/单价/合价为 1/2/3 等连续递增小整数序列、含省略号占位符或表头词黑名单的伪报价行。
7. **E7 开标一览表提取鲁棒性（限价/报价同行防误判与多源分级核验）**：
   - 数字与大写分支统一按格剔除含限价/预算/保证金语境词的单元格（及同格内的限价分句）；
   - 同行值区出现 >=2 个不同金额且无语境词消歧时，严守宁缺毋滥，不赋值 bid_total（置 None）并产出 warning 级 finding；
   - 独立《开标一览表》作为唯一主赋值源，商务自洽表/应答表降级为交叉核验源（不一致产出 warning 级 finding）。
8. **E8 多明细表小计聚合与分项汇总表识别**：
   - 按表分组收集明细表的小计/合计行：多张分表明细表各带"小计"行时自动执行 Decimal 累加（table_total = Σ各分表小计），
     同表内既有小计又有合计时合计优先，存在全量明细表（含非"小计"的合计行且含多个分项行）时以其为准；
   - 识别无单价/数量列但含"分项名称+金额+合计行"的《分项汇总表》作为 table_total 最高优先级权威源，
     与 Σ各分表小计交叉核验（不一致产出 warning 级 finding 而非 error），并以表元素引用 _tbl 去重严防双重计数。
"""
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from core.parser import parse_document
from core.price_sandbox import PriceSandbox

CENT = Decimal('0.01')
WAN = Decimal('10000')

# ── 正则模式定义 ──────────────────────────────────────────────────────────

# 最高限价 / 控制价（高优先级）与 预算金额 / 采购预算（次优先级）
_CAP_HIGH_RE = re.compile(
    r'(?:最高(?:投标)?限价|招标控制价|控制价|拦标价|最高限额)'
    r'[^0-9\n]{0,45}?'
    r'([0-9][0-9,]*(?:\.[0-9]+)?)\s*(万元|万|元)',
    re.IGNORECASE
)
_CAP_UNIT_HEADER_RE = re.compile(
    r'(?:最高(?:投标)?限价|招标控制价|预算金额|采购预算)\s*[（(]\s*(万元|元)\s*[)）]'
    # E1 验收修正：(?<![包号]) 防跨行 filler 吃进"采购包1"式包号尾巴——
    # 数字若紧跟"包/号"字（如"采购包1"的 1）则不是金额，正则回溯继续找真值
    r'[\s\S]{0,40}?(?<![包号])([0-9][0-9,]*(?:\.[0-9]+)?)',
    re.IGNORECASE
)
_CAP_BUDGET_RE = re.compile(
    r'(?:采购预算|预算金额|项目预算|预算总价|最高预算)'
    r'[^0-9\n]{0,45}?'
    r'([0-9][0-9,]*(?:\.[0-9]+)?)\s*(万元|万|元)',
    re.IGNORECASE
)

# E1 升级点 4b：支持跨换行（docx 表格展平后标签格与数值格以 \n 分隔）及中间夹带分包/标段编号的限价与预算匹配
_CAP_HIGH_MULTILINE_RE = re.compile(
    r'(?:最高(?:投标)?限价|招标控制价|最高限额)'
    r'(?:[\s:：—\-]|人民币|小写|(?:采购)?包\s*\d+|分包\s*[0-9一二三四五六七八九十]+|标段\s*[0-9一二三四五六七八九十]+){0,30}?'
    r'([0-9][0-9,]*(?:\.[0-9]+)?)\s*(万元|万|元)',
    re.IGNORECASE
)
_CAP_BUDGET_MULTILINE_RE = re.compile(
    r'(?:采购预算|预算金额|项目预算|预算总价|最高预算|本项目预算)'
    r'(?:[\s:：—\-、，]|人民币|小写|为|共计|合计|(?:采购)?包\s*\d+|分包\s*[0-9一二三四五六七八九十]+|标段\s*[0-9一二三四五六七八九十]+){0,35}?'
    r'([0-9][0-9,]*(?:\.[0-9]+)?)\s*(万元|万|元)',
    re.IGNORECASE
)
# 分包小节预算匹配（如 "分包1：采购预算 80万元" / "采购包1 预算金额：120万元"）
_PACKAGE_BUDGET_RE = re.compile(
    r'(?:采购包|分包|标段)\s*([0-9一二三四五六七八九十]{1,3})'
    r'[^\n。；]{0,40}?'
    r'(?:最高(?:投标)?限价|采购预算|预算金额|项目预算|预算)'
    r'[^0-9\n]{0,20}?'
    r'([0-9][0-9,]*(?:\.[0-9]+)?)\s*(万元|万|元)',
    re.IGNORECASE
)

# 人民币大写金额匹配（至少含两个大写数字词 + 元/圆/角/分）
_UPPER_AMOUNT_RE = re.compile(
    r'((?:人民币)?[（(]?大写[）)]?[：:\s]*)?'
    r'([零壹贰叁肆伍陆柒捌玖拾佰仟万亿]{2,}(?:元|圆)(?:整|正|[零壹贰叁肆伍陆柒捌玖角分]+)?)'
)

# 开标一览表总价文本行匹配（要求明确冒号与货币单位，防止误匹配"投标报价×30分"等评分公式）
_BID_TOTAL_LINE_RE = re.compile(
    r'(?:投标总报价|投标总价|报价总计|总报价|投标报价)'
    r'\s*(?:[（(][^）)\n]{0,16}[）)])?\s*[：:]\s*'
    r'(?:小写[：:\s]*|人民币\s*|[￥¥]\s*)?'
    r'([0-9][0-9,]*(?:\.[0-9]+)?)\s*(万元|元)'
)

# 自由文本中带单位金额全局扫描正则（供单格/同行多金额歧义检测）
_MONEY_WITH_UNIT_SCAN_RE = re.compile(
    r'(?:[￥¥]\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(万元|万|元)?)'
    r'|(?<![0-9.])([0-9][0-9,]*(?:\.[0-9]+)?)\s*(万元|万|元)'
)

# 表头列角色关键词
_COL_NAME_KEYS = ('品目名称', '货物名称', '服务名称', '分项名称', '项目名称', '系统名称',
                  '模块名称', '平台名称', '子系统名称', '设备名称', '标的名称', '标的物',
                  '名称', '品目', '内容', '分项', '项目')
_COL_QTY_KEYS = ('数量', '采购数量', '投标数量', '工作量', '套数', '台数')
_COL_UNIT_PRICE_KEYS = ('单价', '投标单价', '含税单价', '综合单价', '全费用单价')
_COL_TOTAL_PRICE_KEYS = ('合价', '分项合计', '分项总价', '投标合价', '含税总价',
                         '含税合价', '分项报价', '小计', '总价', '金额', '费用')

# 限价/预算表头或标签关键词（高优先级：最高限价/控制价；法定等同级：采购预算/预算金额）
_CAP_LABEL_HIGH_KEYS = ('最高限价', '最高投标限价', '招标控制价', '控制价', '最高限额')
_CAP_LABEL_BUDGET_KEYS = ('采购预算', '预算金额', '项目预算', '预算总价')
_CAP_LABEL_ALL_KEYS = _CAP_LABEL_HIGH_KEYS + _CAP_LABEL_BUDGET_KEYS

# 投标总价标签关键词
_BID_TOTAL_LABEL_KEYS = ('投标总价', '投标总报价', '报价总计', '总报价', '投标报价')

# 汇总行识别（区分小计行与合计/总计行）
_SUMMARY_ROW_RE = re.compile(
    r'^\s*(?:合\s*计|总\s*计|报价合计|投标总价|投标总报价|总报价|分项合计|本表合计|汇总合计|小\s*计|本表小计|分项小计|总金额|大\s*写)',
    re.IGNORECASE
)
_SUBTOTAL_ROW_RE = re.compile(
    r'^\s*(?:小\s*计|本表小计|分项小计|分包小计|模块小计|平台小计)',
    re.IGNORECASE
)
_GRAND_TOTAL_ROW_RE = re.compile(
    r'^\s*(?:合\s*计|总\s*计|报价合计|投标总价|投标总报价|总报价|分项合计|本表合计|汇总合计|总金额)',
    re.IGNORECASE
)

# 纯序号判定
_SEQ_ONLY_RE = re.compile(r'^\s*(?:[0-9]{1,3}|[一二三四五六七八九十]+|[（(][0-9一二三四五六七八九十]+[)）])\s*$')
# 说明性长句排除词（防把招标须知/评分说明当成报价数值）
_INSTRUCTION_NOISE_KEYS = ('不得超过', '不得高于', '不高于', '不超过', '超过最高限价',
                           '无效标', '废标', '投标无效', '得分', '评标基准价',
                           '计算公式', '保留两位小数', '满分为', '扣完为止', '保证金')
# E7 扩充：非投标报价的数字与大写上下文排除词（防把最高限价/预算/控制价/保证金的数字或大写误提为 bid_total / bid_total_upper）
_NON_BID_UPPER_KEYS = (
    '最高限价', '最高投标限价', '采购预算', '预算金额', '项目预算', '预算总价', '最高预算',
    '招标控制价', '控制价', '拦标价', '最高限额', '保证金',
    '不得超过', '不得高于', '不高于', '不超过', '超过最高限价',
    '无效标', '废标', '投标无效', '否决投标',
)

# 自洽表 / 商务应答表表头特征词（用于区分独立《开标一览表》与自洽核验表）
_SELF_CHECK_HEADER_KEYS = (
    '招标文件要求', '招标要求', '承诺要点', '响应承诺', '投标响应',
    '响应情况', '偏离情况', '偏离说明', '核验内容', '自洽', '符合性',
)

# 空白格式表防线：表头词黑名单（在 docx 表格路径与 PDF 文本路径共同生效）
_HEADER_NAME_BLACKLIST = frozenset({
    '金额（元）', '金额(元)', '单价（元）', '单价(元)', '总价（元）', '总价(元)',
    '合价（元）', '合价(元)', '小计（元）', '小计(元)',
    '数量', '备注', '分项价格', '合计价', '单价', '合价', '金额', '总价',
    '证书有效期', '服务费用报价依据', '分项名称', '品目名称', '货物名称', '服务名称',
    '项目名称', '证书名称', '发证单位', '证书等级', '从事专业', '技术参数',
    '响应情况', '投标文件响应部分', '偏离说明', '商务要求', '（页码）', '(页码)',
    '页码', '品牌', '规格型号', '产地', '厂家', '单位', '序号',
    '符合或偏离', '主要技术参数及性能描述',
})

_PLACEHOLDER_CELL_TOKENS = ('/', '-', '—', '--', '…', '...', '……', '无', '详见', '投标人填报', '待填')


def _fmt_yuan(val: Decimal) -> str:
    return str(val.quantize(CENT, rounding=ROUND_HALF_UP))


def _is_bogus_price_row(
    item_name: str,
    qty_raw: str,
    unit_raw: str,
    total_raw: str,
    qty_val: Decimal,
    unit_val: Decimal,
    total_val: Decimal,
) -> bool:
    """E3 修复 1 核心守卫：判断候选分项行是否为空白模板行号序列（如 1/2/3）、列号指引行或表头黑名单。"""
    clean_name = re.sub(r'\s+', '', item_name or '')
    if not clean_name:
        return True
    if '…' in clean_name or '...' in clean_name:
        return True
    if clean_name in _HEADER_NAME_BLACKLIST or any(k == clean_name for k in _HEADER_NAME_BLACKLIST):
        return True
    if any(k in clean_name for k in ('报价依据', '证书有效期', '单价（元）', '金额（元）', '合价（元）')):
        return True

    for r_txt in (qty_raw, unit_raw, total_raw):
        s_txt = (r_txt or '').strip()
        if '…' in s_txt or '...' in s_txt:
            return True

    is_int_qty = (qty_val == int(qty_val))
    is_int_unit = (unit_val == int(unit_val))
    is_int_total = (total_val == int(total_val))

    if is_int_qty and is_int_unit and is_int_total:
        iq = int(qty_val)
        iu = int(unit_val)
        it = int(total_val)
        if 1 <= iq <= 50 and iu == iq + 1 and it == iu + 1:
            return True
        if 1 <= iq <= 10 and 1 <= iu <= 10 and 1 <= it <= 10 and (iq * iu != it):
            return True

    return False


def _parse_money_str(raw: str, default_wan: bool = False,
                     require_unit: bool = False) -> Decimal | None:
    """从单元格或文本片段解析金额并统一转为元；非有效数字返回 None。"""
    if not raw:
        return None
    s = str(raw).strip()
    if not s or s in _PLACEHOLDER_CELL_TOKENS or '…' in s or '...' in s:
        return None
    if any(k in s for k in _INSTRUCTION_NOISE_KEYS):
        return None
    # 限价语境排除不在纯解析层做（E7 验收修正）：_parse_money_str 是无语境语义的
    # 纯值解析器，限价/预算上下文拒绝统一由调用点 _has_non_bid_context/
    # _strip_non_bid_clauses/_is_tender_requirement_column_header 负责。

    is_wan = default_wan
    if '万元' in s or re.search(r'[0-9]\s*万(?![0-9])', s):
        is_wan = True
    elif '元' in s and '万元' not in s:
        is_wan = False

    if require_unit:
        m = (re.search(r'([0-9][0-9,]*(?:\.[0-9]+)?)\s*(万元|万|元)', s)
             or re.search(r'[￥¥]\s*([0-9][0-9,]*(?:\.[0-9]+)?)', s))
        if not m:
            return None
        num_str = m.group(1).replace(',', '')
        if '万元' in m.group(0) or '万' in (m.group(2) if m.lastindex and m.lastindex > 1 else ''):
            is_wan = True
        elif '元' in m.group(0):
            is_wan = False
    else:
        m = re.search(r'([0-9][0-9,]*(?:\.[0-9]+)?)', s)
        if not m:
            return None
        num_str = m.group(1).replace(',', '')
    try:
        d = Decimal(num_str)
    except InvalidOperation:
        return None
    if d < 0:
        return None
    if is_wan:
        d = d * WAN
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def _parse_cap_money_str(raw: str, default_wan: bool = False,
                         require_unit: bool = False) -> Decimal | None:
    """专用于最高限价/预算单元格的金额解析（允许格内含'最高限价/不得高于'等限价词，但排除评分公式噪声）。"""
    if not raw:
        return None
    s = str(raw).strip()
    if not s or s in _PLACEHOLDER_CELL_TOKENS or '…' in s or '...' in s:
        return None
    if any(k in s for k in ('得分', '评标基准价', '计算公式', '保留两位小数', '满分为', '扣完为止', '保证金')):
        return None

    is_wan = default_wan
    if '万元' in s or re.search(r'[0-9]\s*万(?![0-9])', s):
        is_wan = True
    elif '元' in s and '万元' not in s:
        is_wan = False

    if require_unit:
        m = (re.search(r'([0-9][0-9,]*(?:\.[0-9]+)?)\s*(万元|万|元)', s)
             or re.search(r'[￥¥]\s*([0-9][0-9,]*(?:\.[0-9]+)?)', s))
        if not m:
            return None
        num_str = m.group(1).replace(',', '')
        if '万元' in m.group(0) or '万' in (m.group(2) if m.lastindex and m.lastindex > 1 else ''):
            is_wan = True
        elif '元' in m.group(0):
            is_wan = False
    else:
        m = re.search(r'([0-9][0-9,]*(?:\.[0-9]+)?)', s)
        if not m:
            return None
        num_str = m.group(1).replace(',', '')
    try:
        d = Decimal(num_str)
    except InvalidOperation:
        return None
    if d < 0:
        return None
    if is_wan:
        d = d * WAN
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def _parse_qty_str(raw: str) -> Decimal | None:
    """解析数量单元格（兼容 '2', '2台', '1项', '3.5套' 等写法）。"""
    if not raw:
        return None
    s = str(raw).strip().replace(',', '')
    if '…' in s or '...' in s:
        return None
    m = re.match(r'^([0-9]+(?:\.[0-9]+)?)\s*[项套台个批年人月次份座宗件套]?\s*$', s)
    if not m:
        return None
    try:
        d = Decimal(m.group(1))
        return d if d > 0 else None
    except InvalidOperation:
        return None


def extract_price_cap(text: str) -> str | None:
    """从招标/投标文件全文中提取最高限价或采购预算（统一折算为元）。

    依据《政府采购法实施条例》第 33 条：未单独设定最高限价时，以采购预算作为法定上限。
    优先级：最高限价 / 控制价（同行 -> 跨行） > 表头带单位限价/预算 > 采购预算 / 预算金额（同行 -> 跨行 -> 分包）。
    """
    if not text:
        return None

    for regex in (
        _CAP_HIGH_RE,
        _CAP_HIGH_MULTILINE_RE,
        _CAP_UNIT_HEADER_RE,
        _CAP_BUDGET_RE,
        _CAP_BUDGET_MULTILINE_RE,
    ):
        for m in regex.finditer(text):
            g1, g2 = m.group(1), m.group(2)
            if g1 in ('万元', '万', '元'):
                unit, num_s = g1, g2
            else:
                num_s, unit = g1, g2
            if num_s not in m.group(0):
                continue
            try:
                val = Decimal(num_s.replace(',', ''))
            except InvalidOperation:
                continue
            if val <= 0:
                continue
            if '万' in unit:
                val = val * WAN
            if val >= Decimal('100'):
                return _fmt_yuan(val)

    pkg_matches = list(_PACKAGE_BUDGET_RE.finditer(text))
    if pkg_matches:
        for m in pkg_matches:
            num_s, unit = m.group(2), m.group(3)
            if num_s not in m.group(0):
                continue
            try:
                val = Decimal(num_s.replace(',', ''))
            except InvalidOperation:
                continue
            if '万' in unit:
                val = val * WAN
            if val >= Decimal('100'):
                return _fmt_yuan(val)

    return None


def _detect_header_map(cells_text: list[str]) -> dict | None:
    """判断一行是否为分项报价明细表头（含名称+总价+(单价或数量)），
    若是则返回 {'name': idx, 'qty': idx, 'unit': idx, 'total': idx, 'is_wan': bool}。
    """
    joined = ' '.join(cells_text)
    is_wan = ('万元' in joined)
    col_map = {}

    for idx, raw in enumerate(cells_text):
        c = re.sub(r'\s+', '', raw)
        if not c or len(c) > 20 or c in ('序号', '编号', '行号', '包号'):
            continue
        if '单价' in c and 'unit' not in col_map:
            col_map['unit'] = idx
            continue
        if any(k in c for k in _COL_QTY_KEYS) and 'qty' not in col_map:
            col_map['qty'] = idx
            continue
        if any(k in c for k in _COL_TOTAL_PRICE_KEYS) and 'total' not in col_map:
            col_map['total'] = idx
            continue
        if any(k in c for k in _COL_NAME_KEYS) and 'name' not in col_map:
            col_map['name'] = idx

    if 'name' in col_map and 'total' in col_map and ('unit' in col_map or 'qty' in col_map):
        col_map['is_wan'] = is_wan
        return col_map
    return None


def _detect_summary_table_header_map(cells_text: list[str]) -> dict | None:
    """E8 §3.2：判断一行是否为《分项汇总表》表头（无单价/数量列，但含分项名称列 + 金额/合价列）。
    排除含"招标文件要求/承诺要点/偏离"的自洽表及"投标总报价"开标一览表。
    """
    cleaned_cells = [re.sub(r'\s+', '', raw) for raw in cells_text]
    joined = ' '.join(cleaned_cells)
    if any(k in joined for k in _SELF_CHECK_HEADER_KEYS):
        return None
    if any(k in joined for k in ('分值', '得分', '满分', '评分标准', '评审因素')):
        return None

    is_wan = ('万元' in joined)
    col_map = {}
    for idx, c in enumerate(cleaned_cells):
        if not c or len(c) > 22 or c in ('序号', '编号', '行号', '包号', '备注', '说明'):
            continue
        if '单价' in c or any(k in c for k in _COL_QTY_KEYS):
            return None
        # 若表头直接是"投标总价/投标总报价"，属于横向开标一览表而非分项汇总表
        if any(bk in c for bk in _BID_TOTAL_LABEL_KEYS):
            return None
        if any(k in c for k in _COL_TOTAL_PRICE_KEYS) and 'total' not in col_map:
            col_map['total'] = idx
            continue
        if any(k in c for k in _COL_NAME_KEYS) and 'name' not in col_map:
            col_map['name'] = idx

    if 'name' in col_map and 'total' in col_map and col_map['name'] != col_map['total']:
        col_map['is_wan'] = is_wan
        return col_map
    return None


def _try_parse_cap_cell_value(cell_txt: str, label_has_unit: bool, label_is_wan: bool) -> Decimal | None:
    """从表格目标单元格提取限价/预算金额。"""
    txt = (cell_txt or '').strip()
    if not txt:
        return None
    if re.match(r'^(?:(?:采购)?包|分包|标段)?\s*[0-9一二三四五六七八九十]{1,2}\s*$', txt):
        if not label_has_unit:
            return None
    v = _parse_cap_money_str(
        txt,
        default_wan=(label_is_wan or '万元' in txt),
        require_unit=(not label_has_unit),
    )
    if v is not None and v >= Decimal('100'):
        return v
    return None


# ── E7 / E8 限价语境剔除、多金额歧义护栏与表格分级辅助函数 ───────────────

def _has_non_bid_context(text: str) -> bool:
    """判断文本片段是否含最高限价/预算/控制价/不得高于/保证金等非投标报价语境词。"""
    if not text:
        return False
    return any(k in text for k in _NON_BID_UPPER_KEYS) or any(k in text for k in _INSTRUCTION_NOISE_KEYS)


def _strip_non_bid_clauses(cell_text: str) -> str:
    """从单元格文本中剥离含限价/预算语境词的分句或括号注记，仅保留纯投标响应分句。
    若整格均为限价说明（如"不得高于最高限价390,000.00元（叁拾玖万元整）"），剥离后返回空串 ''。
    """
    raw = (cell_text or '').strip()
    if not raw:
        return ''
    if not _has_non_bid_context(raw):
        return raw

    def _clean_paren(m: re.Match) -> str:
        inner = m.group(0)
        return '' if _has_non_bid_context(inner) else inner

    cleaned = re.sub(r'[（(][^）)]*[）)]', _clean_paren, raw)
    # 千分位逗号保护：ASCII 逗号仅在其两侧不同时紧邻数字时才视为分句符，
    # 否则 1,180,000.00（含括号限价注记）会被切碎成 "1 180 000.00" 致金额解析坍缩
    clauses = re.split(r'[；;。\n，]+|(?<![0-9]),(?![0-9])', cleaned)
    kept = [c.strip() for c in clauses if c.strip() and not _has_non_bid_context(c)]
    return ' '.join(kept).strip()


def _extract_summary_row_amount(cells, total_i: int, is_wan: bool) -> Decimal | None:
    """E8：从明细表或汇总表的小计/合计行提取金额（继承 E7 限价语境剔除，防止备注格里的最高限价被误读为小计/合计）。"""
    if total_i < len(cells):
        cleaned_target = _strip_non_bid_clauses(cells[total_i].text)
        cand_val = _parse_money_str(cleaned_target, default_wan=is_wan)
        if cand_val is not None and cand_val > 0:
            return cand_val

    for c in reversed(cells):
        cleaned_c = _strip_non_bid_clauses(c.text)
        if not cleaned_c:
            continue
        cand_val = _parse_money_str(cleaned_c, default_wan=is_wan)
        if cand_val is not None and cand_val > 0:
            return cand_val
    return None


def _classify_summary_row_kind(first_non_empty: str, name_cell_txt: str, row_full_text: str) -> str:
    """判定汇总行属于 'subtotal'（小计）还是 'grand_total'（合计/总计）。"""
    head_str = f"{first_non_empty} {name_cell_txt}".strip()
    if _SUBTOTAL_ROW_RE.search(head_str) or ('小计' in head_str and '合计' not in head_str and '总计' not in head_str):
        return 'subtotal'
    if _GRAND_TOTAL_ROW_RE.search(head_str) or any(k in head_str for k in ('合计', '总计', '总价', '总报价', '总金额')):
        return 'grand_total'
    if '小计' in row_full_text and '合计' not in row_full_text and '总计' not in row_full_text:
        return 'subtotal'
    return 'grand_total'


def _extract_all_money_values_from_cell(
    cell_text: str,
    default_wan: bool = False,
    require_unit: bool = True,
) -> list[Decimal]:
    """从单个已剥离限价语境的单元格中提取所有有效金额（统一折算为元，去重保序）。"""
    clean_txt = _strip_non_bid_clauses(cell_text)
    if not clean_txt or clean_txt in _PLACEHOLDER_CELL_TOKENS or '…' in clean_txt or '...' in clean_txt:
        return []

    found: list[Decimal] = []
    for m in _MONEY_WITH_UNIT_SCAN_RE.finditer(clean_txt):
        num_s = m.group(1) or m.group(3)
        unit_s = m.group(2) or m.group(4) or ''
        if not num_s:
            continue
        try:
            d = Decimal(num_s.replace(',', ''))
        except InvalidOperation:
            continue
        if d <= 0:
            continue
        is_w = ('万' in unit_s) or (not unit_s and default_wan)
        if is_w:
            d = d * WAN
        q_val = d.quantize(CENT, rounding=ROUND_HALF_UP)
        if q_val >= Decimal('50') and q_val not in found:
            found.append(q_val)

    if not found and not require_unit:
        v = _parse_money_str(clean_txt, default_wan=default_wan, require_unit=False)
        if v is not None and v >= Decimal('50'):
            found.append(v)

    return found


def _extract_all_upper_values_from_cell(cell_text: str) -> list[str]:
    """从单个已剥离限价语境的单元格中提取所有大写金额字符串（去重保序）。"""
    clean_txt = _strip_non_bid_clauses(cell_text)
    if not clean_txt:
        return []
    uppers: list[str] = []
    for m_u in _UPPER_AMOUNT_RE.finditer(clean_txt):
        u_str = m_u.group(2).strip()
        if u_str and u_str not in uppers:
            uppers.append(u_str)
    return uppers


def _is_bid_total_label_cell(label_text: str) -> tuple[bool, bool]:
    """判断单元格是否为「投标总报价」标签格。"""
    clean_lbl = re.sub(r'\s+', '', label_text or '')
    if not clean_lbl or len(clean_lbl) > 42:
        return False, False

    core_no_paren = re.sub(r'[（(][^）)]*[）)]', '', clean_lbl)
    head_core = re.split(r'[：:]', core_no_paren, maxsplit=1)[0].strip()
    if not head_core or len(head_core) > 20:
        return False, False

    if not any(k in head_core for k in _BID_TOTAL_LABEL_KEYS):
        return False, False

    # E8 §5.2 ⑤：若标签含"小计"（如"投标总报价小计"/"分项报价小计"），属于分表小计行而非全书总报价
    if '小计' in head_core:
        return False, False

    if _has_non_bid_context(head_core):
        return False, False

    parens = re.findall(r'[（(]([^）)]*)[）)]', clean_lbl)
    unit_is_wan = ('万元' in head_core) or any(
        '万元' in p and not _has_non_bid_context(p) for p in parens
    )
    return True, unit_is_wan


def _is_tender_requirement_column_header(header_text: str) -> bool:
    """判断表头列是否为"招标文件要求 / 最高限价"列（而非我方投标响应列）。"""
    h = re.sub(r'\s+', '', header_text or '')
    if not h:
        return False
    if any(k in h for k in ('招标文件要求', '招标要求', '最高限价', '控制价', '采购预算', '预算金额')):
        if not any(k in h for k in ('我方', '响应', '承诺', '投标人填写')):
            return True
    return False


def _classify_table_priority(deduped_rows: list[list[str]], prev_heading: str = '') -> str:
    """E7 §3.3：判定表格属于独立《开标一览表》主源 ('primary') 还是自洽表/应答表 ('secondary')。"""
    if not deduped_rows:
        return 'secondary'

    first_row = [re.sub(r'\s+', '', c) for c in deduped_rows[0]]
    first_row_joined = ' '.join(first_row)
    top_two_joined = ' '.join(' '.join(r) for r in deduped_rows[:2])

    if any(k in first_row_joined for k in _SELF_CHECK_HEADER_KEYS):
        return 'secondary'

    if any(k in prev_heading for k in ('开标一览', '报价一览', '唱标一览')):
        return 'primary'
    if any(k in top_two_joined for k in ('开标一览', '报价一览', '唱标一览')):
        return 'primary'

    has_proj_col = any(any(pk in c for pk in ('项目名称', '项目编号', '品目名称', '标段', '包号')) for c in first_row)
    has_bid_col = any(
        any(bk in c for bk in _BID_TOTAL_LABEL_KEYS) and not _has_non_bid_context(c)
        for c in first_row
    )
    if has_proj_col and has_bid_col and len(first_row) >= 2:
        return 'primary'

    if len(first_row) == 2 and any('项目名称' in c for c in first_row):
        all_labels = [re.sub(r'\s+', '', r[0]) for r in deduped_rows if r]
        if any(any(bk in lbl for bk in _BID_TOTAL_LABEL_KEYS) for lbl in all_labels):
            return 'primary'

    return 'secondary'


def _iter_doc_tables_with_prev_heading(doc) -> list[tuple[object, str]]:
    """按文档块顺序遍历 doc.tables，并附带每个表格紧邻的上方非空段落文本。"""
    from docx.oxml.ns import qn

    tbl_by_el = {t._tbl: t for t in doc.tables}
    out: list[tuple[object, str]] = []
    last_para_txt = ''

    body = getattr(doc.element, 'body', None)
    if body is not None:
        for child in body.iterchildren():
            if child.tag == qn('w:p'):
                txt = ''.join(t.text or '' for t in child.iter(qn('w:t'))).strip()
                if txt:
                    last_para_txt = txt
            elif child.tag == qn('w:tbl') and child in tbl_by_el:
                out.append((tbl_by_el[child], last_para_txt))

    if len(out) != len(doc.tables):
        return [(t, '') for t in doc.tables]
    return out


def _extract_from_docx_tables(doc) -> dict:
    """遍历 python-docx 的所有表格，提取开标一览表、分项报价明细表（含多分表小计聚合）、分项汇总表及预算限价。"""
    items = []
    high_cap_in_table = None
    budget_cap_in_table = None
    has_price_table_skeleton = False

    # E8 跨表去重集合（以底层 _tbl 元素引用为键，严防同一张表被重复计入）
    processed_table_els: set[object] = set()
    # 明细表级别汇总记录：[{'tbl_el': _tbl, 'item_count': int, 'subtotals': [Decimal], 'grand_totals': [Decimal]}]
    detail_table_records: list[dict] = []
    # 分项汇总表记录（无单价/数量列，含分项名称+金额+合计行）：[{'tbl_el': _tbl, 'sub_rows_sum': Decimal, 'total_val': Decimal}]
    summary_table_records: list[dict] = []

    # E7 分级收集容器：primary 存独立《开标一览表》候选，secondary 存自洽表/应答表候选
    primary_bid_totals: list[str] = []
    primary_bid_uppers: list[str] = []
    secondary_bid_totals: list[str] = []
    secondary_bid_uppers: list[str] = []
    item_table_uppers: list[str] = []
    warnings: list[dict] = []
    bid_total_ambiguous = False
    has_subtotal_summary_mismatch = False

    for table, prev_heading in _iter_doc_tables_with_prev_heading(doc):
        tbl_el = getattr(table, '_tbl', id(table))
        if tbl_el in processed_table_els:
            continue

        rows = table.rows
        if not rows:
            continue

        first_row_txt = ' '.join(c.text.strip() for c in rows[0].cells)
        table_is_wan = ('万元' in first_row_txt)

        # ── 0. 无论何种表格，先扫描是否存在「最高限价 / 采购预算 / 预算金额」跨格或跨行定义 ──
        deduped_rows: list[list[str]] = []
        for row in rows:
            u_cells = []
            s_tc = set()
            for c in row.cells:
                if c._tc not in s_tc:
                    s_tc.add(c._tc)
                    u_cells.append(c.text.strip())
            if u_cells:
                deduped_rows.append(u_cells)

        for r_idx, uniq_cells in enumerate(deduped_rows):
            for c_idx, label in enumerate(uniq_cells):
                clean_lbl = re.sub(r'\s+', '', label)
                if not clean_lbl or len(clean_lbl) > 35:
                    continue
                is_high_key = any(k in clean_lbl for k in _CAP_LABEL_HIGH_KEYS)
                is_budget_key = any(k in clean_lbl for k in _CAP_LABEL_BUDGET_KEYS)
                if not is_high_key and not is_budget_key:
                    continue

                lbl_is_wan = ('万元' in clean_lbl) or table_is_wan
                lbl_has_unit = ('万元' in clean_lbl) or ('元' in clean_lbl)

                cand_self = _parse_cap_money_str(label, default_wan=lbl_is_wan, require_unit=True)
                if cand_self is not None and cand_self >= Decimal('100'):
                    if is_high_key and high_cap_in_table is None:
                        high_cap_in_table = _fmt_yuan(cand_self)
                    elif budget_cap_in_table is None:
                        budget_cap_in_table = _fmt_yuan(cand_self)
                    continue

                matched_horiz = None
                for step in range(1, 4):
                    if c_idx + step >= len(uniq_cells):
                        break
                    target_txt = uniq_cells[c_idx + step]
                    val = _try_parse_cap_cell_value(target_txt, lbl_has_unit, lbl_is_wan)
                    if val is not None:
                        matched_horiz = val
                        break
                if matched_horiz is not None:
                    if is_high_key and high_cap_in_table is None:
                        high_cap_in_table = _fmt_yuan(matched_horiz)
                    elif budget_cap_in_table is None:
                        budget_cap_in_table = _fmt_yuan(matched_horiz)
                    continue

                if r_idx + 1 < len(deduped_rows):
                    next_row = deduped_rows[r_idx + 1]
                    if c_idx < len(next_row):
                        val_down = _try_parse_cap_cell_value(next_row[c_idx], lbl_has_unit, lbl_is_wan)
                        if val_down is not None:
                            if is_high_key and high_cap_in_table is None:
                                high_cap_in_table = _fmt_yuan(val_down)
                            elif budget_cap_in_table is None:
                                budget_cap_in_table = _fmt_yuan(val_down)

        # ── 1. 尝试作为《分项报价明细表》解析（含单价/数量列） ──
        header_idx = None
        col_map = None
        for r_i, row in enumerate(rows[:5]):
            row_texts = [c.text.strip() for c in row.cells]
            detected = _detect_header_map(row_texts)
            if detected:
                header_idx = r_i
                col_map = detected
                if table_is_wan:
                    col_map['is_wan'] = True
                has_price_table_skeleton = True
                break

        if col_map is not None and header_idx is not None:
            processed_table_els.add(tbl_el)
            is_wan = col_map['is_wan']
            name_i = col_map['name']
            qty_i = col_map.get('qty')
            unit_i = col_map.get('unit')
            total_i = col_map['total']

            seen_row_tcs = set()
            tbl_item_count = 0
            tbl_subtotals: list[Decimal] = []
            tbl_grand_totals: list[Decimal] = []

            for row in rows[header_idx + 1:]:
                cells = row.cells
                if max(name_i, total_i) >= len(cells):
                    continue

                tc_pair = (cells[name_i]._tc, cells[total_i]._tc)
                if tc_pair in seen_row_tcs:
                    continue
                seen_row_tcs.add(tc_pair)

                unique_tcs = {c._tc for c in cells}
                row_full_text = ' '.join(dict.fromkeys(c.text.strip() for c in cells if c.text.strip()))

                if not _has_non_bid_context(row_full_text):
                    m_up = _UPPER_AMOUNT_RE.search(row_full_text)
                    if m_up and not item_table_uppers:
                        item_table_uppers.append(m_up.group(2).strip())

                first_non_empty = next((c.text.strip() for c in cells if c.text.strip()), '')
                name_cell_txt = cells[name_i].text.strip()
                if (_SUMMARY_ROW_RE.match(first_non_empty)
                        or _SUMMARY_ROW_RE.match(name_cell_txt)
                        or any(_SUMMARY_ROW_RE.match(c.text.strip()) for c in cells[:2])):
                    cand_val = _extract_summary_row_amount(cells, total_i, is_wan)
                    if cand_val is not None and cand_val > 0:
                        s_kind = _classify_summary_row_kind(first_non_empty, name_cell_txt, row_full_text)
                        if s_kind == 'subtotal':
                            tbl_subtotals.append(cand_val)
                        else:
                            tbl_grand_totals.append(cand_val)
                    continue

                if len(unique_tcs) == 1:
                    continue

                item_name = name_cell_txt
                if not item_name or _SEQ_ONLY_RE.match(item_name):
                    continue

                raw_total_txt = cells[total_i].text if total_i < len(cells) else ''
                raw_unit_txt = cells[unit_i].text if (unit_i is not None and unit_i < len(cells)) else ''
                raw_qty_txt = cells[qty_i].text if (qty_i is not None and qty_i < len(cells)) else '1'

                total_val = _parse_money_str(_strip_non_bid_clauses(raw_total_txt), default_wan=is_wan)
                unit_val = (_parse_money_str(_strip_non_bid_clauses(raw_unit_txt), default_wan=is_wan)
                            if unit_i is not None and unit_i < len(cells) else None)
                qty_val = (_parse_qty_str(raw_qty_txt)
                           if qty_i is not None and qty_i < len(cells) else Decimal('1'))

                if total_val is None and unit_val is None:
                    continue
                if total_val is not None and unit_val is not None and qty_val is not None:
                    if _is_bogus_price_row(
                        item_name=item_name,
                        qty_raw=raw_qty_txt,
                        unit_raw=raw_unit_txt,
                        total_raw=raw_total_txt,
                        qty_val=qty_val,
                        unit_val=unit_val,
                        total_val=total_val,
                    ):
                        continue
                    items.append({
                        'name': item_name,
                        'qty': str(qty_val),
                        'unit_price': _fmt_yuan(unit_val),
                        'total_price': _fmt_yuan(total_val),
                    })
                    tbl_item_count += 1

            if tbl_subtotals or tbl_grand_totals:
                detail_table_records.append({
                    'tbl_el': tbl_el,
                    'item_count': tbl_item_count,
                    'subtotals': tbl_subtotals,
                    'grand_totals': tbl_grand_totals,
                })
            continue

        # ── 1.5 E8 §3.2 尝试作为《分项汇总表》解析（无单价/数量列，但含分项名称+金额+合计行） ──
        sum_hdr_idx = None
        sum_col_map = None
        for r_i, row in enumerate(rows[:3]):
            row_texts = [c.text.strip() for c in row.cells]
            detected_sum = _detect_summary_table_header_map(row_texts)
            if detected_sum:
                sum_hdr_idx = r_i
                sum_col_map = detected_sum
                if table_is_wan:
                    sum_col_map['is_wan'] = True
                break

        if sum_col_map is not None and sum_hdr_idx is not None:
            is_wan = sum_col_map['is_wan']
            s_name_i = sum_col_map['name']
            s_total_i = sum_col_map['total']
            seen_s_tcs = set()
            sub_row_amounts: list[Decimal] = []
            sum_table_totals: list[Decimal] = []
            sum_table_uppers: list[str] = []

            for row in rows[sum_hdr_idx + 1:]:
                cells = row.cells
                if max(s_name_i, s_total_i) >= len(cells):
                    continue
                tc_pair = (cells[s_name_i]._tc, cells[s_total_i]._tc)
                if tc_pair in seen_s_tcs:
                    continue
                seen_s_tcs.add(tc_pair)

                row_full_text = ' '.join(dict.fromkeys(c.text.strip() for c in cells if c.text.strip()))
                if not _has_non_bid_context(row_full_text):
                    m_up = _UPPER_AMOUNT_RE.search(row_full_text)
                    if m_up and not sum_table_uppers:
                        sum_table_uppers.append(m_up.group(2).strip())

                first_non_empty = next((c.text.strip() for c in cells if c.text.strip()), '')
                name_cell_txt = cells[s_name_i].text.strip()
                if (_SUMMARY_ROW_RE.match(first_non_empty)
                        or _SUMMARY_ROW_RE.match(name_cell_txt)
                        or any(_SUMMARY_ROW_RE.match(c.text.strip()) for c in cells[:2])):
                    cand_val = _extract_summary_row_amount(cells, s_total_i, is_wan)
                    if cand_val is not None and cand_val >= Decimal('50'):
                        sum_table_totals.append(cand_val)
                    continue

                if len({c._tc for c in cells}) == 1:
                    continue
                if not name_cell_txt or _SEQ_ONLY_RE.match(name_cell_txt):
                    continue
                clean_nm = re.sub(r'\s+', '', name_cell_txt)
                if clean_nm in _HEADER_NAME_BLACKLIST or any(bk in clean_nm for bk in _BID_TOTAL_LABEL_KEYS):
                    continue

                r_val = _parse_money_str(_strip_non_bid_clauses(cells[s_total_i].text), default_wan=is_wan)
                if r_val is not None and r_val >= Decimal('50'):
                    sub_row_amounts.append(r_val)

            # 仅当同时存在 >=1 个分项行和 >=1 个合计行时，确认为《分项汇总表》并锁入 processed_table_els
            if sub_row_amounts and sum_table_totals:
                processed_table_els.add(tbl_el)
                has_price_table_skeleton = True
                summary_table_records.append({
                    'tbl_el': tbl_el,
                    'sub_rows_sum': sum(sub_row_amounts, Decimal('0.00')),
                    'total_val': sum_table_totals[-1],
                })
                for u_str in sum_table_uppers:
                    if u_str not in item_table_uppers:
                        item_table_uppers.append(u_str)
                continue

        # ── 2. 作为《开标一览表》或《商务承诺自洽表/应答表》解析投标总报价 ──
        tbl_priority = _classify_table_priority(deduped_rows, prev_heading=prev_heading)
        target_totals = primary_bid_totals if tbl_priority == 'primary' else secondary_bid_totals
        target_uppers = primary_bid_uppers if tbl_priority == 'primary' else secondary_bid_uppers

        header_cells = [re.sub(r'\s+', '', c.text) for c in rows[0].cells]
        bid_col = None
        upper_col = None
        if tbl_priority == 'primary' and len(header_cells) >= 2:
            for idx, h in enumerate(header_cells):
                is_lbl, _ = _is_bid_total_label_cell(h)
                if is_lbl:
                    if '大写' in h and '小写' not in h:
                        upper_col = idx
                    elif bid_col is None:
                        bid_col = idx
                    else:
                        upper_col = idx

        if bid_col is not None and len(rows) >= 2:
            has_price_table_skeleton = True
            row1 = rows[1].cells
            if bid_col < len(row1):
                cell_txt = row1[bid_col].text.strip()
                col_is_wan = ('万元' in header_cells[bid_col])
                vals = _extract_all_money_values_from_cell(
                    cell_txt, default_wan=col_is_wan, require_unit=False
                )
                if len(vals) == 1:
                    target_totals.append(_fmt_yuan(vals[0]))
                elif len(vals) >= 2:
                    bid_total_ambiguous = True
                    warnings.append({
                        'code': 'AMBIGUOUS_BID_TOTAL',
                        'severity': 'warning',
                        'message': (
                            f"开标一览表总价单元格存在多个不同金额（{'、'.join(_fmt_yuan(v) + '元' for v in vals)}），"
                            f"无法自动消歧，已跳过总价自动赋值，请人工复核"
                        ),
                    })
                ups = _extract_all_upper_values_from_cell(cell_txt)
                if len(ups) == 1:
                    target_uppers.append(ups[0])
            if upper_col is not None and upper_col < len(row1):
                ups = _extract_all_upper_values_from_cell(row1[upper_col].text)
                if len(ups) == 1:
                    target_uppers.append(ups[0])

        table_header_row = deduped_rows[0] if len(deduped_rows) >= 2 else []

        for r_idx, uniq_cells in enumerate(deduped_rows):
            row_joined = ' '.join(uniq_cells)
            if any(k in row_joined for k in ('开标一览表', '报价一览表', '投标总价', '投标总报价')):
                has_price_table_skeleton = True

            if r_idx == 0 and bid_col is not None:
                continue

            for i, label in enumerate(uniq_cells):
                clean_lbl = re.sub(r'\s+', '', label)
                if len(clean_lbl) > 42:
                    continue

                is_bid_lbl, lbl_is_wan = _is_bid_total_label_cell(label)
                if is_bid_lbl:
                    if i + 1 >= len(uniq_cells) and '：' not in clean_lbl and ':' not in clean_lbl:
                        continue

                    candidate_cells: list[str] = []
                    if i + 1 < len(uniq_cells):
                        for j in range(i + 1, len(uniq_cells)):
                            if j < len(table_header_row) and _is_tender_requirement_column_header(table_header_row[j]):
                                continue
                            stripped_cell = _strip_non_bid_clauses(uniq_cells[j])
                            if stripped_cell:
                                candidate_cells.append(stripped_cell)
                    else:
                        if _has_non_bid_context(label):
                            continue
                        after_colon = re.split(r'[：:]', label, maxsplit=1)[1].strip()
                        stripped_after = _strip_non_bid_clauses(after_colon)
                        if stripped_after:
                            candidate_cells.append(stripped_after)

                    if not candidate_cells:
                        continue

                    core_lbl_no_paren = re.sub(r'[（(][^）)]*[）)]', '', clean_lbl)
                    allow_unitless = (
                        len(candidate_cells) == 1
                        and ('元' in core_lbl_no_paren or '万元' in core_lbl_no_paren or '小写' in clean_lbl)
                        and not _has_non_bid_context(label)
                    )

                    row_money_vals: list[Decimal] = []
                    row_upper_vals: list[str] = []
                    for c_txt in candidate_cells:
                        for v in _extract_all_money_values_from_cell(
                            c_txt, default_wan=lbl_is_wan, require_unit=(not allow_unitless)
                        ):
                            if v not in row_money_vals:
                                row_money_vals.append(v)
                        for u in _extract_all_upper_values_from_cell(c_txt):
                            if u not in row_upper_vals:
                                row_upper_vals.append(u)

                    if '大写' not in core_lbl_no_paren or '小写' in ' '.join(candidate_cells):
                        if len(row_money_vals) >= 2:
                            bid_total_ambiguous = True
                            warnings.append({
                                'code': 'AMBIGUOUS_BID_TOTAL',
                                'severity': 'warning',
                                'message': (
                                    f"投标总报价同行存在多个不同金额（{'、'.join(_fmt_yuan(v) + '元' for v in row_money_vals)}）"
                                    f"且缺少限价/报价语境词消歧，已跳过总价自动赋值，请人工复核"
                                ),
                            })
                        elif len(row_money_vals) == 1:
                            fmt_v = _fmt_yuan(row_money_vals[0])
                            if fmt_v not in target_totals:
                                target_totals.append(fmt_v)

                    if len(row_upper_vals) >= 2:
                        warnings.append({
                            'code': 'AMBIGUOUS_BID_UPPER',
                            'severity': 'warning',
                            'message': (
                                f"投标总报价同行存在多个不同大写金额（{'、'.join(row_upper_vals)}），"
                                f"已跳过大写自动赋值，请人工复核"
                            ),
                        })
                    elif len(row_upper_vals) == 1 and len(row_money_vals) <= 1:
                        if row_upper_vals[0] not in target_uppers:
                            target_uppers.append(row_upper_vals[0])

                elif '大写' in clean_lbl and not _has_non_bid_context(clean_lbl):
                    cand_up_cells: list[str] = []
                    if i + 1 < len(uniq_cells):
                        for j in range(i + 1, len(uniq_cells)):
                            if j < len(table_header_row) and _is_tender_requirement_column_header(table_header_row[j]):
                                continue
                            sc = _strip_non_bid_clauses(uniq_cells[j])
                            if sc:
                                cand_up_cells.append(sc)
                    elif '：' in clean_lbl or ':' in clean_lbl:
                        sc = _strip_non_bid_clauses(re.split(r'[：:]', label, maxsplit=1)[1])
                        if sc:
                            cand_up_cells.append(sc)

                    up_list: list[str] = []
                    for c_txt in cand_up_cells:
                        for u in _extract_all_upper_values_from_cell(c_txt):
                            if u not in up_list:
                                up_list.append(u)
                    if len(up_list) >= 2:
                        warnings.append({
                            'code': 'AMBIGUOUS_BID_UPPER',
                            'severity': 'warning',
                            'message': (
                                f"大写金额行存在多个不同大写值（{'、'.join(up_list)}），"
                                f"已跳过大写自动赋值，请人工复核"
                            ),
                        })
                    elif len(up_list) == 1:
                        if up_list[0] not in target_uppers:
                            target_uppers.append(up_list[0])

    # ── 3. E8 核心：计算明细表聚合总价 (detail_agg_total) 与《分项汇总表》权威总价 ──
    detail_agg_total: Decimal | None = None
    if len(detail_table_records) == 1:
        rec = detail_table_records[0]
        # 单表内既有小计又有合计时，合计优先
        if rec['grand_totals']:
            detail_agg_total = rec['grand_totals'][-1]
        elif rec['subtotals']:
            detail_agg_total = rec['subtotals'][-1]
    elif len(detail_table_records) >= 2:
        # 多张明细表分类：全量明细表（合计+多分项行）、纯小计分表、纯合计表（分项行全为合并格被跳过，
        # 仅捕获到合计行——如整表纵向合并的《分项报价汇总》，其合计行是全文级总价，严禁再与小计累加）
        full_grand_tables = [
            r for r in detail_table_records
            if r['grand_totals'] and r['item_count'] >= 2
        ]
        subtotal_only_tables = [
            r for r in detail_table_records
            if r['subtotals'] and not r['grand_totals']
        ]
        total_only_tables = [
            r for r in detail_table_records
            if r['grand_totals'] and r['item_count'] < 2
        ]
        if len(full_grand_tables) == 1 and not subtotal_only_tables:
            detail_agg_total = full_grand_tables[0]['grand_totals'][-1]
        elif subtotal_only_tables:
            # 多分表布局：table_total = Σ各分表小计（Decimal 精确累加）
            acc = Decimal('0.00')
            for r in subtotal_only_tables:
                acc += r['subtotals'][-1]
            if acc > Decimal('0.00'):
                detail_agg_total = acc.quantize(CENT, rounding=ROUND_HALF_UP)
            # 交叉核验：合计行表（全文级总价或全量明细表合计）应与 Σ小计一致，不一致仅 warning
            for r in total_only_tables + full_grand_tables:
                gt = r['grand_totals'][-1]
                if detail_agg_total is not None and gt != detail_agg_total:
                    has_subtotal_summary_mismatch = True
                    warnings.append({
                        'code': 'SUBTOTAL_SUMMARY_MISMATCH',
                        'severity': 'warning',
                        'message': (
                            f"明细表合计行（{_fmt_yuan(gt)}元）"
                            f"与各分表明细小计累加（{_fmt_yuan(detail_agg_total)}元）不一致，"
                            f"已以各分表小计累加为准，请人工核对"
                        ),
                    })
        elif len(full_grand_tables) == 1:
            detail_agg_total = full_grand_tables[0]['grand_totals'][-1]
        elif len(total_only_tables) == 1:
            # 仅一张纯合计表（无任何分表小计可加）：其合计行即明细总价
            detail_agg_total = total_only_tables[0]['grand_totals'][-1]

    table_total: str | None = None
    if summary_table_records:
        # 《分项汇总表》合计行具有最高优先级（权威源）
        auth_summary = summary_table_records[0]
        auth_total = auth_summary['total_val']
        table_total = _fmt_yuan(auth_total)

        # 汇总表内部各分项行之和与汇总表合计行交叉核验
        if auth_summary['sub_rows_sum'] != auth_total:
            has_subtotal_summary_mismatch = True
            warnings.append({
                'code': 'SUBTOTAL_SUMMARY_MISMATCH',
                'severity': 'warning',
                'message': (
                    f"分项汇总表各行金额累加（{_fmt_yuan(auth_summary['sub_rows_sum'])}元）"
                    f"与汇总表合计行（{_fmt_yuan(auth_total)}元）不一致，请人工核对"
                ),
            })
        # 汇总表合计行与 Σ各分表小计交叉核验（不一致出 warning 级 finding，非 error）
        if detail_agg_total is not None and detail_agg_total != auth_total:
            has_subtotal_summary_mismatch = True
            warnings.append({
                'code': 'SUBTOTAL_SUMMARY_MISMATCH',
                'severity': 'warning',
                'message': (
                    f"各分表明细小计累加（{_fmt_yuan(detail_agg_total)}元）"
                    f"与分项汇总表合计（{_fmt_yuan(auth_total)}元）不一致，"
                    f"已以汇总表合计为准，请人工核对各分表小计"
                ),
            })
    elif detail_agg_total is not None:
        table_total = _fmt_yuan(detail_agg_total)

    # ── 4. E7 §3.3 汇总 primary（独立《开标一览表》）与 secondary（自洽表/应答表）结果 ──
    bid_total = None
    bid_total_upper = None

    if primary_bid_totals:
        bid_total = primary_bid_totals[0]
        for sec_val in secondary_bid_totals:
            if sec_val != bid_total:
                warnings.append({
                    'code': 'CROSS_TABLE_BID_WARNING',
                    'severity': 'warning',
                    'message': (
                        f"商务自洽表/应答表中的投标总报价（{sec_val}元）"
                        f"与开标一览表总报价（{bid_total}元）不一致，已以开标一览表为准，请人工复核"
                    ),
                })
    elif not bid_total_ambiguous and secondary_bid_totals:
        if len(secondary_bid_totals) == 1:
            bid_total = secondary_bid_totals[0]
        else:
            bid_total = None
            bid_total_ambiguous = True
            warnings.append({
                'code': 'AMBIGUOUS_BID_TOTAL',
                'severity': 'warning',
                'message': (
                    f"多处应答表出现不同投标总报价（{'、'.join(secondary_bid_totals)}）"
                    f"且未定位到独立开标一览表，已跳过总价自动赋值，请人工复核"
                ),
            })

    if primary_bid_uppers:
        bid_total_upper = primary_bid_uppers[0]
    elif not bid_total_ambiguous and secondary_bid_uppers:
        bid_total_upper = secondary_bid_uppers[0]
    elif not bid_total_ambiguous and item_table_uppers:
        bid_total_upper = item_table_uppers[0]

    price_cap_in_table = high_cap_in_table if high_cap_in_table is not None else budget_cap_in_table

    return {
        'items': items,
        'table_total': table_total,
        'bid_total': bid_total,
        'bid_total_upper': bid_total_upper,
        'price_cap': price_cap_in_table,
        'has_price_table_skeleton': has_price_table_skeleton,
        'bid_total_ambiguous': bid_total_ambiguous,
        'has_subtotal_summary_mismatch': has_subtotal_summary_mismatch,
        'warnings': warnings,
    }


def extract_price_from_text(text: str) -> dict:
    """从纯文本（或 PDF 解析出的文本行）提取报价要素。"""
    price_cap = extract_price_cap(text)
    bid_total = None
    bid_total_upper = None
    table_total = None
    items = []
    warnings: list[dict] = []

    if text:
        for line in text.splitlines():
            if not any(k in line for k in _BID_TOTAL_LABEL_KEYS):
                continue
            is_lbl, _ = _is_bid_total_label_cell(line)
            if not is_lbl:
                continue
            clean_line = _strip_non_bid_clauses(line) if _has_non_bid_context(line) else line
            if not clean_line:
                continue
            m_bid = _BID_TOTAL_LINE_RE.search(clean_line)
            if not m_bid:
                continue
            after_colon = re.split(r'[：:]', clean_line, maxsplit=1)[-1]
            all_vals = _extract_all_money_values_from_cell(after_colon, require_unit=True)
            if len(all_vals) >= 2:
                warnings.append({
                    'code': 'AMBIGUOUS_BID_TOTAL',
                    'severity': 'warning',
                    'message': (
                        f"文本行存在多个不同报价金额（{'、'.join(_fmt_yuan(v) + '元' for v in all_vals)}），"
                        f"已跳过总价自动赋值，请人工复核"
                    ),
                })
                bid_total = None
                break
            num_s, unit = m_bid.group(1), (m_bid.group(2) or '元')
            v = _parse_money_str(num_s + unit)
            if v is not None and v > 0:
                bid_total = _fmt_yuan(v)
                break

        for line in text.splitlines():
            clean_line = _strip_non_bid_clauses(line)
            if not clean_line:
                continue
            if any(k in clean_line for k in ('投标', '报价', '总价', '合计', '大写')):
                ups = _extract_all_upper_values_from_cell(clean_line)
                if len(ups) == 1:
                    bid_total_upper = ups[0]
                    break
                if len(ups) >= 2:
                    break

        line_item_re = re.compile(
            r'^[ \t\u3000]*(?:[0-9]{1,2}[ \t\u3000]+)?([^\d\n\r]{2,30}?)[ \t\u3000]+'
            r'([0-9]+(?:\.[0-9]+)?)[ \t\u3000]*[项套台个批年]?[ \t\u3000]+'
            r'([0-9][0-9,]*(?:\.[0-9]+)?)[ \t\u3000]+'
            r'([0-9][0-9,]*(?:\.[0-9]+)?)[ \t\u3000]*$',
            re.MULTILINE
        )
        for m in line_item_re.finditer(text):
            name = m.group(1).strip()
            if _SUMMARY_ROW_RE.match(name):
                continue
            if any(k in name for k in _HEADER_NAME_BLACKLIST):
                continue
            qty = _parse_qty_str(m.group(2))
            u_p = _parse_money_str(m.group(3))
            t_p = _parse_money_str(m.group(4))
            if qty and u_p is not None and t_p is not None:
                if _is_bogus_price_row(
                    item_name=name,
                    qty_raw=m.group(2),
                    unit_raw=m.group(3),
                    total_raw=m.group(4),
                    qty_val=qty,
                    unit_val=u_p,
                    total_val=t_p,
                ):
                    continue
                items.append({
                    'name': name,
                    'qty': str(qty),
                    'unit_price': _fmt_yuan(u_p),
                    'total_price': _fmt_yuan(t_p),
                })

        m_tot = re.search(
            r'(?:分项合计|报价合计|合\s*计)[：:\s￥¥]*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(万元|元)',
            text
        )
        if m_tot:
            v_tot = _parse_money_str(m_tot.group(1) + (m_tot.group(2) or '元'))
            if v_tot is not None and v_tot > 0:
                table_total = _fmt_yuan(v_tot)

    has_valid_bid = bool(items) or (bid_total is not None)
    status = 'OK' if has_valid_bid else 'NEED_MANUAL'
    reason = '' if has_valid_bid else '未提取到已填报的投标报价或分项明细（可能为空白招标模板或异形表格），需人工框选确认'

    return {
        'status': status,
        'reason': reason,
        'items': items,
        'table_total': table_total,
        'bid_total': bid_total,
        'bid_total_upper': bid_total_upper,
        'price_cap': price_cap,
        'warnings': warnings,
    }


def extract_price_data(file_path: str | Path, tender_path: str | Path | None = None) -> dict:
    """从投标文件（或招标文件）提取 PriceSandbox 所需的全部结构化字段。"""
    path = Path(file_path)
    parsed = parse_document(str(path))
    text_res = extract_price_from_text(parsed.text)

    res = {
        'status': text_res['status'],
        'reason': text_res['reason'],
        'items': text_res['items'],
        'table_total': text_res['table_total'],
        'bid_total': text_res['bid_total'],
        'bid_total_upper': text_res['bid_total_upper'],
        'price_cap': text_res['price_cap'],
        'has_price_table_skeleton': False,
        'has_subtotal_summary_mismatch': False,
        'warnings': list(text_res.get('warnings') or []),
    }

    if path.suffix.lower() == '.docx':
        from docx import Document
        doc = Document(str(path))
        tbl_res = _extract_from_docx_tables(doc)
        res['has_price_table_skeleton'] = tbl_res['has_price_table_skeleton']
        res['has_subtotal_summary_mismatch'] = bool(tbl_res.get('has_subtotal_summary_mismatch', False))
        if tbl_res.get('warnings'):
            res['warnings'] = list(tbl_res['warnings'])
        if tbl_res['items']:
            res['items'] = tbl_res['items']
        elif tbl_res['has_price_table_skeleton']:
            res['items'] = []
        if tbl_res['table_total'] is not None:
            res['table_total'] = tbl_res['table_total']

        # E7：若表格检出多金额歧义，强制置 bid_total=None，严禁被展平文本误回填
        if tbl_res.get('bid_total_ambiguous'):
            res['bid_total'] = None
            res['bid_total_upper'] = None
        else:
            if tbl_res['bid_total'] is not None:
                res['bid_total'] = tbl_res['bid_total']
            if tbl_res['bid_total_upper'] is not None:
                res['bid_total_upper'] = tbl_res['bid_total_upper']

        if res['price_cap'] is None and tbl_res['price_cap'] is not None:
            res['price_cap'] = tbl_res['price_cap']

    if tender_path is not None:
        tender_parsed = parse_document(str(tender_path))
        t_cap = extract_price_cap(tender_parsed.text)
        if t_cap is None and Path(tender_path).suffix.lower() == '.docx':
            from docx import Document
            t_doc = Document(str(tender_path))
            t_cap = _extract_from_docx_tables(t_doc).get('price_cap')
        if t_cap is not None:
            res['price_cap'] = t_cap

    if res['items'] or res['bid_total'] is not None:
        res['status'] = 'OK'
        res['reason'] = ''
    else:
        res['status'] = 'NEED_MANUAL'
        if res['has_price_table_skeleton']:
            res['reason'] = '已定位到报价表结构，但金额单元格为空白模板或未填报，请人工框选或确认'
        else:
            res['reason'] = '未定位到结构化报价表或有效投标报价金额，请人工框选报价区域'

    return res


def run_price_check(bid_path: str | Path, tender_path: str | Path | None = None) -> dict:
    """端到端辅助入口：提取报价结构 -> 若 status=='OK' 则送入 PriceSandbox 核验，并合并提取层 warning。"""
    extracted = extract_price_data(bid_path, tender_path=tender_path)
    extractor_warnings = list(extracted.get('warnings') or [])
    if extracted['status'] != 'OK':
        extracted['findings'] = extractor_warnings
        return extracted
    sandbox = PriceSandbox(
        items=extracted['items'],
        table_total=extracted['table_total'],
        bid_total=extracted['bid_total'],
        bid_total_upper=extracted['bid_total_upper'],
        price_cap=extracted['price_cap'],
    )
    sb_findings = sandbox.run()
    # E8 §3.2 & §5.2 ④：当存在《分项汇总表》且已针对"Σ分表小计 != 汇总表合计"产出 warning 级 finding 时，
    # 抑制 PriceSandbox 因同一原因重复抛出的 TOTAL_MISMATCH error（但保留 BID_TOTAL_MISMATCH 等真错误）
    if extracted.get('has_subtotal_summary_mismatch'):
        sb_findings = [f for f in sb_findings if f.get('code') != 'TOTAL_MISMATCH']
    extracted['findings'] = extractor_warnings + sb_findings
    return extracted


if __name__ == '__main__':
    corpus_dir = Path('data/corpus/m0_samples')
    if corpus_dir.exists():
        files = sorted([p for p in corpus_dir.iterdir() if p.suffix.lower() in ('.docx', '.pdf')])
        print(f"=== F2-2 真实语料试跑报告（共 {len(files)} 份）===")
        cap_ok, skel_ok, status_ok, need_manual = 0, 0, 0, 0
        for f in files:
            r = extract_price_data(f)
            if r['price_cap']:
                cap_ok += 1
            if r['has_price_table_skeleton']:
                skel_ok += 1
            if r['status'] == 'OK':
                status_ok += 1
            else:
                need_manual += 1
            print(f"- {f.name}: status={r['status']} | price_cap={r['price_cap']} | "
                  f"skeleton={r['has_price_table_skeleton']} | items={len(r['items'])} | bid_total={r['bid_total']}")
        print(f"汇总: price_cap提取成功={cap_ok}/{len(files)}, 报价表模板定位={skel_ok}/{len(files)}, "
              f"status=OK:{status_ok}, NEED_MANUAL:{need_manual}")