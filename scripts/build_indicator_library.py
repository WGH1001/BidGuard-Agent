#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""E41 · 常见指标与双业务线（网络安全 / 并行 MaaS）确定性草稿提炼脚本（0 Token）。

设计红线：
1. 仅从指定真实标书与合同模板中确定性抽取候选段落（标题层级 + 关键词锚点 + 表格表头），绝不调 LLM；
2. 抽取产物强制经 `sanitize_indicator_text` 脱除具体合同金额/Token 单价为 `[待人工填报：...]` 槽位；
3. 输出仅供人工（用户/领导）终审，默认写入 `output/indicator_drafts/`，严禁自动覆盖 `data/rules/indicator_library/`。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

from docx import Document

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.indicator_library import compute_library_content_hash, sanitize_indicator_text

DEFAULT_BID_FILES: tuple[Path, ...] = (
    Path(r'E:\agent\2.新agent数据法律\安易—02包.docx'),
    Path(r'E:\agent\2.新agent数据法律\安易—04包-工业物性数据应用开发平台.docx'),
)
DEFAULT_CONTRACT_DIR = Path(r'E:\agent\1.合同法律\公司测试合同')
DEFAULT_DRAFT_OUT_DIR = ROOT / 'output' / 'indicator_drafts'

_COMMON_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ('售后运维与故障响应 SLA', 'SLA运维', ('7×24', '5×8', '故障响应', '到达现场', '服务电话', '巡检', '三包')),
    ('系统实施方案与进度计划', '实施交付', ('实施方案', '系统上线', '部署阶段', '设计阶段', '工期', '验收标准')),
    ('产品培训与运维知识转移', '实施交付', ('培训方案', '基础培训', '运行维护培训', '使用操作培训', '培训内容')),
    ('数据保密与安全责任承诺', '安全资质', ('保密信息', '保密义务', '商业信息', '泄露事件')),
)

_SECURITY_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ('普通与安全工作环境隔离', '技术方案', ('环境隔离', '安全工作环境', '普通环境', '双环境')),
    ('重要文件访问防泄漏与行为审计', '技术方案', ('文件访问安全', '粘贴复制', '截屏', '操作记录', '审计')),
    ('逻辑隔离磁盘访问控制技术', '技术方案', ('逻辑隔离磁盘', '磁盘访问控制', '木马', '恶意访问')),
    ('专用磁盘加密与防勒索防护', '技术方案', ('磁盘加密', '防勒索', '数据破坏', '透明加密', '不落地')),
    ('数据传输分级权限与隐私保护', '安全资质', ('分级权限', '加密传输', '隐私保护', '访问权限')),
)

_MAAS_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ('并行 MaaS 平台服务与 Token 计量', '计费配额', ('并行MaaS平台', '模型Token', 'AI智算云平台', 'Token额度', '充值金额')),
    ('主流大模型 SKU 与长上下文目录', '技术方案', ('DeepSeek', 'GLM', 'Coding Plan', 'Tokens输入', 'Tokens输出', '上下文长度', '缓存计费')),
    ('多模型网关调度与本地/云端切换', '技术方案', ('多模型', '本地模型', '大语言模型', '线上大模型', '知识库')),
    ('分布式算力集群与高并发推理', '技术方案', ('分布式硬件集群', '高并发任务', '硬件加速', '算力调度')),
    ('MaaS 知识产权抗辩与客户内容归属', '安全资质', ('客户内容', '知识产权', '第三方索赔', '损失赔偿责任上限')),
)


def _extract_docx_paragraphs_and_tables(path: Path) -> tuple[list[str], list[list[list[str]]]]:
    doc = Document(str(path))
    paras = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
    tables: list[list[list[str]]] = []
    for tbl in doc.tables:
        rows_data: list[list[str]] = []
        for row in tbl.rows[:12]:
            seen_tc = set()
            row_vals: list[str] = []
            for cell in row.cells:
                if cell._tc in seen_tc:
                    continue
                seen_tc.add(cell._tc)
                row_vals.append(cell.text.strip().replace('\n', ' '))
            if any(row_vals):
                rows_data.append(row_vals)
        if rows_data:
            tables.append(rows_data)
    return paras, tables


def _collect_rule_hits(
    paras: list[str],
    rules: tuple[tuple[str, str, tuple[str, ...]], ...],
    *,
    source_name: str,
    line: str,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for idx, (ind_name, cat, kws) in enumerate(rules, start=1):
        matched_paras: list[str] = []
        seen: set[str] = set()
        for p in paras:
            if len(p) < 12 or len(p) > 450:
                continue
            if any(k.lower() in p.lower() for k in kws):
                norm = p[:80]
                if norm not in seen:
                    seen.add(norm)
                    matched_paras.append(sanitize_indicator_text(p))
            if len(matched_paras) >= 3:
                break
        if not matched_paras:
            continue
        candidates.append({
            'indicator_id': f"draft_{line}_{idx:02d}_{abs(hash(source_name)) % 1000:03d}",
            'line': line,
            'category': cat,
            'indicator_name': ind_name,
            'standard_response': '\n'.join(matched_paras),
            'keywords': list(kws),
            'source_ref': source_name,
            'hit_count': len(matched_paras),
            'review_status': 'draft_pending_human_signoff',
        })
    return candidates


def build_draft_bundle(
    bid_files: list[Path],
    contract_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """扫描真实语料与合同模板并产出三份待终审草稿 JSON 及审校清单。"""
    resolved_out = output_dir.resolve()
    official_dir = (ROOT / 'data' / 'rules' / 'indicator_library').resolve()
    if resolved_out == official_dir or official_dir in resolved_out.parents:
        raise RuntimeError(
            '安全红线拦截：scripts/build_indicator_library.py 仅允许输出至草稿目录，严禁直接写入正式库 data/rules/indicator_library/！'
        )

    all_docs: list[Path] = [p for p in bid_files if p.is_file()]
    if contract_dir.is_dir():
        all_docs.extend(sorted(p for p in contract_dir.glob('*.docx') if p.is_file()))

    by_line: dict[str, list[dict[str, Any]]] = {'common': [], 'security': [], 'maas': []}
    file_stats: list[dict[str, Any]] = []
    table_skus: list[dict[str, Any]] = []

    for fp in all_docs:
        paras, tables = _extract_docx_paragraphs_and_tables(fp)
        c_hits = _collect_rule_hits(paras, _COMMON_RULES, source_name=fp.name, line='common')
        s_hits = _collect_rule_hits(paras, _SECURITY_RULES, source_name=fp.name, line='security')
        m_hits = _collect_rule_hits(paras, _MAAS_RULES, source_name=fp.name, line='maas')
        by_line['common'].extend(c_hits)
        by_line['security'].extend(s_hits)
        by_line['maas'].extend(m_hits)

        if 'MaaS' in fp.name or '合同' in fp.name:
            for tbl in tables:
                header_line = ' | '.join(tbl[0]) if tbl else ''
                if any(tok in header_line for tok in ('模型', 'Token', 'Tokens', '订阅方案')):
                    sanitized_rows = [
                        [sanitize_indicator_text(cell) for cell in row]
                        for row in tbl[:8]
                    ]
                    table_skus.append({'source': fp.name, 'rows': sanitized_rows})

        file_stats.append({
            'file': fp.name,
            'paragraphs': len(paras),
            'tables': len(tables),
            'common_hits': len(c_hits),
            'security_hits': len(s_hits),
            'maas_hits': len(m_hits),
        })

    resolved_out.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    written_files: list[str] = []

    for line_name in ('common', 'security', 'maas'):
        entries = by_line[line_name]
        payload = {
            'schema_version': '1.0',
            'line': line_name,
            'review_status': 'draft_pending_human_signoff',
            'generated_at': generated_at,
            'reviewed_at': None,
            'reviewed_by': None,
            'content_hash': compute_library_content_hash(entries),
            'entries': entries,
            **({'extracted_sku_tables': table_skus} if line_name == 'maas' and table_skus else {}),
        }
        out_f = resolved_out / f"{line_name}_indicators.draft.json"
        out_f.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        written_files.append(str(out_f))

    checklist_lines = [
        '# E41 指标库候选草稿人工终审清单',
        '',
        f'- 生成时间：{generated_at}',
        '- **纪律提醒**：本清单及 `.draft.json` 仅供公司技术人员/领导终审确认口径，严禁未核实直接填报具体数字。',
        '',
        '## 各源文件确定性抽提统计',
        '',
        '| 源文件 | 段落数 | 表格数 | 通用命中 | 安全命中 | MaaS 命中 |',
        '|---|---|---|---|---|---|',
    ]
    for st in file_stats:
        checklist_lines.append(
            f"| {st['file']} | {st['paragraphs']} | {st['tables']} | {st['common_hits']} | {st['security_hits']} | {st['maas_hits']} |"
        )
    checklist_lines.extend([
        '',
        '## 待人工终审确认参数清单（缺口项已在标准正文留槽）',
        '',
        '1. **通用 SLA**：核实 `7×24 电话 / 5×8 支持 / ≤1 小时故障响应 / ≤24 小时到达现场` 是否作为公司统一对外最低承诺，若需升级 `15 分钟` 需内部签署；',
        '2. **网络安全线**：补齐公司实际拥有的等保测评等级、密评/ISO27001/ITSS 证书编号及国密 SM2/SM3/SM4 算法支持口径；',
        '3. **并行 MaaS 线**：核实 API 可用性 SLA（如 99.9%）、并发配额（RPM/TPM/QPS）、首字延迟 TTFT 与“客户数据不用于模型训练”承诺天数，具体 Token 单价与折扣仅在报价表按项目填报。',
    ])
    ck_path = resolved_out / 'REVIEW_CHECKLIST.md'
    ck_path.write_text('\n'.join(checklist_lines) + '\n', encoding='utf-8')
    written_files.append(str(ck_path))

    return {
        'output_dir': str(resolved_out),
        'written_files': written_files,
        'file_stats': file_stats,
        'line_counts': {k: len(v) for k, v in by_line.items()},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='E41 0-Token 公司常见指标与业务线草稿抽提工具')
    parser.add_argument('--bid-file', action='append', default=[], help='补充投标/历史方案 .docx 路径（可多次传入）')
    parser.add_argument('--contract-dir', default=str(DEFAULT_CONTRACT_DIR), help='MaaS 合同模板目录')
    parser.add_argument('--output-dir', default=str(DEFAULT_DRAFT_OUT_DIR), help='草稿输出目录（默认 output/indicator_drafts）')
    args = parser.parse_args(argv)

    bid_paths = [Path(p) for p in args.bid_file] if args.bid_file else list(DEFAULT_BID_FILES)
    res = build_draft_bundle(bid_paths, Path(args.contract_dir), Path(args.output_dir))
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
