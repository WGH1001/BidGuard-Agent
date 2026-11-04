# -*- coding: utf-8 -*-
"""F1 确定性提取层单元测试（纯离线，0 Token，合成小型招标文件样本）。"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.tender_extractor import (
    build_review_checklist,
    extract_invalid_bid_clauses,
    extract_marked_clauses,
    locate_chapters,
)

SAMPLE = """第一章  投标邀请
项目概况：本项目为测试项目。

第二章  投标人须知
一、申请人的资格要求
1.投标人须具有有效营业执照。
2.投标人须提供近一年财务状况报告。
二、符合性审查
评标委员会对投标文件进行符合性审查。

第三章  采购需求
2.1 服务器参数
★1) CPU 核数不低于 32 核，内存不低于 128G
★2) 系统须支持国产数据库适配
▲3) 质保期不低于三年
（十五）售后服务要求
本条为不允许偏离的实质性要求和条件，如不满足，在符合性审查时按照投标无效
处理，投标文件中提供《售后服务响应承诺函》。

第五章  评标方法与评标标准
评分细则表如下：价格分 10 分，技术分 60 分。
（十四）投标报价要求：
投标人只能提交一个投标价格，投标人提交多个价格的投标文件，在资格性、符合
性检查时按照无效投标处理。
"""


def test_locate_chapters():
    chapters = locate_chapters(SAMPLE)
    idx = [c['index'] for c in chapters]
    assert idx == ['第一章', '第二章', '第三章', '第五章'], idx
    eval_ch = [c for c in chapters if '评标' in c['title']]
    assert eval_ch and '评标方法与评标标准' in eval_ch[0]['title']


def test_marked_clauses():
    hits = extract_marked_clauses(SAMPLE)
    stars = [h for h in hits if h['mark'] == '★']
    triangles = [h for h in hits if h['mark'] == '▲']
    assert len(stars) == 1, stars  # 连续两行 ★ 合并为一条
    assert 'CPU 核数不低于 32 核' in stars[0]['text']
    assert '国产数据库' in stars[0]['text']  # 续行合并
    assert len(triangles) == 1 and '质保期不低于三年' in triangles[0]['text']
    # 偏移可还原原文
    for h in hits:
        assert SAMPLE[h['start']:h['end']].startswith(h['text'][:5])


def test_marked_clauses_dual_marker_line():
    """同一行同时含 ★ 与 ▲ 时（南京病理实测存在）必须产出两条记录，绝不丢标记。"""
    text = '★1) CPU不低于32核 ▲1) 质保不低于三年'
    hits = extract_marked_clauses(text)
    marks = sorted(h['mark'] for h in hits)
    assert marks == ['▲', '★'], marks
    assert all(h['start'] == 0 for h in hits)


def test_marked_clauses_usage_note_tagged():
    """标记使用说明语（元说明）必须打 usage_note 标签，与采购要求区分。

    语料实证两类：滁州物业 #10「根据采购需求中标"▲"功能要求…」、
    无锡梁溪 #2《节能产品政府采购品目清单》「属于清单中打★品目的…」。
    """
    text = (
        '▲1) 残值回收功能\n▲2) 质保金支付方式\n'
        '根据采购需求中标“▲”功能要求，投标人须提供相关证明材料。\n'
        '依据财政部发布的《节能产品政府采购品目清单》，属于清单中打★品目的，'
        '只能选择具有国家认证标识的节能产品。\n'
        '★1) 系统响应时间不超过 3 秒\n'
    )
    hits = extract_marked_clauses(text)
    notes = [h for h in hits if h['kind'] == 'usage_note']
    reqs = [h for h in hits if h['kind'] == 'requirement']
    assert len(notes) == 2, notes          # 两类说明语各命中一条
    assert any('残值回收' in h['text'] for h in reqs)
    assert any('响应时间' in h['text'] for h in reqs)
    assert all(not h['text'].startswith('根据采购需求') for h in reqs)


def test_marked_clauses_note_line_dual_symbol_single_record():
    """南京病理实测（2026-11-04 用户独立复核）：评分说明句同句含 ★▲ 两种符号，
    双通道各提一条 → 幽灵符号 +1★+1▲ 破坏逐字符守恒。修复后说明语行只出
    一条 usage_note 记录，text 覆盖整句、符号守恒。"""
    text = ('产品技术参数全部满足或高于招标要求，打★指标为必须满足项，'
            '打▲指标为重要指标。')
    hits = extract_marked_clauses(text)
    assert len(hits) == 1, hits
    assert hits[0]['kind'] == 'usage_note'
    assert hits[0]['text'].count('★') == 1 and hits[0]['text'].count('▲') == 1


def test_marked_clauses_table_marker_fragments():
    """芜湖实测（2026-11-04 用户终审第 4 类边界）：中小企业划型附表的行业名
    行尾带★（工业★/仓储业★）→ kind=table_marker（真要求行不存在行尾标记
    形态）；配套两条说明语变体（标注"▲"的标的为主要标的 / 带★的项为行业）
    归 usage_note。已知留白：标的表碎片"▲中共无"（▲+采购人名称断行）与
    滁州物业真功能项"▲残值回"语法同构，不硬判，留人工定稿。"""
    text = (
        '7.采购需求中标注“▲”的标的为主要标的（包括核心产品）。\n'
        '编制招标文件时必须将主要标的标注“▲”。\n'
        '工业★\n'
        '\n'
        '仓储业★\n'
        '\n'
        '2.附表中各行业的范围以《国民经济行业分类》为准。带★的项为行业。\n'
        '▲中共无\n'
        '★1) 数字化加工成果须符合国家相关标准\n'
    )
    hits = extract_marked_clauses(text)
    reqs = [h for h in hits if h['kind'] == 'requirement']
    notes = [h for h in hits if h['kind'] == 'usage_note']
    tables = [h for h in hits if h['kind'] == 'table_marker']
    assert len(reqs) == 2, hits   # 数字化加工要求 + ▲中共无（语法不可分，留人工）
    assert len(notes) == 3, notes    # 标的说明×2 + 划型说明×1
    assert len(tables) == 2, tables  # 工业★ + 仓储业★（行尾标记）
    # 逐字符守恒：记录 text 覆盖原文全部标记
    assert sum(h['text'].count('★') for h in hits) == text.count('★')
    assert sum(h['text'].count('▲') for h in hits) == text.count('▲')


def test_marked_clauses_usage_note_bazhou_templates():
    """亳州 #8 第 61 页两条说明语（2026-11-04 用户独立复核实证）：
    与梁溪/滁州物业同类模板，必须同判 usage_note。注意亳州 PDF 实际排版
    符号带空格和引号：'标有 “★”的参数为实质性参数'。"""
    text = ('标有 “★”的参数为实质性参数，必须响应或优于响应，否则，其投标无效。\n'
            '2、标有 “▲”的为政府强制采购节能产品，须具有相关认证。\n'
            '★1) 平台须支持国产数据库\n')
    hits = extract_marked_clauses(text)
    notes = [h for h in hits if h['kind'] == 'usage_note']
    reqs = [h for h in hits if h['kind'] == 'requirement']
    assert len(notes) == 2 and len(reqs) == 1, hits
    assert {h['mark'] for h in notes} == {'★', '▲'}
    assert '国产数据库' in reqs[0]['text']  # 说明语不得吞并后续要求行


def test_eval_chapter_skips_toc_entry():
    """评标章定位必须跳过目录行（点线引导符+页码），取正文真实章标题。"""
    text = (
        '目 录\n第一章 投标邀请………1\n第五章 评标方法与评标标准…………37\n'
        '正文开始\n第五章 评标方法与评标标准\n价格分 10 分。\n'
    )
    from core.tender_extractor import build_review_checklist  # noqa: F401  确认可导入
    # 直接复用 build_review_checklist 的章选择逻辑代价高（需落盘），此处验证章节清单+过滤口径
    chapters = locate_chapters(text)
    evals = [c for c in chapters if '评标' in c['title']]
    real = [c for c in evals if '…' not in c['title']]
    assert len(evals) == 2 and len(real) == 1
    assert real[0]['title'] == '评标方法与评标标准'


def test_pattern_a_template_sentence():
    hits = extract_invalid_bid_clauses(SAMPLE)
    a_hits = [h for h in hits if h['pattern'] == 'A']
    assert len(a_hits) == 1
    assert a_hits[0]['promise_doc'] == '售后服务响应承诺函'
    assert '实质性要求' in a_hits[0]['text']


def test_pattern_b_trigger_words():
    hits = extract_invalid_bid_clauses(SAMPLE)
    b_hits = [h for h in hits if h['pattern'] == 'B']
    assert b_hits, '至少命中（十四）投标报价要求一处'
    joined = ''.join(h['text'] for h in b_hits)
    assert '投标价格' in joined  # 向前吸附到编号条款
    # 模式 A 与 B 重叠区域已按 A 优先去重，A 记录不被 B 覆盖
    assert any(h['pattern'] == 'A' for h in hits)


def test_build_review_checklist(tmp_path):
    docx_path = None  # 合成样本直接写 txt 验证全流程
    p = tmp_path / 'mini_tender.txt'
    p.write_text(SAMPLE, newline='\n', encoding='utf-8')
    result = build_review_checklist(str(p))
    assert result['source_type'] == 'txt'
    assert result['chars'] == len(SAMPLE)
    assert result['eval_chapter'] and '评标' in result['eval_chapter']['title']
    assert result['marked_clauses']
    assert result['invalid_bid_clauses']
    assert result['qualification_hits']  # 申请人的资格要求
    assert result['compliance_hits']     # 符合性审查
    assert result['scoring_hits']        # 评分细则表
