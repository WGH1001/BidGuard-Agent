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
    r'[^0-9\n]{0,25}?([0-9][0-9,]*(?:\.[0-9]+)?)',
    re.IGNORECASE
)
_CAP_BUDGET_RE = re.compile(
    r'(?:采购预算|预算金额|项目预算|预算总价|最高预算)'
    r'[^0-9\n]{0,45}?'
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
    r'\s*(?:[（(][^）)\n]{0,12}[）)])?\s*[：:]\s*'
    r'(?:小写[：:\s]*|人民币\s*|[￥¥]\s*)?'
    r'([0-9][0-9,]*(?:\.[0-9]+)?)\s*(万元|元)'
)

# 表头列角色关键词
_COL_NAME_KEYS = ('品目名称', '货物名称', '服务名称', '分项名称', '项目名称', '系统名称',
                  '模块名称', '设备名称', '标的物', '名称', '品目', '内容', '项目')
_COL_QTY_KEYS = ('数量', '采购数量', '投标数量', '工作量', '套数', '台数')
_COL_UNIT_PRICE_KEYS = ('单价', '投标单价', '含税单价', '综合单价', '全费用单价')
_COL_TOTAL_PRICE_KEYS = ('合价', '分项合计', '分项总价', '投标合价', '含税总价',
                         '含税合价', '小计', '总价', '金额')

# 汇总行识别
_SUMMARY_ROW_RE = re.compile(
    r'^\s*(?:合\s*计|总\s*计|报价合计|投标总价|投标总报价|总报价|分项合计|小\s*计|总金额|大\s*写)',
    re.IGNORECASE
)
# 纯序号判定
_SEQ_ONLY_RE = re.compile(r'^\s*(?:[0-9]{1,3}|[一二三四五六七八九十]+|[（(][0-9一二三四五六七八九十]+[)）])\s*$')
# 说明性长句排除词（防把招标须知/评分说明当成报价数值）
_INSTRUCTION_NOISE_KEYS = ('不得超过', '超过最高限价', '无效标', '废标', '得分', '评标基准价',
                           '计算公式', '保留两位小数', '满分为', '扣完为止', '保证金')
# 非投标报价的大写上下文排除词（防把最高限价/预算/保证金的大写误提为 bid_total_upper）
_NON_BID_UPPER_KEYS = ('最高限价', '最高投标限价', '采购预算', '预算金额', '控制价', '保证金')


def _fmt_yuan(val: Decimal) -> str:
    return str(val.quantize(CENT, rounding=ROUND_HALF_UP))


def _parse_money_str(raw: str, default_wan: bool = False,
                     require_unit: bool = False) -> Decimal | None:
    """从单元格或文本片段解析金额并统一转为元；非有效数字返回 None。

    require_unit=True 用于**自由文本语境**（邻格说明文字、标签后随文）：
    数字必须紧邻 元/万元/￥ 才采信——防止"第9条要求""评标基准价×30分"等
    说明性数字被当成金额（滁州托育实测：'第9条' 被解析成 9.00 元）。
    """
    if not raw:
        return None
    s = str(raw).strip()
    if not s or s in ('/', '-', '—', '--', '无', '详见', '投标人填报'):
        return None
    if any(k in s for k in _INSTRUCTION_NOISE_KEYS):
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

    优先级：最高限价 / 控制价 > 表头带单位限价 > 采购预算 / 预算金额。
    """
    if not text:
        return None

    for regex in (_CAP_HIGH_RE, _CAP_UNIT_HEADER_RE, _CAP_BUDGET_RE):
        for m in regex.finditer(text):
            g1, g2 = m.group(1), m.group(2)
            if g1 in ('万元', '万', '元'):
                unit, num_s = g1, g2
            else:
                num_s, unit = g1, g2
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
    return None


def _detect_header_map(cells_text: list[str]) -> dict | None:
    """判断一行是否为分项报价表头，若是则返回 {'name': idx, 'qty': idx, 'unit': idx, 'total': idx, 'is_wan': bool}。"""
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


def _extract_from_docx_tables(doc) -> dict:
    """遍历 python-docx 的所有表格，提取开标一览表与分项报价明细表数据。"""
    items = []
    table_total = None
    bid_total = None
    bid_total_upper = None
    price_cap_in_table = None
    has_price_table_skeleton = False

    for table in doc.tables:
        rows = table.rows
        if not rows:
            continue

        first_row_txt = ' '.join(c.text.strip() for c in rows[0].cells)
        table_is_wan = ('万元' in first_row_txt)

        # ── 1. 尝试作为《分项报价明细表》解析（查找表头行） ──
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
            is_wan = col_map['is_wan']
            name_i = col_map['name']
            qty_i = col_map.get('qty')
            unit_i = col_map.get('unit')
            total_i = col_map['total']

            seen_row_tcs = set()
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

                if not any(k in row_full_text for k in _NON_BID_UPPER_KEYS):
                    m_up = _UPPER_AMOUNT_RE.search(row_full_text)
                    if m_up and not bid_total_upper:
                        bid_total_upper = m_up.group(2).strip()

                first_non_empty = next((c.text.strip() for c in cells if c.text.strip()), '')
                name_cell_txt = cells[name_i].text.strip()
                if (_SUMMARY_ROW_RE.match(first_non_empty)
                        or _SUMMARY_ROW_RE.match(name_cell_txt)
                        or any(_SUMMARY_ROW_RE.match(c.text.strip()) for c in cells[:2])):
                    cand_val = _parse_money_str(cells[total_i].text, default_wan=is_wan)
                    if cand_val is None:
                        for c in reversed(cells):
                            cand_val = _parse_money_str(c.text, default_wan=is_wan)
                            if cand_val is not None:
                                break
                    if cand_val is not None and cand_val > 0:
                        if table_total is None or '小计' not in row_full_text:
                            table_total = _fmt_yuan(cand_val)
                    continue

                if len(unique_tcs) == 1:
                    continue

                item_name = name_cell_txt
                if not item_name or _SEQ_ONLY_RE.match(item_name):
                    continue

                total_val = _parse_money_str(cells[total_i].text, default_wan=is_wan)
                unit_val = (_parse_money_str(cells[unit_i].text, default_wan=is_wan)
                            if unit_i is not None and unit_i < len(cells) else None)
                qty_val = (_parse_qty_str(cells[qty_i].text)
                           if qty_i is not None and qty_i < len(cells) else Decimal('1'))

                if total_val is None and unit_val is None:
                    continue
                if total_val is not None and unit_val is not None and qty_val is not None:
                    items.append({
                        'name': item_name,
                        'qty': str(qty_val),
                        'unit_price': _fmt_yuan(unit_val),
                        'total_price': _fmt_yuan(total_val),
                    })
            continue

        # ── 2. 尝试作为《开标一览表》或《投标人须知前附表》解析 ──
        header_cells = [re.sub(r'\s+', '', c.text) for c in rows[0].cells]
        bid_col = None
        upper_col = None
        for idx, h in enumerate(header_cells):
            if len(h) <= 20 and any(k in h for k in ('投标总价', '投标报价', '投标总报价', '总报价')):
                if '大写' in h:
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
                v = _parse_money_str(cell_txt, default_wan=('万元' in header_cells[bid_col]))
                if v is not None and v > 0 and bid_total is None:
                    bid_total = _fmt_yuan(v)
                m_u = _UPPER_AMOUNT_RE.search(cell_txt)
                if m_u and not bid_total_upper:
                    bid_total_upper = m_u.group(2).strip()
            if upper_col is not None and upper_col < len(row1) and not bid_total_upper:
                m_u = _UPPER_AMOUNT_RE.search(row1[upper_col].text)
                if m_u:
                    bid_total_upper = m_u.group(2).strip()

        for row in rows:
            uniq_cells = []
            seen_tc = set()
            for c in row.cells:
                if c._tc not in seen_tc:
                    seen_tc.add(c._tc)
                    uniq_cells.append(c.text.strip())
            if not uniq_cells:
                continue
            row_joined = ' '.join(uniq_cells)
            if any(k in row_joined for k in ('开标一览表', '报价一览表', '投标总价', '投标总报价')):
                has_price_table_skeleton = True

            for i, label in enumerate(uniq_cells):
                clean_lbl = re.sub(r'\s+', '', label)
                if len(clean_lbl) > 30:
                    continue
                if any(k in clean_lbl for k in ('最高限价', '最高投标限价', '招标控制价', '采购预算', '预算金额')):
                    target_txt = uniq_cells[i + 1] if i + 1 < len(uniq_cells) else label
                    v_cap = _parse_money_str(target_txt,
                                             default_wan=('万元' in clean_lbl or '万元' in target_txt),
                                             require_unit=True)
                    if v_cap is not None and v_cap >= Decimal('100') and price_cap_in_table is None:
                        price_cap_in_table = _fmt_yuan(v_cap)

                if any(k in clean_lbl for k in ('投标总价', '投标总报价', '报价总计', '总报价', '投标报价')):
                    if any(k in clean_lbl for k in _NON_BID_UPPER_KEYS):
                        continue
                    if i + 1 >= len(uniq_cells) and '：' not in clean_lbl and ':' not in clean_lbl:
                        continue
                    val_part = ' '.join(uniq_cells[i + 1:]) if i + 1 < len(uniq_cells) else label
                    m_u = _UPPER_AMOUNT_RE.search(val_part)
                    if m_u and not bid_total_upper:
                        bid_total_upper = m_u.group(2).strip()
                    if '大写' not in clean_lbl or '小写' in val_part:
                        v_bid = _parse_money_str(val_part,
                                                 default_wan=('万元' in clean_lbl),
                                                 require_unit=True)
                        if v_bid is not None and v_bid > 0 and bid_total is None:
                            bid_total = _fmt_yuan(v_bid)

                elif '大写' in clean_lbl and not any(k in clean_lbl for k in _NON_BID_UPPER_KEYS) and not bid_total_upper:
                    val_part = uniq_cells[i + 1] if i + 1 < len(uniq_cells) else label
                    m_u = _UPPER_AMOUNT_RE.search(val_part)
                    if m_u:
                        bid_total_upper = m_u.group(2).strip()

    return {
        'items': items,
        'table_total': table_total,
        'bid_total': bid_total,
        'bid_total_upper': bid_total_upper,
        'price_cap': price_cap_in_table,
        'has_price_table_skeleton': has_price_table_skeleton,
    }


def extract_price_from_text(text: str) -> dict:
    """从纯文本（或 PDF 解析出的文本行）提取报价要素。"""
    price_cap = extract_price_cap(text)
    bid_total = None
    bid_total_upper = None
    table_total = None
    items = []

    if text:
        for m_bid in _BID_TOTAL_LINE_RE.finditer(text):
            line_ctx = m_bid.group(0)
            if any(k in line_ctx for k in _INSTRUCTION_NOISE_KEYS):
                continue
            num_s, unit = m_bid.group(1), (m_bid.group(2) or '元')
            v = _parse_money_str(num_s + unit)
            if v is not None and v > 0:
                bid_total = _fmt_yuan(v)
                break

        # 逐行扫描投标大写金额（排除最高限价/预算/保证金所在行的大写金额）
        for line in text.splitlines():
            if any(k in line for k in _NON_BID_UPPER_KEYS):
                continue
            if any(k in line for k in ('投标', '报价', '总价', '合计', '大写')):
                m_up = _UPPER_AMOUNT_RE.search(line)
                if m_up:
                    bid_total_upper = m_up.group(2).strip()
                    break

        line_item_re = re.compile(
            r'^\s*(?:[0-9]{1,2}\s+)?([^\d\n\r]{2,30}?)\s+'
            r'([0-9]+(?:\.[0-9]+)?)\s*[项套台个批年]?\s+'
            r'([0-9][0-9,]*(?:\.[0-9]+)?)\s+'
            r'([0-9][0-9,]*(?:\.[0-9]+)?)\s*$',
            re.MULTILINE
        )
        for m in line_item_re.finditer(text):
            name = m.group(1).strip()
            if _SUMMARY_ROW_RE.match(name):
                continue
            qty = _parse_qty_str(m.group(2))
            u_p = _parse_money_str(m.group(3))
            t_p = _parse_money_str(m.group(4))
            if qty and u_p is not None and t_p is not None:
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
    }

    if path.suffix.lower() == '.docx':
        from docx import Document
        doc = Document(str(path))
        tbl_res = _extract_from_docx_tables(doc)
        res['has_price_table_skeleton'] = tbl_res['has_price_table_skeleton']
        if tbl_res['items']:
            res['items'] = tbl_res['items']
        if tbl_res['table_total'] is not None:
            res['table_total'] = tbl_res['table_total']
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
    """端到端辅助入口：提取报价结构 -> 若 status=='OK' 则送入 PriceSandbox 核验。"""
    extracted = extract_price_data(bid_path, tender_path=tender_path)
    if extracted['status'] != 'OK':
        extracted['findings'] = []
        return extracted
    sandbox = PriceSandbox(
        items=extracted['items'],
        table_total=extracted['table_total'],
        bid_total=extracted['bid_total'],
        bid_total_upper=extracted['bid_total_upper'],
        price_cap=extracted['price_cap'],
    )
    extracted['findings'] = sandbox.run()
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