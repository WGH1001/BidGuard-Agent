# -*- coding: utf-8 -*-
"""F2 报价算术沙盒 —— 0 Token 确定性核验引擎（与表格解析完全解耦）。

设计要点（对应 Gemini 开发指令）：
1. **核心与解析解耦**：PriceSandbox 只吃结构化数据（分项列表 + 合计 + 开标
   一览表总价 + 大写 + 最高限价），表格提取失败时由上层 API 走人工框选回填，
   本模块永不接触原始文档；
2. **四大核验点**：分项算术（单价×数量==合价）、汇总一致（∑分项==表合计==
   开标总价）、大小写严格一致、最高限价；
3. **Decimal 全程**：金额计算禁止 float，容差 0.01 元（差 1 分钱必须报警）；
4. **人民币大写**：按《正确填写票据和结算凭证》规范生成（壹拾起头、节内零
   合并、跨节零、角分/整），比对时归一化"正/整"与空白（WPS/Word 书写变体）。

验收线：差 1 分钱、大写错 1 字必须 100% 报警。
"""
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Optional

CENT = Decimal('0.01')
_WAN = Decimal('10000')
_CN_DIGITS = '零壹贰叁肆伍陆柒捌玖'
_CN_UNITS = ('', '拾', '佰', '仟')
_CN_SECTIONS = ('', '万', '亿', '兆')

_EMPTY_ANCHOR_TOKENS = frozenset({
    '', '/', '-', '—', '--', '…', '...', '……', '无', 'null', 'none', 'n/a', 'na', '待定', '未填写',
})
_CN_SIMPLE_QTY_MAP: dict[str, Decimal] = {
    '一': Decimal('1'),
    '二': Decimal('2'),
    '两': Decimal('2'),
    '三': Decimal('3'),
    '四': Decimal('4'),
    '五': Decimal('5'),
    '六': Decimal('6'),
    '七': Decimal('7'),
    '八': Decimal('8'),
    '九': Decimal('9'),
    '十': Decimal('10'),
    '半': Decimal('0.5'),
}
_SANDBOX_QTY_UNIT_RE = re.compile(
    r'^([0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)\s*'
    r'(?:系统|节点|点位|[项套台个批年人月次份座宗件点路端式天日周季米公顷亩部辆页条卷张册盒袋箱千克瓶期/·\-]{1,6}|[a-zA-Z]{1,6})?$'
)
_SANDBOX_CN_QTY_RE = re.compile(
    r'^([一二三四五六七八九十两半])\s*(?:系统|节点|点位|年|个?月|项|套|台|个|批|页|条|卷|张|册|盒|袋|箱|瓶|次)?$'
)


def _parse_sandbox_amount(val: Any, *, allow_negative: bool = False) -> Optional[Decimal]:
    """零外部依赖的金额安全解析器（绝不反向导入 price_extractor，防循环依赖）。

    支持 int / float / Decimal 以及带千分位逗号、货币前缀（￥/¥/人民币/小写）、
    单位后缀（万元/万自动×10000、元/圆/整/正）的字符串；占位符与无效串返回 None。
    """
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, Decimal):
        if not val.is_finite():
            return None
        if not allow_negative and val < 0:
            return None
        return val.quantize(CENT, rounding=ROUND_HALF_UP)
    if isinstance(val, (int, float)):
        try:
            d = Decimal(str(val))
        except InvalidOperation:
            return None
        if not d.is_finite():
            return None
        if not allow_negative and d < 0:
            return None
        return d.quantize(CENT, rounding=ROUND_HALF_UP)

    s = str(val).strip()
    if not s or s.lower() in _EMPTY_ANCHOR_TOKENS or '…' in s or '...' in s:
        return None

    compact = s.replace(' ', '').replace('　', '')
    is_wan = ('万元' in compact) or ('万圆' in compact) or bool(re.search(r'[0-9]\s*万(?![0-9])', compact))

    # 剥离常见货币前缀（人民币/小写/符号）与尾部单位/正整后缀
    cleaned = re.sub(r'^(?:[（(]?(?:人民币|小写|rmb|cny)[）)]?|[￥¥：:])+', '', compact, flags=re.IGNORECASE)
    cleaned = re.sub(r'(?:[万元圆整正]+)$', '', cleaned)
    if not cleaned:
        return None

    if not re.fullmatch(r'[+-]?(?:[0-9]+(?:,[0-9]+)*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?', cleaned):
        return None

    try:
        d = Decimal(cleaned.replace(',', ''))
    except InvalidOperation:
        return None
    if not d.is_finite():
        return None
    if not allow_negative and d < 0:
        return None
    if is_wan:
        d = d * _WAN
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def _parse_sandbox_qty(val: Any) -> Optional[Decimal]:
    """零外部依赖的数量安全解析器：清洗逗号、量词后缀（1套/2台/3项等）与简单中文数字。"""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, Decimal):
        return val if (val.is_finite() and val >= 0) else None
    if isinstance(val, (int, float)):
        try:
            d = Decimal(str(val))
        except InvalidOperation:
            return None
        return d if (d.is_finite() and d >= 0) else None

    s = str(val).strip().replace(' ', '').replace('　', '').replace(',', '')
    if not s or s.lower() in _EMPTY_ANCHOR_TOKENS or '…' in s or '...' in s:
        return None
    # 拒绝把带货币单位的金额误解析为数量
    if '万元' in s or '元' in s or '圆' in s or '￥' in s or '¥' in s:
        return None

    m = _SANDBOX_QTY_UNIT_RE.match(s)
    if m:
        try:
            d = Decimal(m.group(1))
            return d if (d.is_finite() and d >= 0) else None
        except InvalidOperation:
            return None

    m_cn = _SANDBOX_CN_QTY_RE.match(s)
    if m_cn and m_cn.group(1) in _CN_SIMPLE_QTY_MAP:
        return _CN_SIMPLE_QTY_MAP[m_cn.group(1)]
    return None


def _four_to_cn(n: int) -> str:
    """0-9999 转中文大写（节内零合并：1030→壹仟零叁拾，1000→壹仟，10→壹拾）。"""
    s = ''
    pending_zero = False
    for pos in (3, 2, 1, 0):
        d = (n // 10 ** pos) % 10
        if d:
            if pending_zero:
                s += '零'
                pending_zero = False
            s += _CN_DIGITS[d] + _CN_UNITS[pos]
        elif s:
            pending_zero = True
    return s


def amount_to_upper(amount) -> str:
    """人民币金额 → 规范大写。

    边缘覆盖：整（无角分）/角/分、壹拾起头、节内零（1030→壹仟零叁拾）、
    跨节零（100000001→壹亿零壹元）、节首补零（10005000→壹仟万零伍仟）、
    纯角分（0.05→伍分）、壹角无分（1.50→壹元伍角）。
    """
    d = _parse_sandbox_amount(amount, allow_negative=True)
    if d is None:
        raise ValueError(f"无法将金额「{amount}」解析为有效十进制数值")
    neg = d < 0
    d = -d if neg else d
    yuan = int(d)
    jiao = int(d * 10) % 10
    fen = int(d * 100) % 10

    if yuan == 0:
        int_part = ''
    else:
        sections = []                          # 低位起每 4 位一组
        v = yuan
        while v:
            sections.append(v % 10000)
            v //= 10000
        parts = []
        zero_pending = False
        last_sec = None
        for i in range(len(sections) - 1, -1, -1):
            sec = sections[i]
            if sec == 0:
                zero_pending = True
                continue
            if parts and (zero_pending or sec < 1000 or last_sec % 1000 == 0):
                # 跨节零三形态：中间节全零 / 低节千位为零 / 高节末尾全零
                # （10005000 → 壹仟万零伍仟；100000001 → 壹亿零壹元）
                parts.append('零')
            zero_pending = False
            parts.append(_four_to_cn(sec) + _CN_SECTIONS[i])
            last_sec = sec
        int_part = ''.join(parts)

    out = ('负' if neg else '') + (int_part + '元' if int_part else '')
    if jiao == 0 and fen == 0:
        return out + ('整' if int_part else '零元整')
    if jiao:
        out += _CN_DIGITS[jiao] + '角'
    if fen:
        if jiao == 0 and int_part:
            out += '零'                        # 1.05 → 壹元零伍分
        out += _CN_DIGITS[fen] + '分'
    return out


def _normalize_upper(text: str) -> str:
    """大写金额归一化：去空白、"（大写）"标注前缀、货币符号、"正"→"整"、"圆"→"元"、以"元"结尾省写"整"时补全"整"。"""
    t = (text or '').strip().replace(' ', '').replace('　', '')
    t = re.sub(r'^[（(]?\s*大写\s*[）)]?', '', t)
    t = t.lstrip('￥¥人民币：:').rstrip('）)')
    t = t.replace('正', '整').replace('圆', '元')
    if t.endswith('元'):
        t = t + '整'
    return t


def _to_decimal(v) -> Decimal:
    d = _parse_sandbox_amount(v, allow_negative=True)
    if d is None:
        raise ValueError(f"无法将金额「{v}」解析为有效十进制数值")
    return d


class PriceSandbox:
    """报价核验沙盒。所有输入均为结构化数据（金额单位：元）。

    Args:
        items: 分项列表 [{'name': str, 'qty': num, 'unit_price': num,
               'total_price': num}, ...]，可为空（只核总价/大写/限价）。
        table_total: 分项报价明细表的"合计"行金额（无则 None）。
        bid_total: 开标一览表投标总报价（数字）。
        bid_total_upper: 开标一览表投标总报价（大写）。
        price_cap: 招标文件最高限价（无则跳过核验点 4）。
    """

    def __init__(self, items=None, table_total=None, bid_total=None,
                 bid_total_upper=None, price_cap=None):
        self.items = items or []
        self.table_total = table_total
        self.bid_total = bid_total
        self.bid_total_upper = bid_total_upper
        self.price_cap = price_cap

    def _finding(self, code, message, **detail) -> dict:
        f = {'code': code, 'severity': 'error', 'message': message}
        f.update(detail)
        return f

    def _resolve_anchor(
        self,
        raw_val: Any,
        field_key: str,
        label: str,
        findings: list[dict],
    ) -> tuple[Optional[Decimal], bool]:
        """安全解析汇总/限价锚点：占位符视作未提供；格式异常记录 INVALID_PRICE_FORMAT 且绝不抛异常。"""
        if raw_val is None:
            return None, False
        if isinstance(raw_val, str) and raw_val.strip().lower() in _EMPTY_ANCHOR_TOKENS:
            return None, False
        parsed = _parse_sandbox_amount(raw_val)
        if parsed is None:
            findings.append(self._finding(
                'INVALID_PRICE_FORMAT',
                f"{label}「{raw_val}」格式无法解析为有效金额",
                field=field_key,
                actual=str(raw_val),
            ))
            return None, True
        return parsed, False

    def run(self) -> list:
        """执行四大核验，返回 findings 列表；空列表 = 全部通过。"""
        findings: list[dict] = []
        sum_of_items = Decimal('0.00')
        has_unparseable_item_total = False

        # 核验点 1：分项算术 单价×数量 == 合价（容差 0.01）
        for idx, item in enumerate(self.items, 1):
            if not isinstance(item, dict):
                findings.append(self._finding(
                    'INVALID_PRICE_FORMAT',
                    f"分项[{idx}]格式错误：应为字典结构，当前为 {type(item).__name__}",
                    index=idx,
                    field='item',
                    actual=str(item),
                ))
                has_unparseable_item_total = True
                continue

            item_name = str(item.get('name', '') or '')
            raw_qty = item.get('qty', 1)
            if raw_qty is None or (isinstance(raw_qty, str) and raw_qty.strip() == ''):
                qty: Optional[Decimal] = Decimal('1')
            else:
                qty = _parse_sandbox_qty(raw_qty)

            raw_unit = item.get('unit_price')
            unit = _parse_sandbox_amount(raw_unit)

            raw_total = item.get('total_price')
            actual = _parse_sandbox_amount(raw_total)

            if actual is not None:
                sum_of_items += actual
            else:
                has_unparseable_item_total = True

            bad_fields: list[str] = []
            if qty is None:
                bad_fields.append(f"数量(qty={raw_qty!r})")
            if unit is None:
                bad_fields.append(f"单价(unit_price={raw_unit!r})")
            if actual is None:
                bad_fields.append(f"合价(total_price={raw_total!r})")

            if bad_fields:
                findings.append(self._finding(
                    'INVALID_PRICE_FORMAT',
                    f"分项[{idx} {item_name}]格式无法解析：{'、'.join(bad_fields)}无法转为有效数值",
                    index=idx,
                    name=item_name,
                    field='item',
                    bad_fields=bad_fields,
                ))
                continue

            expected = (qty * unit).quantize(CENT, rounding=ROUND_HALF_UP)
            if abs(actual - expected) >= CENT:
                findings.append(self._finding(
                    'ITEM_ARITHMETIC',
                    f"分项[{idx} {item_name}]算术错误：单价×数量="
                    f"{expected}，填写合价={actual}，差 {actual - expected}",
                    index=idx, name=item_name,
                    expected=str(expected), actual=str(actual),
                    diff=str((actual - expected).quantize(CENT))))

        # 核验点 2：∑分项合价 == 表合计 == 开标一览表总价
        anchor, anchor_invalid = self._resolve_anchor(
            self.table_total, 'table_total', '分项报价明细表合计', findings
        )
        bid_val, bid_invalid = self._resolve_anchor(
            self.bid_total, 'bid_total', '开标一览表总报价', findings
        )
        cap_val, _ = self._resolve_anchor(
            self.price_cap, 'price_cap', '最高限价', findings
        )

        if anchor is not None and not has_unparseable_item_total:
            if abs(sum_of_items - anchor) >= CENT:
                findings.append(self._finding(
                    'TOTAL_MISMATCH',
                    f"分项合价累加={sum_of_items}，与明细表合计 {anchor} 不一致，"
                    f"差 {sum_of_items - anchor}",
                    expected=str(sum_of_items), actual=str(anchor),
                    diff=str((sum_of_items - anchor).quantize(CENT))))

        if bid_val is not None and (self.items or anchor is not None):
            # 仅给开标总价（无分项/表合计可比）时跳过汇总核验，避免 ∑=0 误报
            if anchor is not None or not has_unparseable_item_total:
                ref = anchor if anchor is not None else sum_of_items
                if abs(ref - bid_val) >= CENT:
                    code = 'BID_TOTAL_MISMATCH' if anchor is not None else 'TOTAL_MISMATCH'
                    findings.append(self._finding(
                        code,
                        f"分项报价{'表合计' if anchor is not None else '累加'}={ref}，"
                        f"与开标一览表总报价 {bid_val} 不一致，差 {ref - bid_val}",
                        expected=str(ref), actual=str(bid_val),
                        diff=str((ref - bid_val).quantize(CENT))))

        # 核验点 3：大小写严格一致（数字 → 规范大写 → 归一化比对）
        if bid_val is not None and self.bid_total_upper:
            got = _normalize_upper(self.bid_total_upper)
            if got:
                expect_upper = amount_to_upper(bid_val)
                if got != expect_upper:
                    findings.append(self._finding(
                        'UPPER_MISMATCH',
                        f"大写金额与数字不一致：数字 {self.bid_total} 的规范大写应为"
                        f"「{expect_upper}」，标书填写「{self.bid_total_upper}」",
                        expected=expect_upper, actual=self.bid_total_upper))

        # 核验点 4：最高限价（投标总价 ≤ 限价通过；恰等限价通过，超 1 分报警）
        if cap_val is not None and not bid_invalid:
            if bid_val is not None:
                bid_or_sum: Optional[Decimal] = bid_val
            elif not anchor_invalid:
                if anchor is not None:
                    bid_or_sum = anchor
                elif self.items and not has_unparseable_item_total:
                    bid_or_sum = sum_of_items
                else:
                    bid_or_sum = None
            else:
                bid_or_sum = None

            if bid_or_sum is not None and bid_or_sum > cap_val:
                findings.append(self._finding(
                    'CAP_EXCEEDED',
                    f"投标总价 {bid_or_sum} 超过最高限价 {cap_val}，超 {bid_or_sum - cap_val}",
                    cap=str(cap_val), actual=str(bid_or_sum),
                    diff=str((bid_or_sum - cap_val).quantize(CENT))))
        return findings
