# -*- coding: utf-8 -*-
"""E29 验收测试：感知层升级（F4 定位过滤+OCR 区间/F2 合并开标表与大模型表格坐标感知/OCR 进度播报/真标书回归）。"""
import io
import json
import sys
from pathlib import Path

import docx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import parser as parser_mod
from core.parser import parse_document
from core.price_extractor import extract_price_data, run_price_check
from core.price_sandbox import PriceSandbox, _normalize_upper
from core.response_checker import run_response_check


def _make_valid_png_bytes(width: int = 280, height: int = 220) -> bytes:
    """生成可通过尺寸与字节阈值过滤护栏（>20KB 且 >=200x200）的合法 PNG 图片字节流。"""
    import os
    from PIL import Image

    buf = io.BytesIO()
    Image.frombytes('RGB', (width, height), os.urandom(width * height * 3)).save(buf, format='PNG')
    return buf.getvalue()


def test_e29_parser_records_media_ocr_spans_and_f4_skips_toc_and_ocr_contract(tmp_path, monkeypatch):
    """① ParsedDoc 挂载精确 media_ocr_spans；F4 跳过目录页与内嵌历史合同图片 OCR，精准定位偏离表正面响应。"""
    tender_p = tmp_path / 'tender.txt'
    tender_p.write_text(
        "第四章 采购需求\n"
        "★1. 工业物性数据知识库：支持接入大语言模型构建工业自然语言知识库。\n"
        "★2. 五、验收标准：按照技术参数逐条进行验收，不超过3个月完成现场验收。\n",
        encoding='utf-8',
        newline='\n',
    )

    bid_docx = tmp_path / 'bid_with_toc_and_image.docx'
    doc = docx.Document()
    doc.add_paragraph('目 录')
    doc.add_paragraph('4.1. 工业物性数据知识库\t114')
    doc.add_paragraph('4.2. 五、验收标准\t120')

    # 插入一张内嵌扫描图片，模拟“湖州历史合同业绩扫描件”
    img_stream = io.BytesIO(_make_valid_png_bytes())
    doc.add_paragraph().add_run().add_picture(img_stream)

    # 后文附带真实偏离表与履约响应
    tbl = doc.add_table(rows=2, cols=4)
    tbl.cell(0, 0).text = '序号'
    tbl.cell(0, 1).text = '招标文件要求'
    tbl.cell(0, 2).text = '投标响应内容'
    tbl.cell(0, 3).text = '偏离情况'
    tbl.cell(1, 0).text = '1'
    tbl.cell(1, 1).text = '工业物性数据知识库：支持接入大语言模型。'
    tbl.cell(1, 2).text = '我公司提供的产品完全满足招标文件的要求：工业物性数据知识库全面支持工业自然语言处理。'
    tbl.cell(1, 3).text = '无偏离'
    doc.add_paragraph('五、验收标准：我方承诺按照技术参数逐条进行验收，不超过3个月完成现场验收，并保持质保期1年。')
    doc.save(str(bid_docx))

    hist_contract_ocr = (
        '该项目在_湖州_实施\n'
        '四、验收标准和方式\n'
        '技术服务按技术要求标准，采用专家评审方式验收。\n'
        '本合同服务项目的质保期为3年，自验收合格之日起算。\n'
        '合同类2020V2.0版（20200722启用）'
    )
    monkeypatch.setattr(parser_mod, '_ocr_single_image_bytes', lambda _b: (hist_contract_ocr, False))

    bdoc = parse_document(str(bid_docx), ocr_images=True)
    spans = getattr(bdoc, 'media_ocr_spans', None)
    assert isinstance(spans, list) and len(spans) == 1
    sp = spans[0]
    assert bdoc.text[sp['start']:sp['end']] == hist_contract_ocr

    reqs = [
        {
            'clause_no': '1',
            'mark': '★',
            'text': '工业物性数据知识库：支持接入大语言模型构建工业自然语言知识库。',
        },
        {
            'clause_no': '五、',
            'mark': '★',
            'text': '五、验收标准：按照技术参数逐条进行验收，不超过3个月完成现场验收。',
        },
    ]
    res = run_response_check(
        tender_p,
        bid_docx,
        requirements=reqs,
        client=None,
        bid_parsed_doc=bdoc,
    )
    by_no = {r['clause_no']: r for r in res['star_results']}
    assert by_no['1']['status'] == 'RESPONSE_OK'
    assert by_no['1']['bid_start'] > sp['end']
    assert '完全满足' in bdoc.text[by_no['1']['bid_start'] - 40:by_no['1']['bid_end'] + 40] or '全面支持' in by_no['1']['bid_quote']
    assert by_no['五、']['status'] == 'RESPONSE_OK'
    assert by_no['五、']['bid_start'] > sp['end']
    assert not (sp['start'] <= by_no['五、']['bid_start'] < sp['end'])


def test_e29_price_extractor_heji_column_and_merged_kaibiao_table(tmp_path):
    """②/③ 覆盖：_normalize_upper 省写整字放行与缺元拦截、'合计'列名、开标一览表横向合并跨列多行子标签取值。"""
    # A. 大小写归一化边界：有元省整放行，连元都缺必须拦截
    assert _normalize_upper('肆拾柒万元') == '肆拾柒万元整'
    assert _normalize_upper('壹佰叁拾玖万伍仟') == '壹佰叁拾玖万伍仟'
    assert PriceSandbox(bid_total='470000.00', bid_total_upper='肆拾柒万元').run() == []
    assert any(
        f['code'] == 'UPPER_MISMATCH'
        for f in PriceSandbox(bid_total='1395000.00', bid_total_upper='壹佰叁拾玖万伍仟').run()
    )

    # B. 横向合并《开标一览表》+ '合计' 分项列（04包合规形态）
    tender_p = tmp_path / 'tender_04.txt'
    tender_p.write_text('第一章 招标公告\n分包04预算：50万元。\n', encoding='utf-8', newline='\n')

    bid_04 = tmp_path / 'bid_04_merged.docx'
    d04 = docx.Document()
    d04.add_paragraph('2 开标一览表')
    d04.add_paragraph('项目名称：数据库平台  包号：04包')
    t1 = d04.add_table(rows=3, cols=4)
    t1.cell(0, 0).text = '投标人名称'
    t1.cell(0, 1).merge(t1.cell(0, 2))
    t1.cell(0, 1).text = '投标总价（大写/小写，单位：元）'
    t1.cell(0, 3).text = '备注'
    t1.cell(1, 0).text = '测试公司'
    t1.cell(1, 1).text = '人民币小写：'
    t1.cell(1, 2).text = '470000'
    t1.cell(2, 0).text = '测试公司'
    t1.cell(2, 1).text = '人民币大写：'
    t1.cell(2, 2).text = '肆拾柒万元'

    t2 = d04.add_table(rows=2, cols=5)
    for idx, hd in enumerate(['序号', '名称', '数量（单位）', '单价', '合计']):
        t2.cell(0, idx).text = hd
    for idx, val in enumerate(['1.', '工业物性数据平台', '1套', '470000', '470000']):
        t2.cell(1, idx).text = val
    d04.save(str(bid_04))

    r04 = run_price_check(bid_04, tender_path=tender_p)
    assert r04['status'] == 'OK'
    assert r04['bid_total'] == '470000.00'
    assert r04['bid_total_upper'] == '肆拾柒万元'
    assert len(r04['items']) == 1 and r04['items'][0]['total_price'] == '470000.00'
    assert r04['findings'] == []

    # C. 缺 '元整' 的《开标一览表》保留抓取并报出 UPPER_MISMATCH
    bid_02 = tmp_path / 'bid_02_bad_upper.docx'
    d02 = docx.Document()
    d02.add_paragraph('2 开标一览表')
    t1b = d02.add_table(rows=3, cols=3)
    t1b.cell(0, 0).text = '投标人名称'
    t1b.cell(0, 1).merge(t1b.cell(0, 2))
    t1b.cell(0, 1).text = '投标总价（大写/小写，单位：元）'
    t1b.cell(1, 0).text = '测试公司'
    t1b.cell(1, 1).text = '人民币小写：'
    t1b.cell(1, 2).text = '1395000'
    t1b.cell(2, 0).text = '测试公司'
    t1b.cell(2, 1).text = '人民币大写：'
    t1b.cell(2, 2).text = '壹佰叁拾玖万伍仟'
    d02.save(str(bid_02))

    r02 = run_price_check(bid_02)
    assert r02['status'] == 'OK'
    assert r02['bid_total'] == '1395000.00'
    assert r02['bid_total_upper'] == '壹佰叁拾玖万伍仟'
    assert any(f['code'] == 'UPPER_MISMATCH' for f in r02['findings'])


def test_e29_price_llm_table_perception_with_cell_coordinate_verification(tmp_path):
    """④ 规则 NEED_MANUAL 时触发 LLM 表格坐标感知；Python 按坐标回读原格并用 Decimal 复算，拦截伪造坐标。"""
    tender_p = tmp_path / 'tender_cap.txt'
    tender_p.write_text('最高限价：100000.00元。\n', encoding='utf-8', newline='\n')

    # 故意构造非标表头（避开确定性列名映射，确保确定性规则进入 NEED_MANUAL，且含报价/元线索进入 LLM 感知窗口）
    bid_p = tmp_path / 'weird_price.docx'
    doc = docx.Document()
    tbl = doc.add_table(rows=3, cols=4)
    for ci, h in enumerate(['收费科目', '交付规模', '基准费率(元)', '测算核定数(元)']):
        tbl.cell(0, ci).text = h
    for ci, v in enumerate(['核心算法引擎授权', '2套', '50000', '100500']):  # 2*50000=100000 vs 100500 错算500
        tbl.cell(1, ci).text = v
    tbl.cell(2, 0).text = '最终包干报价'
    tbl.cell(2, 1).text = ''
    tbl.cell(2, 2).text = ''
    tbl.cell(2, 3).text = '100500.00'
    doc.save(str(bid_p))

    det_only = run_price_check(bid_p, tender_path=tender_p, client=None)
    assert det_only['status'] == 'NEED_MANUAL'

    class _ValidCoordClient:
        def chat(self, messages, temperature=0):
            payload = {
                'bid_total_coord': 'T0R2C3',
                'bid_total_quote': '100500.00',
                'bid_total_unit_wan': False,
                'items': [
                    {
                        'name_coord': 'T0R1C0',
                        'qty_coord': 'T0R1C1',
                        'unit_price_coord': 'T0R1C2',
                        'total_price_coord': 'T0R1C3',
                    }
                ],
            }
            return {'content': json.dumps(payload, ensure_ascii=False)}

    llm_ok = run_price_check(bid_p, tender_path=tender_p, client=_ValidCoordClient())
    assert llm_ok['status'] == 'OK'
    assert llm_ok['llm_status'] == 'done'
    assert llm_ok['bid_total'] == '100500.00'
    assert len(llm_ok['items']) == 1 and llm_ok['items'][0]['unit_price'] == '50000.00'
    codes = {f['code'] for f in llm_ok['findings']}
    assert 'ITEM_ARITHMETIC' in codes
    assert 'CAP_EXCEEDED' in codes

    class _FakeCoordClient:
        def chat(self, messages, temperature=0):
            payload = {
                'bid_total_coord': 'T0R2C3',
                'bid_total_quote': '999999.00',  # 与原格 '100500.00' 不符，须被强校验拒绝
                'items': [
                    {
                        'name_coord': 'T9R9C9',  # 不存在坐标
                        'qty_coord': 'T0R1C1',
                        'unit_price_coord': 'T0R1C2',
                        'total_price_coord': 'T0R1C3',
                    }
                ],
            }
            return {'content': json.dumps(payload, ensure_ascii=False)}

    llm_rejected = extract_price_data(bid_p, tender_path=tender_p, client=_FakeCoordClient())
    assert llm_rejected['status'] == 'NEED_MANUAL'
    assert llm_rejected['bid_total'] is None
    assert llm_rejected['items'] == []


def test_e29_ocr_progress_broadcasts_warm_cache_and_zero_frame(tmp_path, monkeypatch):
    """③ Stage 1.5 OCR 进度回调：全缓存命中播报加载张数，冷启动第 0 帧立即播报 0/total。"""
    import backend.worker as worker_mod
    from backend.database import Base, get_engine, get_session_factory
    from backend.models import BidCheckTask, Tenant
    from backend.worker import BidTaskWorker, _cleanup_task_deviation_artifacts

    tender_txt = tmp_path / 't.txt'
    bid_txt = tmp_path / 'b.txt'
    tender_txt.write_text('第一章 招标公告\n★1. 质保期不少于1年。\n', encoding='utf-8', newline='\n')
    bid_txt.write_text('投标响应：★1. 质保期1年。\n', encoding='utf-8', newline='\n')

    scenarios = [
        ('warm_cache', [(0, 0, 42)]),
        ('cold_start_zero', [(0, 8, 0), (8, 8, 0)]),
    ]

    for sc_name, cb_events in scenarios:
        db_file = tmp_path / f'{sc_name}.db'
        eng = get_engine(f"sqlite:///{db_file.as_posix()}")
        Base.metadata.create_all(bind=eng)
        SessionMaker = get_session_factory(eng)
        s = SessionMaker()
        try:
            tenant = Tenant(name=f'OCR-{sc_name}', is_active=True)
            s.add(tenant)
            s.flush()
            task = BidCheckTask(
                tenant_id=tenant.id,
                task_type='pre_check',
                title=f'OCR进度-{sc_name}',
                status='created',
                progress=0,
                tender_filename='t.txt',
                bid_filename='b.docx',
                tender_path=str(tender_txt),
                bid_path=str(bid_txt),
            )
            s.add(task)
            s.commit()
            tid = task.id
        finally:
            s.close()

        orig_parse = parser_mod.parse_document

        def _fake_parse(file_path, *, ocr_images=True, progress_cb=None):
            res = orig_parse(file_path, ocr_images=ocr_images, progress_cb=None)
            if progress_cb is not None:
                for ev in cb_events:
                    progress_cb(*ev)
            return res

        monkeypatch.setattr(worker_mod, 'parse_document', _fake_parse)
        worker = BidTaskWorker(session_factory=SessionMaker, max_workers=1)
        emitted: list[str] = []
        orig_emit = worker._emit_stage

        def _spy_emit(session, task_obj, status, progress, detail):
            emitted.append(str(detail))
            return orig_emit(session, task_obj, status, progress, detail)

        monkeypatch.setattr(worker, '_emit_stage', _spy_emit)
        try:
            worker.submit_check_task(tid, sync=True)
            if sc_name == 'warm_cache':
                assert any('从本地缓存' in d and '42' in d for d in emitted), emitted
            else:
                assert any('0 / 8' in d for d in emitted), emitted
                assert any('8 / 8' in d for d in emitted), emitted
        finally:
            _cleanup_task_deviation_artifacts(tid)
            worker.shutdown(wait=False)
            eng.dispose()


def test_e29_real_corpus_04_and_02_packages_regression():
    """真标书实弹回归：北交大 04/02 包 F2 总价与分项全部自动提取成功，F4 跳过目录与第22页湖州合同图片并保留真实负偏离。"""
    root = Path(__file__).resolve().parents[1]
    t04 = root / 'data' / 'corpus' / 'm0_samples' / '标记版本（发售版）04包-北京交通大学数据库稳定性运行测试与优化 _招标文件10.23 (1).pdf'
    b04 = root / 'data' / 'corpus' / 'truebids' / '安易—04包-工业物性数据应用开发平台.docx'
    t02 = root / 'data' / 'corpus' / 'm0_samples' / '（发售版）02包-北京交通大学数据库稳定性运行测试与优化 _招标文件10.23.pdf'
    b02 = root / 'data' / 'corpus' / 'truebids' / '安易—02包.docx'

    if not all(p.is_file() for p in (t04, b04, t02, b02)):
        pytest.skip('本地未放置北交大 02/04 包真实语料，跳过实弹回归')

    # F2 04包与02包实弹
    p04 = run_price_check(b04, tender_path=t04)
    assert p04['status'] == 'OK'
    assert p04['bid_total'] == '470000.00'
    assert p04['bid_total_upper'] == '肆拾柒万元'
    assert p04['price_cap'] == '500000.00'
    assert len(p04['items']) == 1 and p04['items'][0]['total_price'] == '470000.00'
    assert p04['findings'] == []

    p02 = run_price_check(b02, tender_path=t02)
    assert p02['status'] == 'OK'
    assert p02['bid_total'] == '1395000.00'
    assert p02['bid_total_upper'] == '壹佰叁拾玖万伍仟'
    assert p02['price_cap'] == '1400000.00'
    assert any(f['code'] == 'UPPER_MISMATCH' for f in p02['findings'])

    # F4 04包（跳过目录页，定位偏离表 + 保留四、真实售后段用于负偏离检出）
    b04_doc = parse_document(str(b04), ocr_images=True)
    reqs04 = [
        {
            'clause_no': '采购需求-工业物性数据知识库',
            'mark': '★',
            'text': '工业物性数据知识库 包括物性数据库手册、用户指南、物质属性列表、数据质量报告等。接入大语言模型理解工业自然语言，并生成高质量的文本内容，用户可直接使用自然语言提出问题、下达指令，构建面向物性数据的知识库，将迅速理解并提供有针对性的回应。',
        },
        {
            'clause_no': '采购需求-知识库扩展构建',
            'mark': '★',
            'text': '知识库扩展构建 支持直接上传文档、自动爬取在线文档，支持文本自动拆分、向量化、RAG（检索增强生成），智能问答交互体验好；',
        },
        {
            'clause_no': '采购需求-无缝嵌入',
            'mark': '★',
            'text': '无缝嵌入 支持零编码快速嵌入到第三方业务系统，让已有系统快速拥有智能问答能力，提高用户满意度；',
        },
        {
            'clause_no': '四、',
            'mark': '★',
            'text': '须上门现场安装调试；现场安装调试后，对技术人员进行3天使用操作培训；现场测试、验收，在系统保证期内，卖方应提供系统软件升级。在系统保证期内，设备若有故障，卖方应在接到用户故障报告后24小时内做出响应，必要时应能在72小时内派技术人员上门进行维修更换。',
        },
    ]
    f4_04 = run_response_check(t04, b04, requirements=reqs04, client=None, bid_parsed_doc=b04_doc)
    r04_map = {r['clause_no']: r for r in f4_04['star_results']}
    for cno in ('采购需求-工业物性数据知识库', '采购需求-知识库扩展构建', '采购需求-无缝嵌入'):
        assert r04_map[cno]['status'] == 'RESPONSE_OK'
        assert r04_map[cno]['bid_start'] is not None and r04_map[cno]['bid_start'] >= 110000
    assert r04_map['四、']['bid_start'] is not None and r04_map['四、']['bid_start'] >= 90000
    assert '24' in r04_map['四、']['bid_quote']

    # F4 02包（跳过第22页湖州历史合同图片 OCR）
    b02_doc = parse_document(str(b02), ocr_images=True)
    reqs02 = [
        {
            'clause_no': '五、',
            'mark': '★',
            'text': '五、验收标准：按照技术参数逐条进行验收，不超过3个月完成现场验收。',
        },
        {
            'clause_no': '六、',
            'mark': '★',
            'text': '六、售后服务：质保期自验收合格之日起不少于1年',
        },
    ]
    f4_02 = run_response_check(t02, b02, requirements=reqs02, client=None, bid_parsed_doc=b02_doc)
    huzhou_spans = [
        sp for sp in getattr(b02_doc, 'media_ocr_spans', [])
        if '湖州' in b02_doc.text[sp['start']:sp['end']]
    ]
    assert huzhou_spans, '02包语料中应识别出湖州历史合同内嵌图片 OCR 区间'
    for item in f4_02['star_results']:
        assert not any(sp['start'] <= (item['bid_start'] or -1) < sp['end'] for sp in huzhou_spans)
    assert f4_02['self_contradictions'] == []
