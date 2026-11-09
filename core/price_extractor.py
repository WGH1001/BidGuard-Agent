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
9. **E9 明细行收集完整性加固与租赁时长/服务期限定价算术适配**：
   - 放宽数量单元格多字符计量单位（如"人月/个月/节点/点位/式/系统"）、兼容双行合并表头、子序号（1.1/2.1）及纵向合并分类列下探取名，确保分表明细行零漏收；
   - 识别单价列头时间单位（元/年、元/月、元/天）与期限列（租赁期限/服务期/租期），按"有效数量 = 数量 × 期限换算系数"执行算术校验；期限无法解析时跳过该行算术校验并产出 warning（宁缺毋滥）。
10. **E13 双胞胎明细表去重（滁州物业实锤）**：
    - 对收集到的多张明细表按分项合价金额多重集（Decimal 精确比较，不依赖分项名称逐字一致）两两比对；
    - 若两张（或多张）明细表的分项行数一致且分项合价金额多重集完全相等，判定为双胞胎副本表，优先保留列更全（含数量列与单价列）的一张（相同时保留先出现者），剔除副本表 items 并产出 DUPLICATE_DETAIL_TABLE (warning)。
11. **E14 汇总行后缀识别（芜湖无为实锤）**：
    - 在保留前缀匹配的基础上追加后缀分支：当名称单元格以"合计/总计/小计/总报价/总价/投标总价"结尾（如"年度投标报价合计"）且满足防误伤护栏（不含动词/服务描述词、不含说明噪声词、不具备独立单价×数量算术明细特征）时，准确识别为汇总行。
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

# 表头列角色关键词（E9 扩充：覆盖工作内容/功能模块/人数/工时/租赁期限/服务期/费用标准等合法政采表头）
_COL_SEQ_EXACT_KEYS = frozenset({
    '序号', '编号', '行号', '包号', '项号', '子项号', '分项序号', '项目序号', '分项编号', '序号/编号', 'no', 'no.',
})
_COL_NAME_KEYS = (
    '品目名称', '货物名称', '服务名称', '分项名称', '分项工程名称', '项目名称', '系统名称',
    '模块名称', '平台名称', '子系统名称', '设备名称', '标的名称', '标的物',
    '租赁服务名称', '软件名称', '硬件名称', '产品名称', '功能模块', '配置名称',
    '工作内容', '建设内容', '服务内容', '采购内容', '具体内容', '细目名称',
    '岗位名称', '人员岗位', '名称', '品目', '内容', '分项', '子项', '事项', '科目', '项目',
)
_COL_QTY_KEYS = (
    '数量', '采购数量', '投标数量', '计价数量', '数量及单位', '工作量', '套数', '台数',
    '人数', '投入人数', '人月数', '人月', '人天', '工时', '点位数', '路数', '项数',
)
_COL_DURATION_KEYS = (
    '租赁期限', '服务期限', '服务期', '租期', '租赁期', '期限', '年限',
    '服务年限', '维保期', '运维期', '计费周期', '使用期限', '周期',
)
_COL_UNIT_PRICE_KEYS = (
    '单价', '投标单价', '含税单价', '综合单价', '全费用单价', '分项单价',
    '费用标准', '收费标准', '计费标准', '报价单价', '月单价', '年单价',
)
_COL_TOTAL_PRICE_KEYS = (
    '合价', '分项合计', '分项总价', '投标合价', '含税总价',
    '含税合价', '分项报价', '单项合价', '单项合计', '费用合计',
    '小计', '总价', '金额', '费用', '报价',
)

# 单价列头中的时间计费单位识别（如 "单价(元/年)"、"单价（万元/月）"、"元/天"）
_UNIT_HEADER_TIME_RE = re.compile(r'(?:万元|元)\s*[/／每]\s*(年|个?月|天|日)')

# 期限单元格解析正则（支持 "3年"、"36个月"、"36月"、"1.5年"、"三年"、"二十四个月"、"180天"）
_DURATION_CELL_RE = re.compile(
    r'^([0-9]+(?:\.[0-9]+)?|[一二三四五六七八九十百零两半]+)\s*(年|个?月|天|日)\s*[期]?$'
)

_CN_SIMPLE_NUM_MAP = {
    '半': Decimal('0.5'),
    '一': Decimal('1'), '二': Decimal('2'), '两': Decimal('2'), '三': Decimal('3'),
    '四': Decimal('4'), '五': Decimal('5'), '六': Decimal('6'), '七': Decimal('7'),
    '八': Decimal('8'), '九': Decimal('9'), '十': Decimal('10'),
    '十二': Decimal('12'), '十八': Decimal('18'), '二十四': Decimal('24'),
    '三十': Decimal('30'), '三十六': Decimal('36'), '四十八': Decimal('48'), '六十': Decimal('60'),
}

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

# E14 汇总行后缀匹配正则与防误伤词表（芜湖无为实锤："年度投标报价合计"等以汇总词结尾的行名）
_SUMMARY_SUFFIX_RE = re.compile(
    r'(?:投标总报价|投标报价合计|投标总价|报价合计|报价总计|分项合计|本表合计|汇总合计|分项报价小计|分项小计|本表小计|合\s*计|总\s*计|小\s*计|总报价|总价)'
    r'\s*(?:[（(]\s*(?:元|万元|人民币)\s*[)）])?\s*[：:]?\s*$',
    re.IGNORECASE
)
_SUBTOTAL_SUFFIX_RE = re.compile(
    r'(?:小\s*计)\s*(?:[（(]\s*(?:元|万元|人民币)\s*[)）])?\s*[：:]?\s*$',
    re.IGNORECASE
)
# E14 防误伤护栏：行名若含具体业务动词或服务/软硬件交付物描述词，属于真实分项而非汇总行
_SUMMARY_SUFFIX_SERVICE_NOISE_KEYS = (
    '提供', '开发', '运维', '培训', '建设', '实施', '部署', '定制', '升级', '改造',
    '迁移', '集成', '巡检', '驻场', '测试', '测评', '审计', '咨询', '设计',
    '施工', '安装', '调试', '服务', '系统', '模块', '平台', '软件', '硬件', '设备', '接口',
)

# 纯序号判定（E9 扩充：兼容 '1'、'1.1'、'2.1.3'、'（1）'、'一' 等层级子序号）
_SEQ_ONLY_RE = re.compile(
    r'^\s*(?:[0-9]{1,3}(?:\.[0-9]{1,3})*|[一二三四五六七八九十]+|[（(][0-9一二三四五六七八九十]+[)）])\s*$'
)
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
    """E9/E14 扩充：解析数量单元格（兼容 '2', '2台', '1项', '3.5套', '12人月', '36个月', '1节点', '1式', '300000页', '300000条', '100卷' 等多字符量词写法）。"""
    if not raw:
        return None
    s = str(raw).strip().replace(',', '')
    if not s or s in _PLACEHOLDER_CELL_TOKENS or '…' in s or '...' in s:
        return None
    # 排除带货币单位的金额串（防止把金额当成数量）
    if '万元' in s or '元' in s or '￥' in s or '¥' in s:
        return None
    m = re.match(
        r'^([0-9]+(?:\.[0-9]+)?)\s*(?:[项套台个批年人月次份座宗件点路端式天日周季米公顷亩部辆台套页条卷张册盒袋箱千克瓶/·\-]{1,6}|[a-zA-Z]{1,6})?\s*$',
        s,
    )
    if m:
        try:
            d = Decimal(m.group(1))
            return d if d > 0 else None
        except InvalidOperation:
            return None
    # 兼容中文数字数量/时长（如 "三年"、"一年"、"两年"）
    m_cn = re.match(r'^([一二三四五六七八九十两半]{1,3})\s*(?:年|个?月|项|套|台|批|页|条|卷|张|册|盒|袋|箱|瓶|次)?\s*$', s)
    if m_cn and m_cn.group(1) in _CN_SIMPLE_NUM_MAP:
        return _CN_SIMPLE_NUM_MAP[m_cn.group(1)]
    return None


def _parse_duration_factor(
    raw_dur: str,
    unit_time_unit: str | None = None,
    base_qty: Decimal = Decimal('1'),
    unit_val: Decimal | None = None,
    total_val: Decimal | None = None,
) -> Decimal | None:
    """E9 事故二：解析租赁期限/服务期单元格（如 '3年', '36个月', '三年'）并结合单价时间单位（元/年、元/月、元/天）换算有效时长倍数。
    若期限单元格为空或 '-' 等无法解析，返回 None（供上层触发 warning 并跳过算术硬判）。
    """
    if not raw_dur:
        return None
    s = str(raw_dur).strip().replace(',', '')
    if not s or s in _PLACEHOLDER_CELL_TOKENS or '…' in s or '...' in s:
        return None

    m = _DURATION_CELL_RE.match(s)
    if m:
        num_part, dur_unit = m.group(1), m.group(2)
        if num_part in _CN_SIMPLE_NUM_MAP:
            dur_val = _CN_SIMPLE_NUM_MAP[num_part]
        else:
            try:
                dur_val = Decimal(num_part)
            except InvalidOperation:
                return None
        if dur_val <= 0:
            return None
        norm_dur_unit = '月' if '月' in dur_unit else ('天' if dur_unit in ('天', '日') else '年')

        # 按单价列头的时间单位（元/年 vs 元/月 vs 元/天）做跨单位换算
        if unit_time_unit == '年':
            if norm_dur_unit == '月':
                return (dur_val / Decimal('12')).quantize(Decimal('0.0001'))
            if norm_dur_unit == '天':
                return (dur_val / Decimal('365')).quantize(Decimal('0.0001'))
            return dur_val
        if unit_time_unit == '月':
            if norm_dur_unit == '年':
                return dur_val * Decimal('12')
            return dur_val
        if unit_time_unit in ('天', '日'):
            if norm_dur_unit == '年':
                return dur_val * Decimal('365')
            if norm_dur_unit == '月':
                return dur_val * Decimal('30')
            return dur_val

        # 若单价表头未标明 /年 或 /月，但期限写了 "36个月" 且按 3 年折算恰好使 单价×数量×3 == 合价，自适应换算为年
        if (
            norm_dur_unit == '月'
            and dur_val >= Decimal('12')
            and dur_val % Decimal('12') == 0
            and unit_val is not None
            and total_val is not None
        ):
            years_factor = dur_val / Decimal('12')
            if (base_qty * years_factor * unit_val).quantize(CENT, rounding=ROUND_HALF_UP) == total_val:
                return years_factor

        return dur_val

    # 若期限格仅填纯数字（如表头已写"租赁期限（年）"，格内填 "3"）
    return _parse_qty_str(s)


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
    """判断一行是否为分项报价明细表头（含名称+总价+(单价或数量或租赁期限)），
    若是则返回 {'name': idx, 'qty': idx, 'duration': idx, 'unit': idx, 'total': idx, 'is_wan': bool, 'unit_time_unit': str|None}。
    """
    joined = ' '.join(cells_text)
    if any(k in joined for k in _SELF_CHECK_HEADER_KEYS):
        return None
    if any(k in joined for k in ('分值', '得分', '满分', '评分标准', '评审因素')):
        return None

    is_wan = ('万元' in joined)
    col_map: dict = {}

    for idx, raw in enumerate(cells_text):
        c = re.sub(r'\s+', '', raw)
        if not c or len(c) > 40 or c.lower() in _COL_SEQ_EXACT_KEYS:
            continue
        if re.match(r'^(?:序号|编号|行号|包号|项号|子项号|分项序号|项目序号)[（(]?.*[)）]?$', c):
            continue

        # 1. 单价列（含 "单价 (元/年)"、"费用标准" 等）
        if ('单价' in c or any(k in c for k in _COL_UNIT_PRICE_KEYS)) and 'unit' not in col_map:
            col_map['unit'] = idx
            m_tm = _UNIT_HEADER_TIME_RE.search(c)
            if m_tm:
                raw_tu = m_tm.group(1)
                col_map['unit_time_unit'] = '月' if '月' in raw_tu else ('天' if raw_tu in ('天', '日') else '年')
            continue

        # 2. 租赁期限 / 服务期列（优先于普通数量列识别，以便与数量列共存相乘）
        if any(k in c for k in _COL_DURATION_KEYS) and 'duration' not in col_map:
            col_map['duration'] = idx
            continue

        # 3. 数量列
        if any(k in c for k in _COL_QTY_KEYS) and 'qty' not in col_map:
            col_map['qty'] = idx
            continue

        # 4. 名称列优先判据：若表头以"名称/内容/模块/品目/子系统/事项/科目"结尾且不含"元/合价/总价/金额"，优先归入 name
        is_pure_name_hdr = (
            any(k in c for k in _COL_NAME_KEYS)
            and not any(pk in c for pk in ('合价', '总价', '金额', '小计', '合计', '元', '万元'))
        )
        if is_pure_name_hdr and 'name' not in col_map:
            col_map['name'] = idx
            continue

        # 5. 合价 / 总价 / 金额列
        if any(k in c for k in _COL_TOTAL_PRICE_KEYS) and 'total' not in col_map:
            # 排除横向《开标一览表》的"投标总价/投标总报价"列头
            if any(bk in c for bk in ('投标总价', '投标总报价', '总报价')):
                continue
            col_map['total'] = idx
            continue

        # 6. 名称列兜底
        if any(k in c for k in _COL_NAME_KEYS) and 'name' not in col_map:
            col_map['name'] = idx

    if (
        'name' in col_map
        and 'total' in col_map
        and ('unit' in col_map or 'qty' in col_map or 'duration' in col_map)
    ):
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
        if not c or len(c) > 30 or c.lower() in _COL_SEQ_EXACT_KEYS or c in ('备注', '说明'):
            continue
        if '单价' in c or any(k in c for k in _COL_QTY_KEYS) or any(k in c for k in _COL_DURATION_KEYS):
            return None
        if any(bk in c for bk in _BID_TOTAL_LABEL_KEYS):
            return None
        is_pure_name_hdr = (
            any(k in c for k in _COL_NAME_KEYS)
            and not any(pk in c for pk in ('合价', '总价', '金额', '小计', '合计', '元', '万元'))
        )
        if is_pure_name_hdr and 'name' not in col_map:
            col_map['name'] = idx
            continue
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


# ── E7 / E8 / E9 / E13 / E14 限价语境剔除、多金额歧义护栏与表格分级辅助函数 ──

def _has_non_bid_context(text: str) -> bool:
    """判断文本片段是否含最高限价/预算/控制价/不得高于/保证金等非投标报价语境词。"""
    if not text:
        return False
    return any(k in text for k in _NON_BID_UPPER_KEYS) or any(k in text for k in _INSTRUCTION_NOISE_KEYS)


def _strip_non_bid_clauses(cell_text: str) -> str:
    """从单元格文本中剥离含限价/预算语境词的分句或括号注记，仅保留纯投标响应分句。"""
    raw = (cell_text or '').strip()
    if not raw:
        return ''
    if not _has_non_bid_context(raw):
        return raw

    def _clean_paren(m: re.Match) -> str:
        inner = m.group(0)
        return '' if _has_non_bid_context(inner) else inner

    cleaned = re.sub(r'[（(][^）)]*[）)]', _clean_paren, raw)
    # 千分位逗号保护：ASCII 逗号仅在其两侧不同时紧邻数字时才视为分句符
    clauses = re.split(r'[；;。\n，]+|(?<![0-9]),(?![0-9])', cleaned)
    kept = [c.strip() for c in clauses if c.strip() and not _has_non_bid_context(c)]
    return ' '.join(kept).strip()


def _extract_summary_row_amount(cells, total_i: int, is_wan: bool) -> Decimal | None:
    """E8：从明细表或汇总表的小计/合计行提取金额（继承 E7 限价语境剔除）。"""
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


def _has_independent_detail_arithmetic(
    cells,
    total_i: int,
    unit_i: int | None,
    qty_i: int | None,
    is_wan: bool,
) -> bool:
    """E14 护栏 3（前后缀共用）：若该行存在独立的单价列（与合价列非同一合并格），
    且满足 (a) 单价×数量 == 合价（Decimal 精确，数量格可解析出正数），或
    (b) 单价 > 0 且单价 != 合价（典型数量 != 1 的明细行），
    则判定该行具备标准明细算术特征，豁免汇总行识别（按普通明细行保留）。
    """
    if unit_i is None or unit_i >= len(cells) or total_i >= len(cells):
        return False
    if cells[unit_i]._tc is cells[total_i]._tc:
        return False
    u_val = _parse_money_str(_strip_non_bid_clauses(cells[unit_i].text), default_wan=is_wan)
    t_val = _parse_money_str(_strip_non_bid_clauses(cells[total_i].text), default_wan=is_wan)
    if u_val is None or u_val <= 0 or t_val is None or t_val <= 0:
        return False
    if u_val != t_val:
        return True
    if qty_i is not None and qty_i < len(cells):
        if cells[qty_i]._tc is not cells[total_i]._tc and cells[qty_i]._tc is not cells[unit_i]._tc:
            q_val = _parse_qty_str(cells[qty_i].text)
            if q_val is not None and q_val > 0:
                expected_total = (u_val * q_val).quantize(CENT, rounding=ROUND_HALF_UP)
                if expected_total == t_val:
                    return True
    return False


def _is_suffix_summary_row(
    name_cell_txt: str,
    row_full_text: str,
    cells,
    total_i: int,
    unit_i: int | None,
    is_wan: bool,
    qty_i: int | None = None,
) -> bool:
    """E14 §2：汇总行后缀识别与三道防误伤护栏（纯追加判定，前缀匹配未命中时调用）。

    触发条件与护栏（必须全部满足）：
    1. 名称单元格文本（name_cell_txt）以 合计/总计/小计/总报价/总价/投标总价 结尾（_SUMMARY_SUFFIX_RE）；
    2. 名称单元格不命中 _INSTRUCTION_NOISE_KEYS / _NON_BID_UPPER_KEYS（合并整行亦不得命中）；
    3. 名称单元格不含动词或服务/软硬件交付内容描述词（_SUMMARY_SUFFIX_SERVICE_NOISE_KEYS）；
    4. 该行含 >=1 个有效金额单元格（_extract_summary_row_amount 非空），且不具备独立明细算术特征（_has_independent_detail_arithmetic 为 False）。
    """
    nm = (name_cell_txt or '').strip()
    if not nm or len(nm) > 28:
        return False
    if not _SUMMARY_SUFFIX_RE.search(nm):
        return False
    if any(k in nm for k in _INSTRUCTION_NOISE_KEYS) or _has_non_bid_context(nm):
        return False
    if len({c._tc for c in cells}) == 1 and any(k in row_full_text for k in _INSTRUCTION_NOISE_KEYS):
        return False
    if any(k in nm for k in _SUMMARY_SUFFIX_SERVICE_NOISE_KEYS):
        return False

    amt = _extract_summary_row_amount(cells, total_i, is_wan)
    if amt is None or amt <= 0:
        return False

    if _has_independent_detail_arithmetic(cells, total_i, unit_i, qty_i, is_wan):
        return False

    return True


def _classify_summary_row_kind(first_non_empty: str, name_cell_txt: str, row_full_text: str) -> str:
    """判定汇总行属于 'subtotal'（小计）还是 'grand_total'（合计/总计）。"""
    head_str = f"{first_non_empty} {name_cell_txt}".strip()
    if (
        _SUBTOTAL_ROW_RE.search(head_str)
        or _SUBTOTAL_SUFFIX_RE.search(name_cell_txt or '')
        or ('小计' in head_str and '合计' not in head_str and '总计' not in head_str)
    ):
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


def _resolve_row_item_name(
    cells,
    name_i: int,
    excluded_col_indices: set[int],
    last_valid_name: str,
) -> str:
    """E9 事故一修复：当 name_i 列单元格为空（纵向合并 vMerge 延续行）或恰为子序号（如 '1.1'/'2.1'）时，
    自动从同行相邻的非数值文本列（如二级模块/具体内容列）提取名称；若仍无则继承上一行分类名称。
    """
    raw_name = cells[name_i].text.strip() if name_i < len(cells) else ''
    if raw_name and not _SEQ_ONLY_RE.match(raw_name):
        return raw_name

    for c_idx, cell in enumerate(cells):
        if c_idx == name_i or c_idx in excluded_col_indices:
            continue
        txt = cell.text.strip()
        if not txt or _SEQ_ONLY_RE.match(txt) or txt in _PLACEHOLDER_CELL_TOKENS:
            continue
        if _SUMMARY_ROW_RE.match(txt):
            continue
        if re.match(r'^[￥¥]?\s*[0-9][0-9,]*(?:\.[0-9]+)?\s*(?:万元|万|元|%|\s)*$', txt):
            continue
        if _parse_qty_str(txt) is not None and len(txt) <= 5:
            continue
        return f"{raw_name} {txt}".strip() if raw_name else txt

    if last_valid_name:
        return f"{last_valid_name} ({raw_name})" if raw_name else last_valid_name
    return ''


def _deduplicate_twin_detail_tables(
    items: list[dict],
    detail_table_records: list[dict],
    warnings: list[dict],
) -> None:
    """E13 §2：双胞胎明细表去重（原地更新 items、detail_table_records 与 warnings）。

    判定与保留规则：
    1. 按来源表分组，对分项行数 >= 2 且分项合价金额多重集（tuple(sorted(item_vals))，Decimal 精确比较）
       完全一致的多张明细表，判定为双胞胎副本表（不依赖分项名称逐字相等）；
    2. 保留标准：优先保留列更全的一张（含数量列 has_qty_col + 含单价列 has_unit_col 得分更高者优先；
       得分相同时保留文档中先出现者）；
    3. 若被剔除的副本表带有小计/合计行而保留表未带，将小计/合计行转移至保留表；
    4. 剔除副本表的全部 items，并在 warnings 中追加 code='DUPLICATE_DETAIL_TABLE' (severity='warning')。
    """
    if len(detail_table_records) < 2:
        return

    groups_by_multiset: dict[tuple[Decimal, ...], list[tuple[int, dict]]] = {}
    for order_idx, rec in enumerate(detail_table_records):
        vals = rec.get('item_vals') or []
        if len(vals) < 2:
            continue
        key = tuple(sorted(vals))
        groups_by_multiset.setdefault(key, []).append((order_idx, rec))

    drop_item_obj_ids: set[int] = set()
    drop_rec_ids: set[int] = set()

    for key, grp in groups_by_multiset.items():
        if len(grp) < 2:
            continue

        # 选出唯一保留表：优先列更全（has_qty_col + has_unit_col），相同时保留先出现者（-order_idx 最大）
        def _completeness_score(entry: tuple[int, dict]) -> tuple[int, int]:
            ord_i, r = entry
            col_score = (1 if r.get('has_qty_col') else 0) + (1 if r.get('has_unit_col') else 0)
            return col_score, -ord_i

        winner_ord, winner_rec = max(grp, key=_completeness_score)
        for ord_i, r in grp:
            if r is winner_rec:
                continue
            drop_rec_ids.add(id(r))
            for obj in (r.get('item_objs') or []):
                drop_item_obj_ids.add(id(obj))
            if not winner_rec.get('grand_totals') and r.get('grand_totals'):
                winner_rec['grand_totals'] = list(r['grand_totals'])
            if not winner_rec.get('subtotals') and r.get('subtotals'):
                winner_rec['subtotals'] = list(r['subtotals'])

        warnings.append({
            'code': 'DUPLICATE_DETAIL_TABLE',
            'severity': 'warning',
            'message': (
                f"检测到 {len(grp)} 张内容完全一致的报价明细表"
                f"（共 {len(key)} 项分项，合计 {_fmt_yuan(sum(key, Decimal('0.00')))} 元），已去重保留 1 张"
            ),
        })

    if not drop_rec_ids:
        return

    items[:] = [it for it in items if id(it) not in drop_item_obj_ids]
    detail_table_records[:] = [r for r in detail_table_records if id(r) not in drop_rec_ids]

    # 重新同步剩余各表的 item_indices（供后续 E9 §2.5 父子表防重安全消费）
    idx_by_obj_id = {id(it): idx for idx, it in enumerate(items)}
    for r in detail_table_records:
        r['item_indices'] = [
            idx_by_obj_id[id(obj)]
            for obj in (r.get('item_objs') or [])
            if id(obj) in idx_by_obj_id
        ]


def _extract_from_docx_tables(doc) -> dict:
    """遍历 python-docx 的所有表格，提取开标一览表、分项报价明细表（含多分表小计聚合与租赁时长定价）、分项汇总表及预算限价。"""
    items = []
    high_cap_in_table = None
    budget_cap_in_table = None
    has_price_table_skeleton = False

    processed_table_els: set[object] = set()
    detail_table_records: list[dict] = []
    summary_table_records: list[dict] = []

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

        # ── 1. 尝试作为《分项报价明细表》解析（支持单行表头与双行纵向合并表头） ──
        header_idx = None
        col_map = None
        for r_i, row in enumerate(rows[:6]):
            row_texts = [c.text.strip() for c in row.cells]
            detected = _detect_header_map(row_texts)
            if not detected and r_i > 0:
                merged_hdr_texts = list(row_texts)
                for prev_r in range(r_i - 1, -1, -1):
                    prev_cells = rows[prev_r].cells
                    for c_i in range(min(len(merged_hdr_texts), len(prev_cells))):
                        if not merged_hdr_texts[c_i] and prev_cells[c_i].text.strip():
                            merged_hdr_texts[c_i] = prev_cells[c_i].text.strip()
                detected = _detect_header_map(merged_hdr_texts)
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
            dur_i = col_map.get('duration')
            unit_i = col_map.get('unit')
            total_i = col_map['total']
            unit_time_unit = col_map.get('unit_time_unit')
            is_duration_priced_table = (dur_i is not None) or (unit_time_unit is not None)

            numeric_col_indices = {
                idx for idx in (qty_i, dur_i, unit_i, total_i) if idx is not None
            }

            seen_row_tcs = set()
            tbl_item_count = 0
            tbl_item_vals: list[Decimal] = []
            tbl_item_indices: list[int] = []
            tbl_item_objs: list[dict] = []
            tbl_subtotals: list[Decimal] = []
            tbl_grand_totals: list[Decimal] = []
            last_valid_name = ''

            for row in rows[header_idx + 1:]:
                cells = row.cells
                if max(name_i, total_i) >= len(cells):
                    continue

                unit_tc = cells[unit_i]._tc if (unit_i is not None and unit_i < len(cells)) else None
                tc_tuple = (cells[name_i]._tc, unit_tc, cells[total_i]._tc)
                if tc_tuple in seen_row_tcs:
                    continue
                seen_row_tcs.add(tc_tuple)

                unique_tcs = {c._tc for c in cells}
                row_full_text = ' '.join(dict.fromkeys(c.text.strip() for c in cells if c.text.strip()))

                if not _has_non_bid_context(row_full_text):
                    m_up = _UPPER_AMOUNT_RE.search(row_full_text)
                    if m_up and not item_table_uppers:
                        item_table_uppers.append(m_up.group(2).strip())

                first_non_empty = next((c.text.strip() for c in cells if c.text.strip()), '')
                name_cell_txt = cells[name_i].text.strip()
                is_prefix_summary = (
                    (
                        _SUMMARY_ROW_RE.match(first_non_empty)
                        or _SUMMARY_ROW_RE.match(name_cell_txt)
                        or any(_SUMMARY_ROW_RE.match(c.text.strip()) for c in cells[:2])
                    )
                    and not _has_independent_detail_arithmetic(cells, total_i, unit_i, qty_i, is_wan)
                )
                is_suffix_summary = (
                    not is_prefix_summary
                    and _is_suffix_summary_row(
                        name_cell_txt, row_full_text, cells, total_i, unit_i, is_wan, qty_i=qty_i
                    )
                )
                if is_prefix_summary or is_suffix_summary:
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

                item_name = _resolve_row_item_name(cells, name_i, numeric_col_indices, last_valid_name)
                if not item_name or _SEQ_ONLY_RE.match(item_name):
                    continue
                if name_cell_txt and not _SEQ_ONLY_RE.match(name_cell_txt):
                    last_valid_name = name_cell_txt

                raw_total_txt = cells[total_i].text if total_i < len(cells) else ''
                raw_unit_txt = cells[unit_i].text if (unit_i is not None and unit_i < len(cells)) else ''
                raw_qty_txt = cells[qty_i].text if (qty_i is not None and qty_i < len(cells)) else ''
                raw_dur_txt = cells[dur_i].text if (dur_i is not None and dur_i < len(cells)) else ''

                total_val = _parse_money_str(_strip_non_bid_clauses(raw_total_txt), default_wan=is_wan)
                unit_val = (_parse_money_str(_strip_non_bid_clauses(raw_unit_txt), default_wan=is_wan)
                            if unit_i is not None and unit_i < len(cells) else None)

                if total_val is None and unit_val is None:
                    continue

                parsed_qty = _parse_qty_str(raw_qty_txt) if qty_i is not None else Decimal('1')
                base_qty = parsed_qty if parsed_qty is not None else Decimal('1')

                skip_row_arithmetic = False
                effective_qty: Decimal | None = parsed_qty if qty_i is not None else Decimal('1')

                if is_duration_priced_table:
                    dur_source_txt = raw_dur_txt if dur_i is not None else raw_qty_txt
                    dur_factor = _parse_duration_factor(
                        dur_source_txt,
                        unit_time_unit=unit_time_unit,
                        base_qty=base_qty if dur_i is not None else Decimal('1'),
                        unit_val=unit_val,
                        total_val=total_val,
                    )
                    if dur_factor is not None:
                        if dur_i is not None:
                            effective_qty = base_qty * dur_factor
                        else:
                            effective_qty = dur_factor
                    else:
                        skip_row_arithmetic = True
                        effective_qty = base_qty
                        warnings.append({
                            'code': 'DURATION_UNPARSEABLE_SKIP',
                            'severity': 'warning',
                            'message': (
                                f"分项「{item_name}」采用期限定价但期限单元格（{dur_source_txt.strip() or '空'}）"
                                f"无法解析出有效时长，已跳过该行单价×数量算术校验，请人工复核"
                            ),
                        })
                elif effective_qty is None:
                    if total_val is not None and unit_val is not None and total_val == unit_val:
                        effective_qty = Decimal('1')
                    elif total_val is not None and unit_val is not None and unit_val > 0 and (total_val % unit_val == 0):
                        effective_qty = (total_val / unit_val).quantize(Decimal('1'))
                    elif total_val is not None and unit_val is None:
                        effective_qty = Decimal('1')

                if total_val is not None and unit_val is None and effective_qty is not None and effective_qty > 0:
                    unit_val = (total_val / effective_qty).quantize(CENT, rounding=ROUND_HALF_UP)

                if total_val is not None and unit_val is not None and effective_qty is not None:
                    if _is_bogus_price_row(
                        item_name=item_name,
                        qty_raw=raw_qty_txt or raw_dur_txt or '1',
                        unit_raw=raw_unit_txt or str(unit_val),
                        total_raw=raw_total_txt,
                        qty_val=effective_qty,
                        unit_val=unit_val,
                        total_val=total_val,
                    ):
                        continue
                    item_dict = {
                        'name': item_name,
                        'qty': str(effective_qty.normalize() if effective_qty == int(effective_qty) else effective_qty),
                        'unit_price': _fmt_yuan(unit_val),
                        'total_price': _fmt_yuan(total_val),
                    }
                    if skip_row_arithmetic:
                        item_dict['skip_arithmetic'] = True
                    items.append(item_dict)
                    tbl_item_vals.append(total_val)
                    tbl_item_indices.append(len(items) - 1)
                    tbl_item_objs.append(item_dict)
                    tbl_item_count += 1

            # E13：凡收集到有效分项行或汇总行的明细表均记入 detail_table_records，确保无合计行的双胞胎副本表也能被去重
            if tbl_item_count > 0 or tbl_subtotals or tbl_grand_totals:
                detail_table_records.append({
                    'tbl_el': tbl_el,
                    'item_count': tbl_item_count,
                    'subtotals': tbl_subtotals,
                    'grand_totals': tbl_grand_totals,
                    'item_vals': tbl_item_vals,
                    'item_indices': tbl_item_indices,
                    'item_objs': tbl_item_objs,
                    'has_qty_col': (qty_i is not None) or (dur_i is not None),
                    'has_unit_col': (unit_i is not None),
                })
            continue

        # ── 1.5 E8/E9 尝试作为《分项汇总表》或《无单价列的分表明细表》解析 ──
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
            sub_row_entries: list[tuple[str, Decimal]] = []
            sum_table_subtotals: list[Decimal] = []
            sum_table_grand_totals: list[Decimal] = []
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
                is_prefix_sum = (
                    _SUMMARY_ROW_RE.match(first_non_empty)
                    or _SUMMARY_ROW_RE.match(name_cell_txt)
                    or any(_SUMMARY_ROW_RE.match(c.text.strip()) for c in cells[:2])
                )
                is_suffix_sum = (
                    not is_prefix_sum
                    and _is_suffix_summary_row(name_cell_txt, row_full_text, cells, s_total_i, None, is_wan)
                )
                if is_prefix_sum or is_suffix_sum:
                    cand_val = _extract_summary_row_amount(cells, s_total_i, is_wan)
                    if cand_val is not None and cand_val >= Decimal('50'):
                        s_kind = _classify_summary_row_kind(first_non_empty, name_cell_txt, row_full_text)
                        if s_kind == 'subtotal':
                            sum_table_subtotals.append(cand_val)
                        else:
                            sum_table_grand_totals.append(cand_val)
                    continue

                if len({c._tc for c in cells}) == 1:
                    continue
                item_nm = _resolve_row_item_name(cells, s_name_i, {s_total_i}, '')
                if not item_nm or _SEQ_ONLY_RE.match(item_nm):
                    continue
                clean_nm = re.sub(r'\s+', '', item_nm)
                if clean_nm in _HEADER_NAME_BLACKLIST or any(bk in clean_nm for bk in _BID_TOTAL_LABEL_KEYS):
                    continue

                r_val = _parse_money_str(_strip_non_bid_clauses(cells[s_total_i].text), default_wan=is_wan)
                if r_val is not None and r_val >= Decimal('50'):
                    sub_row_entries.append((item_nm, r_val))

            if sub_row_entries and (sum_table_grand_totals or sum_table_subtotals):
                processed_table_els.add(tbl_el)
                has_price_table_skeleton = True
                if sum_table_subtotals and not sum_table_grand_totals:
                    s_vals: list[Decimal] = []
                    s_indices: list[int] = []
                    s_objs: list[dict] = []
                    for nm, val in sub_row_entries:
                        it_obj = {
                            'name': nm,
                            'qty': '1',
                            'unit_price': _fmt_yuan(val),
                            'total_price': _fmt_yuan(val),
                        }
                        items.append(it_obj)
                        s_vals.append(val)
                        s_indices.append(len(items) - 1)
                        s_objs.append(it_obj)
                    detail_table_records.append({
                        'tbl_el': tbl_el,
                        'item_count': len(sub_row_entries),
                        'subtotals': sum_table_subtotals,
                        'grand_totals': [],
                        'item_vals': s_vals,
                        'item_indices': s_indices,
                        'item_objs': s_objs,
                        'has_qty_col': False,
                        'has_unit_col': False,
                    })
                else:
                    summary_table_records.append({
                        'tbl_el': tbl_el,
                        'sub_rows_sum': sum((v for _, v in sub_row_entries), Decimal('0.00')),
                        'total_val': sum_table_grand_totals[-1],
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

    # ── 2.4 E13 新增：双胞胎明细表去重（滁州物业实锤：两张明细表分项金额序列/多重集完全一致）──
    _deduplicate_twin_detail_tables(items, detail_table_records, warnings)

    # ── 2.5 E9 回归修复：父级汇总表与子分表同级双收防重（滁州实锤）──
    for _pi, _rec in enumerate(list(detail_table_records)):
        _vals = sorted(_rec.get('item_vals') or [])
        if len(_vals) < 2:
            continue
        _others = sorted(
            v for r2 in detail_table_records if r2 is not _rec for v in (r2['subtotals'] or [])
        )
        if not _others or _vals != _others:
            continue
        for _idx in sorted(_rec.get('item_indices') or [], reverse=True):
            if 0 <= _idx < len(items):
                items.pop(_idx)
        summary_table_records.append({
            'tbl_el': _rec['tbl_el'],
            'sub_rows_sum': sum(_vals, Decimal('0.00')),
            'total_val': _rec['grand_totals'][-1] if _rec['grand_totals'] else sum(_vals, Decimal('0.00')),
        })
        detail_table_records.remove(_rec)
        break

    # ── 3. E8 核心：计算明细表聚合总价 (detail_agg_total) 与《分项汇总表》权威总价 ──
    records_with_totals = [
        r for r in detail_table_records if r.get('grand_totals') or r.get('subtotals')
    ]
    detail_agg_total: Decimal | None = None
    if len(records_with_totals) == 1:
        rec = records_with_totals[0]
        if rec['grand_totals']:
            detail_agg_total = rec['grand_totals'][-1]
        elif rec['subtotals']:
            detail_agg_total = rec['subtotals'][-1]
    elif len(records_with_totals) >= 2:
        full_grand_tables = [
            r for r in records_with_totals
            if r['grand_totals'] and r['item_count'] >= 2
        ]
        subtotal_only_tables = [
            r for r in records_with_totals
            if r['subtotals'] and not r['grand_totals']
        ]
        total_only_tables = [
            r for r in records_with_totals
            if r['grand_totals'] and r['item_count'] < 2
        ]
        if len(full_grand_tables) == 1 and not subtotal_only_tables:
            detail_agg_total = full_grand_tables[0]['grand_totals'][-1]
        elif subtotal_only_tables:
            acc = Decimal('0.00')
            for r in subtotal_only_tables:
                acc += r['subtotals'][-1]
            if acc > Decimal('0.00'):
                detail_agg_total = acc.quantize(CENT, rounding=ROUND_HALF_UP)
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
        elif len(full_grand_tables) >= 1:
            # 当存在 >=2 张含合计行且分项金额序列不一致的明细表（未通过双胞胎去重）时，
            # 取首张表合计作为基准，交由下游 PriceSandbox 触发 TOTAL_MISMATCH 报错
            detail_agg_total = full_grand_tables[0]['grand_totals'][-1]
        elif len(total_only_tables) >= 1:
            detail_agg_total = total_only_tables[0]['grand_totals'][-1]

    table_total: str | None = None
    if summary_table_records:
        auth_summary = summary_table_records[0]
        auth_total = auth_summary['total_val']
        table_total = _fmt_yuan(auth_total)

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
            r'^[ \t\u3000]*(?:[0-9]{1,3}(?:\.[0-9]{1,3})*[ \t\u3000]+)?([^\d\n\r]{2,30}?)[ \t\u3000]+'
            r'([0-9]+(?:\.[0-9]+)?)[ \t\u3000]*(?:[项套台个批年人月次份座宗件点路端式天日周季米公顷亩部辆页条卷张册盒袋箱千克瓶]{1,4})?[ \t\u3000]+'
            r'([0-9][0-9,]*(?:\.[0-9]+)?)[ \t\u3000]+'
            r'([0-9][0-9,]*(?:\.[0-9]+)?)[ \t\u3000]*$',
            re.MULTILINE
        )
        for m in line_item_re.finditer(text):
            name = m.group(1).strip()
            if _SUMMARY_ROW_RE.match(name):
                continue
            if _SUMMARY_SUFFIX_RE.search(name) and not any(k in name for k in _SUMMARY_SUFFIX_SERVICE_NOISE_KEYS):
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

    # E9 事故二：对标记了 skip_arithmetic=True（期限定价但期限无法解析）的行，豁免其单行 ITEM_ARITHMETIC 硬判
    skip_arith_names = {
        it.get('name') for it in (extracted.get('items') or []) if it.get('skip_arithmetic')
    }
    sandbox = PriceSandbox(
        items=extracted['items'],
        table_total=extracted['table_total'],
        bid_total=extracted['bid_total'],
        bid_total_upper=extracted['bid_total_upper'],
        price_cap=extracted['price_cap'],
    )
    sb_findings = sandbox.run()
    if skip_arith_names:
        sb_findings = [
            f for f in sb_findings
            if not (f.get('code') == 'ITEM_ARITHMETIC' and f.get('name') in skip_arith_names)
        ]
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