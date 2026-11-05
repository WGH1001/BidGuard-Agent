# -*- coding: utf-8 -*-
"""F6 投前控标雷达（★大模型④）—— 招标文件排他性/倾向性参数扫描引擎。

模块定位（对应 PRD-02 §7 F6 与 F6+F7 任务卡）：
1. 双层架构（与 F1 同款）：
   - 确定性层（0 Token）：内置排他特征词正则库（品牌绑定/特定型号/原厂唯一授权/特定资质/
     地域或同名业绩定向/毫米级非标参数定向）+ 常见 IT 品牌与型号模式；
     严格执行保守原则：国产化/自主可控/信创/等保/国密/节能品目清单等合法政策导向绝不误报。
   - LLM 层（★大模型④）：只吃确定性层圈出的局部候选窗口（不整本投喂），逐段判定
     is_exclusive + exclusive_type（品牌绑定/资质定向/业绩定向/参数定向/无）+ reason；
     所有输出 quote 强制通过 verify_quote 溯源校验，编造即丢弃。
2. 合规边界（PRD 硬性要求）：
   - 所有检出条目均带"疑似"字样标注；
   - conclusion.recommendation（'值得投'|'谨慎投'|'疑似控标高'）明确声明仅作投前参考意见，不替决策拍板；
   - client=None 时 0 Token 降级，直接输出确定性层结果并标 llm_status='skipped'。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from core.parser import parse_document
from core.quote_verifier import verify_quote

_WINDOW_BEFORE = 80
_WINDOW_AFTER = 220
_JSON_BLOCK_RE = re.compile(r'\{.*\}', re.DOTALL)

# ── 1. 合法政策导向白名单（当句子仅涉及合法国产化/信创/通用国标且无品牌型号指定时免检）──
_LEGITIMATE_POLICY_RE = re.compile(
    r'国产化|自主可控|信创|信息技术应用创新|国产操作系统|国产数据库|国产中间件|国产芯片'
    r'|银河麒麟|统信\s*UOS|达梦|人大金仓|南大通用|鲲鹏|海光|飞腾|龙芯|申威'
    r'|国密算法|SM[234]|网络安全等级保护|等保[二三]级|节能产品政府采购品目清单|环境标志产品'
    r'|中小企业声明函|监狱企业|残疾人福利性单位'
)

# ── 2. 确定性排他特征词正则库（四大类）──────────────────────────────────────

# (1) 品牌绑定 / 特定型号指定
# 注意：品牌词尾部触发词不含"原厂"——"XX品牌原厂授权"属资质定向（_QUAL_EXCLUSIVE_RE 兜底），
# 若此处也命中会与资质定向同句近邻冲突，挤掉正确的资质定向分类（2026-11-05 验收实证）。
_BRAND_MODEL_RE = re.compile(
    r'(?:'
    r'参照\s*[^\s，。；、（）()]{1,20}\s*(?:品牌|型号|系列|产品)'
    r'|指定\s*(?:使用|采用|采购)?\s*[^\s，。；、]{1,20}\s*(?:品牌|型号|产品|厂家|厂商)'
    r'|(?:选型|设备|产品|交换机|路由器|防火墙|服务器)\s*(?:等同于|相当于|同档次于)\s*[^\s，。；、]{2,20}'
    r'|(?:华为|新华三|H3C|华三|中兴|ZXR10|深信服|奇安信|天融信|启明星辰|海康威视|大华|锐捷|浪潮|思科|Cisco|Oracle|甲骨文)'
    r'[^。；\n]{0,20}?(?:型号|系列|同档次|指定|ZXR\d+|S\d{4}[A-Z]*|USG\d+)'
    r'|(?:型号|系列)\s*[：:为]?\s*[A-Z]{2,8}[- ]?\d{2,5}[A-Z0-9-]*'
    r')',
    re.IGNORECASE,
)

# (2) 资质定向 / 原厂唯一授权
_QUAL_EXCLUSIVE_RE = re.compile(
    r'(?:'
    r'仅限\s*[^。；\n]{1,35}?(?:原厂|授权|认证|资质|证书|厂商|代理商)'
    r'|(?:必须|须|应)\s*(?:提供|具备|具有|出具)\s*[^。；\n]{0,25}?原厂(?:针对本项目)?(?:唯一|专项|直接)?(?:授权书|授权函|售后服务承诺函|质保函|服务函)'
    r'|原厂(?:针对本项目)?(?:唯一)?授权'
    r'|唯一(?:合法)?授权'
    r'|仅(?:认可|接受)\s*[^。；\n]{2,25}?(?:认证|证书|资质)'
    r')'
)

# (3) 业绩定向（特定软著全称一字不差 / 狭隘地域业绩限制）
_PERF_EXCLUSIVE_RE = re.compile(
    r'(?:'
    r'一字不差[^。；\n]{0,20}?《[^》]{4,40}》'
    r'|(?:软著|软件著作权)[^。；\n]{0,20}?名称(?:须|必须|应)?为\s*《[^》]{6,50}[Vv]\d+\.\d+[^》]*》'
    r'|仅限\s*(?:本市|本区|本县|本地)\s*[^。；\n]{0,20}?业绩'
    r')'
)

# (4) 参数定向（毫米级严苛非标公差 / 特定指示灯颗数 / 唯一性排他）
_PARAM_EXCLUSIVE_RE = re.compile(
    r'(?:'
    r'(?:机箱|设备|外观)(?:尺寸|深度|高度|宽度)[^。；\n]{0,15}?(?:精确为|须为|为)\s*\d{2,4}\s*mm\s*±\s*[012]\s*mm'
    r'|前面板\s*\d+\s*颗[^。；\n]{0,15}?指示灯'
    r'|具有唯一性|唯一满足'
    r')'
)

_EXCLUSIVE_PATTERNS: list[tuple[str, re.Pattern, float, str]] = [
    ('品牌绑定', _BRAND_MODEL_RE, 0.88, '疑似指定或参照特定品牌/型号，存在品牌倾向性控标风险'),
    ('资质定向', _QUAL_EXCLUSIVE_RE, 0.85, '疑似要求原厂授权或特定排他资质，限制潜在供应商竞争'),
    ('业绩定向', _PERF_EXCLUSIVE_RE, 0.82, '疑似设定特定软著全称或排他性业绩条件，倾向特定厂商'),
    ('参数定向', _PARAM_EXCLUSIVE_RE, 0.80, '疑似设置非通用物理外观或唯一性定制参数，指向特定产品'),
]

_RADAR_LLM_PROMPT = """你是招投标合规与反控标审查专家。下面是从招标文件中截取的一个【候选原文窗口】。
请判断该条款是否属于“疑似排他性/倾向性控标参数”（如：指定或参照某品牌/特定型号、要求原厂授权函、绑定特定软著名称或非标外观尺寸）。

⚠️ 保守原则（严禁误报）：
- 国家政策鼓励的“国产化、自主可控、信创适配（如银河麒麟、统信UOS、达梦、人大金仓、鲲鹏、国密算法、等保认证）”属于合法通用要求，is_exclusive 必须判为 false！
- 反排他声明（如“不限定特定品牌”“任何品牌均可”）is_exclusive 必须判为 false！

【候选原文窗口】
{window}

【确定性初筛命中句】
{candidate_quote}

只输出一个 JSON 对象，字段如下：
1. is_exclusive：布尔值（true 表示疑似排他控标参数，false 表示正常合法要求）；
2. exclusive_type：只能取 "品牌绑定" | "资质定向" | "业绩定向" | "参数定向" | "无" 之一；
3. confidence：0.0 ~ 1.0 之间的置信度浮点数；
4. quote：从【候选原文窗口】中逐字抄录的排他参数原句（严禁改写、缩写或编造）；
5. reason：30 字以内的判定理由（必须包含“疑似”二字）。

只输出 JSON，不要任何其他文字。"""


def _parse_llm_json(content: str) -> Optional[dict]:
    if not content:
        return None
    m = _JSON_BLOCK_RE.search(content)
    if not m:
        return None
    try:
        obj = json.loads(m.group())
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _extract_sentence_around(text: str, start: int, end: int) -> tuple[str, int]:
    """提取命中点所在的完整单句或单行（确保 100% 为原文连续子串）。"""
    lo = start
    while lo > 0 and text[lo - 1] not in '\n。；！？':
        if start - lo >= 100:
            break
        lo -= 1
    hi = end
    while hi < len(text) and text[hi] not in '\n。；！？':
        if hi - end >= 100:
            break
        hi += 1
    if hi < len(text) and text[hi] in '。；！？':
        hi += 1
    quote = text[lo:hi].strip()
    real_pos = text.find(quote, lo)
    if real_pos < 0:
        real_pos = start
        quote = text[start:end].strip()
    return quote, real_pos


def _is_negated_or_legitimate(sentence: str, ex_type: str) -> bool:
    """保守过滤：反排他说明句（如'不得指定品牌'）或纯国产化通用政策要求不报排他。"""
    if re.search(r'不得(?:指定|限定|要求)|不指定|不限定|禁止指定|无排他', sentence):
        return True
    # 若命中合法国产化/信创导向，且未出现"参照XX品牌/型号"、"仅限"、"唯一授权"等强排他词，则免检
    if _LEGITIMATE_POLICY_RE.search(sentence):
        if not re.search(r'参照|指定.*?型号|ZXR\d+|仅限|唯一授权|一字不差', sentence, re.IGNORECASE):
            return True
    return False


def _ensure_suspected_prefix(reason: str) -> str:
    """确保每条原因说明都带有'疑似'字样（PRD 合规边界要求）。"""
    r = (reason or '').strip()
    if not r:
        return '疑似存在排他性或倾向性控标参数'
    if '疑似' not in r:
        return f"疑似排他参数：{r}"
    return r


def extract_suspicious_candidates(text: str) -> list[dict[str, Any]]:
    """确定性层（0 Token）：从招标文件全文圈出疑似排他性参数候选列表。"""
    if not text:
        return []

    candidates: list[dict[str, Any]] = []
    seen_offsets: list[int] = []

    for ex_type, pattern, default_conf, default_reason in _EXCLUSIVE_PATTERNS:
        for m in pattern.finditer(text):
            quote, real_offset = _extract_sentence_around(text, m.start(), m.end())
            if not quote:
                continue
            if _is_negated_or_legitimate(quote, ex_type):
                continue
            if any(abs(real_offset - prev) < 25 for prev in seen_offsets):
                continue
            seen_offsets.append(real_offset)

            w_lo = max(0, real_offset - _WINDOW_BEFORE)
            w_hi = min(len(text), real_offset + len(quote) + _WINDOW_AFTER)
            candidates.append({
                'quote': quote,
                'offset': real_offset,
                'window': text[w_lo:w_hi],
                'exclusive_type': ex_type,
                'confidence': default_conf,
                'reason': _ensure_suspected_prefix(default_reason),
            })

    candidates.sort(key=lambda c: c['offset'])
    return candidates


def _build_conclusion(suspicious_params: list[dict[str, Any]]) -> dict[str, str]:
    """根据疑似排他参数数量生成投前参考结论（只做参考意见，不替老板拍板）。"""
    cnt = len(suspicious_params)
    if cnt == 0:
        rec = '值得投'
        summary = (
            '本次扫描未检出明显疑似排他性或品牌绑定参数，招标技术与资格要求整体开放，'
            '评估为「值得投」（本结论仅为疑似度筛查参考意见，不替代人工立项决策）。'
        )
    elif cnt <= 2:
        types_str = '、'.join(dict.fromkeys(p['exclusive_type'] for p in suspicious_params))
        rec = '谨慎投'
        summary = (
            f"检出 {cnt} 处疑似排他性/倾向性参数（涉及{types_str}），建议结合我方资质与厂商关系"
            f"评估是否发起书面质疑澄清，「谨慎投」（本结论仅为疑似控标参考意见，不替决策者拍板）。"
        )
    else:
        types_str = '、'.join(dict.fromkeys(p['exclusive_type'] for p in suspicious_params))
        rec = '疑似控标高'
        summary = (
            f"检出 {cnt} 处高密度疑似排他性控标参数（涉及{types_str}），存在较强的定向控标迹象，"
            f"评定为「疑似控标高」（本结论仅为疑似风险参考意见，最终是否参标请业务负责人决策）。"
        )
    return {'recommendation': rec, 'summary': summary}


def scan_rigging_radar(tender_path: str | Path, client=None) -> dict[str, Any]:
    """F6 投前控标雷达主入口。

    Args:
        tender_path: 招标文件路径（docx/pdf/txt）。
        client: 可选，LLM 客户端；None 时执行 0 Token 确定性降级并标 llm_status='skipped'。

    Returns:
        {
            'suspicious_params': [
                {'quote', 'offset', 'exclusive_type', 'confidence', 'reason', 'quote_verified', 'llm_status'},
                ...
            ],
            'conclusion': {'recommendation': '值得投'|'谨慎投'|'疑似控标高', 'summary': str},
            'llm_status': 'done' | 'skipped',
        }
    """
    doc = parse_document(str(tender_path))
    full_text = doc.text or ''
    candidates = extract_suspicious_candidates(full_text)

    if client is None:
        suspicious_params = []
        for c in candidates:
            if verify_quote(c['quote'], full_text):
                suspicious_params.append({
                    'quote': c['quote'],
                    'offset': c['offset'],
                    'exclusive_type': c['exclusive_type'],
                    'confidence': c['confidence'],
                    'reason': _ensure_suspected_prefix(c['reason']),
                    'quote_verified': True,
                    'llm_status': 'skipped',
                })
        return {
            'suspicious_params': suspicious_params,
            'conclusion': _build_conclusion(suspicious_params),
            'llm_status': 'skipped',
        }

    # LLM 层（★大模型④）：逐候选窗口核验 + verify_quote 强制溯源，编造即丢弃
    verified_params: list[dict[str, Any]] = []
    accepted_quotes: list[str] = []          # 已采信 quote 文本（防同句重复收录）
    for c in candidates:
        prompt = _RADAR_LLM_PROMPT.format(
            window=c['window'],
            candidate_quote=c['quote'],
        )
        resp = client.chat([{'role': 'user', 'content': prompt}], model_tier='flash')
        parsed = _parse_llm_json(getattr(resp, 'content', '') or '')
        if not parsed:
            continue

        if parsed.get('is_exclusive') is not True:
            continue

        ex_type = str(parsed.get('exclusive_type') or c['exclusive_type']).strip()
        if ex_type == '无':
            continue
        if ex_type not in ('品牌绑定', '资质定向', '业绩定向', '参数定向'):
            ex_type = c['exclusive_type']

        llm_quote = str(parsed.get('quote') or '').strip()
        # 强制 verify_quote 溯源校验：LLM 输出编造的直接丢弃，绝不采信
        if not llm_quote or not verify_quote(llm_quote, full_text):
            continue

        real_off = full_text.find(llm_quote)
        if real_off < 0:
            real_off = c['offset']

        # 近邻/同文去重（确定性层同款纪律）：同句被多候选重复采信只保留一条
        if llm_quote in accepted_quotes or any(
                abs(real_off - p['offset']) < 25 for p in verified_params):
            continue
        accepted_quotes.append(llm_quote)

        try:
            conf = float(parsed.get('confidence', c['confidence']))
        except (TypeError, ValueError):
            conf = c['confidence']

        reason = _ensure_suspected_prefix(str(parsed.get('reason') or c['reason']))
        verified_params.append({
            'quote': llm_quote,
            'offset': real_off,
            'exclusive_type': ex_type,
            'confidence': round(min(max(conf, 0.0), 1.0), 2),
            'reason': reason,
            'quote_verified': True,
            'llm_status': 'done',
        })

    return {
        'suspicious_params': verified_params,
        'conclusion': _build_conclusion(verified_params),
        'llm_status': 'done',
    }


def dry_run_corpus_radar(corpus_dir: str | Path = 'data/corpus/m0_samples') -> list[dict[str, Any]]:
    """对 11 份真实招标文件语料执行 0 Token 控标雷达扫描并生成统计报告。"""
    c_dir = Path(corpus_dir)
    if not c_dir.exists():
        return []
    files = sorted([p for p in c_dir.iterdir() if p.suffix.lower() in ('.docx', '.pdf')])
    report = []
    for f in files:
        res = scan_rigging_radar(f, client=None)
        report.append({
            'file': f.name,
            'suspicious_count': len(res['suspicious_params']),
            'recommendation': res['conclusion']['recommendation'],
            'types': [p['exclusive_type'] for p in res['suspicious_params']],
        })
    return report


if __name__ == '__main__':
    rows = dry_run_corpus_radar()
    print(f"=== F6 投前控标雷达 11 份真实语料 0-Token 扫描报告（共 {len(rows)} 份）===")
    for r in rows:
        print(
            f"- {r['file']}: 检出疑似排他={r['suspicious_count']} 处 | "
            f"评估建议={r['recommendation']} | 类型分布={r['types']}"
        )