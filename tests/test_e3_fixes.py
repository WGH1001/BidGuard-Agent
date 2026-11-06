# -*- coding: utf-8 -*-
"""E3 / E3R · 体验回归六项修复与交叉复查实锤单元测试（断言严格数数，禁空循环假绿）。

覆盖验收点：
1. [实锤 1 · §2.1] docx 表格路径的空白格式表与行号序列（1/2/3、1/2.00/3.00、省略号、表头黑名单）：
   - 构造品目名称为真实名称（如"核心交换机"）、数量/单价/合价恰为 1/2/3 的 docx 表格 ->
     断言 items == []、status == 'NEED_MANUAL'、findings == []（绝不报单价×数量=2.00，填写合价=3.00 假算术错）；
2. [§2.2] PDF 页码映射（带 page label 印刷页码 vs 无 label 回退物理页码，docx 恒为 None）：
   - 构造带自定义 page label（如从第 10 页起跳）与默认物理页码的 PDF，断言 build_review_checklist
     每条记录的 page 字段精确命中印刷页码/物理页号；docx 的 page 恒为 None；
3. [§2.3] 碎片过滤：
   - 单字/双字无动宾语义碎片（如 "▲值回"）降级为 usage_note 不进 requirement 清单；
   - 正常短要求（如 "▲残值回"、"▲资金使"、"★（1）常规制片"）100% 保持 requirement 零误伤；
4. [§2.4] 同文对齐零漏答：
   - 构造含括号长句、分句与短要求的 docx，两份完全相同的 docx 跑 run_response_check ->
     断言 MISSING_RESPONSE 数量严格为 0，且 100% 走 direct_gate_hits（0ms 直通门控）；
5. [实锤 B · §2b] 同一体检任务连续两次 run 的 SSE 终态帧清理与进度从 0 推进验证：
   - 断言第一次 run 完成后 latest 为 ('done', ...)，第二次触发 submit_check_task 时旧终态帧被立即清除，
     且订阅流能完整收到从 progress=0 到 delivered(100) 的推进序列。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from docx import Document
import pymupdf

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.database import Base, get_engine, get_session_factory
from backend.models import BidCheckTask, Tenant
from backend.worker import BidTaskWorker
from core.hybrid_retriever import get_hybrid_stats, reset_hybrid_stats
from core.parser import build_pdf_page_spans, offset_to_page
from core.price_extractor import extract_price_data, run_price_check
from core.response_checker import run_response_check
from core.tender_extractor import build_review_checklist, extract_marked_clauses


def _save_temp_docx(doc: Document) -> Path:
    fd, path = tempfile.mkstemp(suffix='.docx')
    os.close(fd)
    doc.save(path)
    return Path(path)


# ── 1. 实锤 1（§2.1）：docx 表格路径真实品目名 + 1/2/3 行号序列严禁误读为报价 ──

def test_docx_table_lineno_sequence_123_returns_empty_and_need_manual():
    doc = Document()
    doc.add_paragraph('第六章 投标文件格式 —— 分项报价表（样表）')
    tbl = doc.add_table(rows=4, cols=5)
    for col_i, h in enumerate(['序号', '品目名称', '数量', '单价（元）', '合价（元）']):
        tbl.cell(0, col_i).text = h

    # 行 1：品目名称为真实设备名，数量/单价/合价填的是列序号 1 / 2 / 3
    tbl.cell(1, 0).text = '1'
    tbl.cell(1, 1).text = '核心万兆交换机'
    tbl.cell(1, 2).text = '1'
    tbl.cell(1, 3).text = '2'
    tbl.cell(1, 4).text = '3'

    # 行 2：品目名称为真实软件名，数量/单价/合价填的是 1 / 2.00 / 3.00
    tbl.cell(2, 0).text = '2'
    tbl.cell(2, 1).text = '日志审计与态势感知软件'
    tbl.cell(2, 2).text = '1台'
    tbl.cell(2, 3).text = '2.00'
    tbl.cell(2, 4).text = '3.00'

    # 行 3：含省略号占位符
    tbl.cell(3, 0).text = '3'
    tbl.cell(3, 1).text = '数据备份一体机'
    tbl.cell(3, 2).text = '…'
    tbl.cell(3, 3).text = '…'
    tbl.cell(3, 4).text = '…'

    p = _save_temp_docx(doc)
    try:
        res = extract_price_data(p)
        assert res['items'] == []
        assert res['status'] == 'NEED_MANUAL'

        chk = run_price_check(p)
        assert chk['items'] == []
        assert chk['status'] == 'NEED_MANUAL'
        assert chk['findings'] == []
    finally:
        p.unlink(missing_ok=True)


# ── 2. §2.2：PDF 页码映射（优先 page.get_label() 印刷页码，无 label 回退物理页码，docx 恒为 None）──

def test_pdf_page_label_and_physical_fallback_and_docx_null():
    fd_pdf, pdf_str = tempfile.mkstemp(suffix='.pdf')
    os.close(fd_pdf)
    pdf_path = Path(pdf_str)

    # 构造 2 页 PDF，并将印刷页码 label 设置为从第 11 页开始（第1页->'11'，第2页->'12'）
    pdf_doc = pymupdf.open()
    p1 = pdf_doc.new_page()
    p1.insert_text((72, 72), "第一章 投标邀请\n★1. 投标人须具备合法有效的营业执照。", fontname="china-s", fontsize=12)
    p2 = pdf_doc.new_page()
    p2.insert_text((72, 72), "第五章 评标办法\n★2. 核心平台须支持国产银河麒麟操作系统 V10。", fontname="china-s", fontsize=12)
    try:
        pdf_doc.set_page_labels([{'startpage': 0, 'prefix': '', 'style': 'D', 'firstpagenum': 11}])
        has_custom_label = (pdf_doc[0].get_label() == '11')
    except Exception:
        has_custom_label = False
    pdf_doc.save(str(pdf_path))
    pdf_doc.close()

    # 构造对照 docx
    docx_doc = Document()
    docx_doc.add_paragraph('第一章 采购需求\n★1. 系统须支持双机热备。')
    docx_path = _save_temp_docx(docx_doc)

    try:
        spans = build_pdf_page_spans(pdf_path)
        assert len(spans) == 2
        expected_p1 = 11 if has_custom_label else 1
        expected_p2 = 12 if has_custom_label else 2
        assert offset_to_page(spans[0][0] + 2, spans) == expected_p1
        assert offset_to_page(spans[1][0] + 2, spans) == expected_p2

        checklist_pdf = build_review_checklist(str(pdf_path))
        marked_pdf = checklist_pdf['marked_clauses']
        assert len(marked_pdf) == 2
        assert marked_pdf[0]['page'] == expected_p1
        assert marked_pdf[1]['page'] == expected_p2

        checklist_docx = build_review_checklist(str(docx_path))
        marked_docx = checklist_docx['marked_clauses']
        assert len(marked_docx) == 1
        assert marked_docx[0]['page'] is None
    finally:
        pdf_path.unlink(missing_ok=True)
        docx_path.unlink(missing_ok=True)


# ── 3. §2.3：碎片过滤（"▲值回" 降级 usage_note 不进 requirement，正常短要求零误伤）──

def test_short_fragment_filtered_without_hurting_valid_short_requirements():
    text = (
        '▲值回\n'
        '上述为表格断行残词。\n'
        '▲残值回\n'
        '上述为滁州物业 3 字真功能项。\n'
        '▲资金使\n'
        '上述为滁州物业资金使用监测真功能项。\n'
        '★（1）常规制片\n'
        '上述为南京病理带编号短要求。\n'
    )
    clauses = extract_marked_clauses(text)
    by_text = {c['text']: c['kind'] for c in clauses}

    # "▲值回" 必须被拦在 requirement 之外（降级为 usage_note）
    assert by_text['▲值回'] == 'usage_note'
    # 其余 3 条正常短要求必须 100% 保持 requirement
    assert by_text['▲残值回'] == 'requirement'
    assert by_text['▲资金使'] == 'requirement'
    assert by_text['★（1）常规制片'] == 'requirement'

    reqs_only = [c for c in clauses if c['kind'] == 'requirement']
    assert len(reqs_only) == 3


# ── 4. §2.4：两份完全相同的 docx（含括号长句、分句与短要求）跑 F4 漏应答必须为 0 ──

def test_identical_docx_zero_missing_response_and_direct_gate():
    reset_hybrid_stats()
    paras = [
        '第三章 采购需求与技术规格',
        '★（1）常规制片',
        '上述为第一项短要求。',
        '▲资金使',
        '用监测——对维修资金使用的全流程情况进行实时监测（须提供系统功能页面截图）。',
        '★2. 系统须支持分布式微服务架构（包含服务注册发现、配置中心、链路追踪；支持容器化一键部署）。',
        '上述为架构要求。',
    ]
    doc1 = Document()
    doc2 = Document()
    for p in paras:
        doc1.add_paragraph(p)
        doc2.add_paragraph(p)

    p1 = _save_temp_docx(doc1)
    p2 = _save_temp_docx(doc2)
    try:
        res = run_response_check(p1, p2, client=None)
        missing = [f for f in res['findings'] if f['code'] == 'MISSING_RESPONSE']
        assert len(missing) == 0
        assert res['alignment_summary']['total_requirements'] == 3
        assert res['alignment_summary']['aligned_hits'] == 3
        assert res['alignment_summary']['direct_gate_hits'] == 3
        assert res['alignment_summary']['hybrid_gate_hits'] == 0
        assert get_hybrid_stats()['total_calls'] == 0
    finally:
        p1.unlink(missing_ok=True)
        p2.unlink(missing_ok=True)


# ── 5. 实锤 B（§2b）：同一任务连续两次 run，SSE 终态帧清理且进度从 0 推进到 100 ──

def test_rerun_same_task_clears_stale_done_frame_and_progresses_from_zero(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'test_e3_rerun.db'
    eng = get_engine(f"sqlite:///{db_file.as_posix()}")
    Base.metadata.create_all(bind=eng)
    SessionMaker = get_session_factory(eng)

    t_doc = Document()
    t_doc.add_paragraph('★1. 系统须支持国产银河麒麟操作系统 V10。')
    t_doc.add_paragraph('上述为基础环境要求。')
    t_path = _save_temp_docx(t_doc)

    b_doc = Document()
    b_doc.add_paragraph('★1. 系统须支持国产银河麒麟操作系统 V10。（我方完全满足）')
    b_path = _save_temp_docx(b_doc)

    session = SessionMaker()
    try:
        tenant = Tenant(name='重跑测试部', is_active=True)
        session.add(tenant)
        session.flush()
        task = BidCheckTask(
            tenant_id=tenant.id,
            task_type='pre_check',
            title='连续两次运行测试任务',
            status='created',
            progress=0,
            tender_filename='t.docx',
            bid_filename='b.docx',
            tender_path=str(t_path),
            bid_path=str(b_path),
        )
        session.add(task)
        session.commit()
        task_id = task.id
    finally:
        session.close()

    worker = BidTaskWorker(session_factory=SessionMaker, max_workers=2)
    try:
        # 第一次运行至 delivered (100%)
        worker.submit_check_task(task_id, sync=True)
        latest_after_run1 = worker.broadcaster.latest(task_id)
        assert latest_after_run1 is not None
        assert latest_after_run1[0] == 'done'
        assert latest_after_run1[1]['status'] == 'delivered'

        # 订阅队列，准备观测第二次运行帧序列
        sub_q = worker.broadcaster.subscribe(task_id)
        try:
            # 触发第二次运行：验证 _reset_task_for_rerun 立即把首帧置为 progress=0，随后推进至 20->55->85->done
            worker.submit_check_task(task_id, sync=True)
            frames = []
            while not sub_q.empty():
                frames.append(sub_q.get_nowait())

            assert len(frames) >= 4
            # 第一帧必须是重置后的 progress=0 帧（绝不是残留的上一轮 done 帧）
            assert frames[0][0] == 'progress'
            assert frames[0][1]['progress'] == 0
            # 最后一帧必须是本轮新完成的 done 帧
            assert frames[-1][0] == 'done'
            assert frames[-1][1]['status'] == 'delivered'

            prog_values = [f[1].get('progress', 100) for f in frames if f[0] == 'progress']
            assert prog_values[0] == 0
            assert any(p > 0 for p in prog_values)
        finally:
            worker.broadcaster.unsubscribe(task_id, sub_q)
    finally:
        worker.shutdown(wait=True)
        t_path.unlink(missing_ok=True)
        b_path.unlink(missing_ok=True)
        eng.dispose()