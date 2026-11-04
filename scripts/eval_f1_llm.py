# -*- coding: utf-8 -*-
"""F1 LLM 层对账脚本：11 份语料真调对账，LLM 结构化结果对照 ground truth。

用法（项目根目录）：
    python scripts/eval_f1_llm.py                 # 全量 11 份（约 20-40 分钟）
    python scripts/eval_f1_llm.py --file 南京     # 单份试跑（按文件名子串过滤）
    python scripts/eval_f1_llm.py --env prod      # 走内网网关（默认 demo 智谱）

对账口径（三板斧之守恒对账）：
    召回 = ground truth 的 requirement 记录被送入 LLM 结构化的比例（应为 100%）；
    一致率 = LLM is_requirement 判定与 ground truth kind 的一致性（ground truth
    全部为真要求，LLM 判 false 即分歧，逐条列出供人工裁决）；
    溯源率 = quote_verified 比例（full_text 逐字摘自窗口，<100% 需人工看）；
    成本 = 网关 total_usage 累计。

产物：data/corpus/m0_samples/llm_eval/_eval_report.md + 逐份结构化 JSON。
"""
import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.tender_llm import extract_tender_with_llm

CORPUS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'data', 'corpus', 'm0_samples'))
GT_DIR = os.path.join(CORPUS_DIR, 'ground_truth')
OUT_DIR = os.path.join(CORPUS_DIR, 'llm_eval')


def eval_one(corpus_path: str, client, gt: dict) -> dict:
    result = extract_tender_with_llm(corpus_path, client=client)
    struct = result['llm_structured']

    gt_reqs = [c for c in gt['marked_clauses'] if c.get('kind') == 'requirement']
    gt_starts = {c['start'] for c in gt_reqs}
    # 召回：GT requirement 全部被送入结构化（相同 start）
    got_starts = {c['start'] for c in struct if c.get('mark')}
    # 措辞型候选没有 GT 条目，单列计数
    wording = [c for c in struct if c.get('pattern')]
    marked = [c for c in struct if c.get('mark')]

    covered = len(gt_starts & got_starts)
    recalled = covered / len(gt_starts) if gt_starts else 1.0

    disagreements = []
    for c in marked:
        s = c.get('structured') or {}
        if s.get('is_requirement') is False:
            disagreements.append({
                'start': c['start'],
                'candidate': c['text'][:60],
                'llm_reason': s.get('reason', ''),
            })
    verified = sum(1 for c in struct if c.get('quote_verified'))

    return {
        'file': os.path.basename(corpus_path),
        'gt_requirements': len(gt_reqs),
        'llm_calls': len(struct),
        'wording_candidates': len(wording),
        'recall': round(recalled, 4),
        'quote_verified_rate': round(verified / len(struct), 4) if struct else 1.0,
        'is_requirement_disagreements': disagreements,
        'structured': struct,
        'llm_usage': result['llm_usage'],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--file', default='', help='文件名子串过滤（单份试跑）')
    ap.add_argument('--env', default=None, help='模型环境：demo(默认)/prod')
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    from core.llm_gateway import get_llm_client
    client = get_llm_client(args.env)

    gt_files = sorted(glob.glob(os.path.join(GT_DIR, 'candidates_*.json')))
    # GT JSON 名 candidates_<语料名>.json → 语料原文件 <语料名>.docx/pdf
    corpus_files = {
        os.path.splitext(os.path.basename(f))[0]: f
        for f in glob.glob(os.path.join(CORPUS_DIR, '*'))
        if f.lower().endswith(('.docx', '.pdf', '.txt'))
    }
    targets = []
    for g in gt_files:
        stem = os.path.splitext(os.path.basename(g))[0].removeprefix('candidates_')
        if stem in corpus_files:
            targets.append((g, corpus_files[stem]))
    if args.file:
        targets = [(g, c) for g, c in targets if args.file in c]
    if not targets:
        raise SystemExit('没有匹配的语料文件')

    report = ['# F1 LLM 分段抽取对账报告', '',
              f'> 环境：{args.env or "demo(默认)"} ｜ 语料：{len(targets)} 份', '',
              '| 文件 | GT要求 | 调用 | 召回 | 溯源率 | 分歧 |',
              '|---|---|---|---|---|---|']
    total_calls = 0
    for i, (gt_path, corpus_path) in enumerate(targets, 1):
        name = os.path.basename(corpus_path)
        print(f'[{i}/{len(targets)}] {name}')
        gt = json.load(open(gt_path, encoding='utf-8'))
        r = eval_one(corpus_path, client, gt)
        total_calls += r['llm_calls']
        report.append(
            f"| {name[:36]} | {r['gt_requirements']} | {r['llm_calls']} "
            f"| {r['recall']:.0%} | {r['quote_verified_rate']:.0%} "
            f"| {len(r['is_requirement_disagreements'])} |"
        )
        out = os.path.join(OUT_DIR, f"structured_{os.path.splitext(name)[0]}.json")
        with open(out, 'w', encoding='utf-8', newline='\n') as fh:
            json.dump(r, fh, ensure_ascii=False, indent=1)
        for d in r['is_requirement_disagreements']:
            report.append(f"  - 分歧 @{d['start']}: {d['candidate']} ｜ LLM: {d['llm_reason']}")

    usage = client.total_usage()
    report += ['', f"## 汇总", '',
               f"- 结构化调用：{total_calls} 次",
               f"- Token 用量：prompt={usage.get('prompt_tokens', 0)} / "
               f"completion={usage.get('completion_tokens', 0)}",
               f"- 累计成本估算：{usage.get('cost', 0)} 元"]
    with open(os.path.join(OUT_DIR, '_eval_report.md'), 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('\n'.join(report) + '\n')
    print('\n'.join(report[-6:]))
    print(f'\n完成：报告与逐份 JSON → {OUT_DIR}')


if __name__ == '__main__':
    main()
