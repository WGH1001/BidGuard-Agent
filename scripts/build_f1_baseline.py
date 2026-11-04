# -*- coding: utf-8 -*-
"""F1 基准候选清单构建工具：对 data/corpus/m0_samples/ 全部语料跑确定性提取层。

用法（项目根目录）：
    python scripts/build_f1_baseline.py

产物（data/corpus/m0_samples/baseline/）：
    candidates_<项目编号>.json   每份语料的四类候选（含偏移可溯源）
    _summary.md                  人工核对用汇总表（★/▲/模式A/B 计数）
0 Token，全程无 LLM 调用。候选清单经人工核对定稿后即为 F1 的 ground truth 基准。
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.tender_extractor import build_review_checklist

CORPUS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'data', 'corpus', 'm0_samples'))
OUT_DIR = os.path.join(CORPUS_DIR, 'baseline')


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    files = sorted(
        f for f in glob.glob(os.path.join(CORPUS_DIR, '*'))
        if f.lower().endswith(('.docx', '.pdf', '.txt', '.md')) and not f.endswith('语料清单.md')
    )
    if not files:
        raise SystemExit('未找到语料文件，请确认 data/corpus/m0_samples/')

    summary = ['# F1 确定性候选清单汇总（人工核对用）', '',
               '| # | 文件 | 来源 | ★要求 | ▲要求 | 说明语 | 表格碎片 | 模式A | 模式B | 资格命中 | 符合命中 | 评标章 |',
               '|---|---|---|---|---|---|---|---|---|---|---|---|']
    for i, f in enumerate(files, 1):
        name = os.path.basename(f)
        print(f'[{i}/{len(files)}] {name}')
        result = build_review_checklist(f)
        notes = sum(1 for c in result['marked_clauses'] if c.get('kind') == 'usage_note')
        tabs = sum(1 for c in result['marked_clauses'] if c.get('kind') == 'table_marker')
        stars = sum(1 for c in result['marked_clauses']
                    if c['mark'] == '★' and c.get('kind') == 'requirement')
        tri = sum(1 for c in result['marked_clauses']
                  if c['mark'] == '▲' and c.get('kind') == 'requirement')
        pa = sum(1 for c in result['invalid_bid_clauses'] if c['pattern'] == 'A')
        pb = sum(1 for c in result['invalid_bid_clauses'] if c['pattern'] == 'B')
        ev = (result['eval_chapter'] or {}).get('title', '-')
        summary.append(
            f"| {i} | {name} | {result['source_type']} | {stars} | {tri} | {notes} | {tabs} "
            f"| {pa} | {pb} | {len(result['qualification_hits'])} "
            f"| {len(result['compliance_hits'])} | {ev} |"
        )
        out = os.path.join(OUT_DIR, f"candidates_{os.path.splitext(name)[0]}.json")
        with open(out, 'w', encoding='utf-8', newline='\n') as fh:
            json.dump(result, fh, ensure_ascii=False, indent=1)

    with open(os.path.join(OUT_DIR, '_summary.md'), 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('\n'.join(summary) + '\n')
        fh.write(
            '\n> 口径说明：★要求/▲要求 列为 requirement 条款记录数（连续同类型标记行'
            '合并为一条），与原文标记字符数可能不等；说明语列 = usage_note（标记使用'
            '元说明，如"标注▲的标的为主要标的"、节能品目清单条款）；表格碎片列 = '
            'table_marker（附表行标记碎片，如划型表行业名"工业★"）；两者均非投标人'
            '响应要求。模式 A/B 列为无标记措辞型候选数（同条款 A 优先去重）。'
            '人工核对以 JSON 内逐条 text 为准，编辑定稿后即为 F1 ground truth 基准。\n'
        )
    print(f'\n完成：候选 JSON x{len(files)} + _summary.md → {OUT_DIR}')


if __name__ == '__main__':
    main()
