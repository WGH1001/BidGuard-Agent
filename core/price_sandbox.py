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
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal('0.01')
_CN_DIGITS = '零壹贰叁肆伍陆柒捌玖'
_CN_UNITS = ('', '拾', '佰', '仟')
_CN_SECTIONS = ('', '万', '亿', '兆')


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
    d = Decimal(str(amount)).quantize(CENT, rounding=ROUND_HALF_UP)
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
    """大写金额归一化：去空白、"（大写）"标注前缀、货币符号、"正"→"整"。"""
    t = (text or '').strip().replace(' ', '').replace('　', '')
    t = re.sub(r'^[（(]?\s*大写\s*[）)]?', '', t)
    t = t.lstrip('￥¥人民币：:').rstrip('）)')
    return t.replace('正', '整')


def _to_decimal(v) -> Decimal:
    return Decimal(str(v)).quantize(CENT, rounding=ROUND_HALF_UP)


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

    def run(self) -> list:
        """执行四大核验，返回 findings 列表；空列表 = 全部通过。"""
        findings = []
        sum_of_items = sum((_to_decimal(i.get('total_price', 0)) for i in self.items),
                           Decimal('0'))

        # 核验点 1：分项算术 单价×数量 == 合价（容差 0.01）
        for idx, item in enumerate(self.items, 1):
            qty = _to_decimal(item.get('qty', 0))
            unit = _to_decimal(item.get('unit_price', 0))
            actual = _to_decimal(item.get('total_price', 0))
            expected = (qty * unit).quantize(CENT, rounding=ROUND_HALF_UP)
            if abs(actual - expected) >= CENT:
                findings.append(self._finding(
                    'ITEM_ARITHMETIC',
                    f"分项[{idx} {item.get('name', '')}]算术错误：单价×数量="
                    f"{expected}，填写合价={actual}，差 {actual - expected}",
                    index=idx, name=item.get('name', ''),
                    expected=str(expected), actual=str(actual),
                    diff=str((actual - expected).quantize(CENT))))

        # 核验点 2：∑分项合价 == 表合计 == 开标一览表总价
        anchor = None
        if self.table_total is not None:
            anchor = _to_decimal(self.table_total)
            if abs(sum_of_items - anchor) >= CENT:
                findings.append(self._finding(
                    'TOTAL_MISMATCH',
                    f"分项合价累加={sum_of_items}，与明细表合计 {anchor} 不一致，"
                    f"差 {sum_of_items - anchor}",
                    expected=str(sum_of_items), actual=str(anchor),
                    diff=str((sum_of_items - anchor).quantize(CENT))))
        if self.bid_total is not None and (self.items or self.table_total is not None):
            # 仅给开标总价（无分项/表合计可比）时跳过汇总核验，避免 ∑=0 误报
            bid = _to_decimal(self.bid_total)
            ref = anchor if anchor is not None else sum_of_items
            if abs(ref - bid) >= CENT:
                code = 'BID_TOTAL_MISMATCH' if anchor is not None else 'TOTAL_MISMATCH'
                findings.append(self._finding(
                    code,
                    f"分项报价{'表合计' if anchor is not None else '累加'}={ref}，"
                    f"与开标一览表总报价 {bid} 不一致，差 {ref - bid}",
                    expected=str(ref), actual=str(bid),
                    diff=str((ref - bid).quantize(CENT))))

        # 核验点 3：大小写严格一致（数字 → 规范大写 → 归一化比对）
        if self.bid_total is not None and self.bid_total_upper:
            expect_upper = amount_to_upper(self.bid_total)
            got = _normalize_upper(self.bid_total_upper)
            if got != expect_upper:
                findings.append(self._finding(
                    'UPPER_MISMATCH',
                    f"大写金额与数字不一致：数字 {self.bid_total} 的规范大写应为"
                    f"「{expect_upper}」，标书填写「{self.bid_total_upper}」",
                    expected=expect_upper, actual=self.bid_total_upper))

        # 核验点 4：最高限价（投标总价 ≤ 限价通过；恰等限价通过，超 1 分报警）
        if self.price_cap is not None:
            cap = _to_decimal(self.price_cap)
            bid_or_sum = (_to_decimal(self.bid_total)
                          if self.bid_total is not None else sum_of_items)
            if bid_or_sum > cap:
                findings.append(self._finding(
                    'CAP_EXCEEDED',
                    f"投标总价 {bid_or_sum} 超过最高限价 {cap}，超 {bid_or_sum - cap}",
                    cap=str(cap), actual=str(bid_or_sum),
                    diff=str((bid_or_sum - cap).quantize(CENT))))
        return findings
