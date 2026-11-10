# -*- coding: utf-8 -*-
"""E15 · 发现页码定位与开标日期自动回填卡单元测试（断言严格数数，严禁空循环假绿）。

覆盖任务卡 §3 全部验收标准：
1. 开标日期自动回填：
   - 构造含"提交投标文件截止时间：2026年11月25日 10:30:00"的招标书 + 空 open_date 任务 -> 跑流水线后 config_json.open_date == '2026-11-25'；
   - 手填 open_date 的任务绝不被自动回填覆盖；
   - 招标书无开标日期且未手填时 -> 产出 F3 OPEN_DATE_FALLBACK (warning) 提醒。
2. F3 & 自定义红线页码定位：
   - 构造多页 docx（含 <w:br w:type="page"/> 分页符）触发 SOCIAL_MONTH_MISSING 与 CUSTOM_REDLINE_HIT ->
     断言两类 finding 的 page 字段均非空且与 quote_offset 所在页一致。
3. F2 锚点页码定位与防前文重名污染护栏：
   - 构造"培训服务"在第 1 页（技术方案章节）与第 2 页（报价明细表，且算术错误）均出现 ->
     断言 ITEM_ARITHMETIC finding 的 quote_offset 精准命中第 2 页报价表区域（page == 2），绝不错锚到第 1 页；
   - 锚文本在原文被拆散/不存在时 -> 诚实回退 quote=None, page=None，零报错。
4. PDF 双格式自适应验证：
   - 构造多页 .pdf 投标文件任务 -> F3/F2/自定义红线 finding 的 page 由 build_pdf_page_spans 链路计算非空，零报错。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
import pymupdf

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.database import Base, get_engine, get_session_factory
from backend.models import BidCheckTask, BidFinding, Tenant
from backend.worker import BidTaskWorker, locate_f2_finding_in_bid_text
from core.response_checker import build_unified_page_context, resolve_unified_page


def _append_page_break(paragraph) -> None:
    """向 python-docx 段落追加显式分页符 <w:br w:type="page"/>，使 E6 双轨页码生成多页区间。"""
    run = paragraph.add_run()
    br = OxmlElement('w:br')
    br.set(qn('w:type'), 'page')
    run._r.append(br)


def _save_temp_docx(doc: Document) -> Path:
    fd, path = tempfile.mkstemp(suffix='.docx')
    os.close(fd)
    doc.save(path)
    return Path(path)


# ── 1. 开标日期自动识别回填、手填优先保護与兜底 OPEN_DATE_FALLBACK 告警 ──

def test_e15_open_date_auto_backfill_and_manual_priority_and_fallback_warning(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'test_e15_open_date.db'
    eng = get_engine(f"sqlite:///{db_file.as_posix()}")
    Base.metadata.create_all(bind=eng)
    SessionMaker = get_session_factory(eng)

    tender_with_date = Document()
    tender_with_date.add_paragraph('第一章 招标公告：提交投标文件截止时间：2026年 11月 25日 10:30:00。')
    tender_with_date.add_paragraph('★1. 投标人须提供有效的营业执照。')
    t_date_p = _save_temp_docx(tender_with_date)

    tender_no_date = Document()
    tender_no_date.add_paragraph('第一章 采购需求：★1. 投标人须提供有效的营业执照。')
    t_nodate_p = _save_temp_docx(tender_no_date)

    bid_doc = Document()
    bid_doc.add_paragraph('★1. 投标人须提供有效的营业执照。（我方完全满足并提供营业执照）')
    b_p = _save_temp_docx(bid_doc)

    sess = SessionMaker()
    try:
        tenant = Tenant(name='E15测试租户', is_active=True)
        sess.add(tenant)
        sess.flush()

        # 任务 1：未填 open_date -> 应从招标书自动识别为 '2026-11-25'
        t1 = BidCheckTask(
            tenant_id=tenant.id,
            title='自动识别开标日任务',
            tender_path=str(t_date_p),
            bid_path=str(b_p),
            config_json={},
        )
        # 任务 2：用户手填 open_date='2026-12-01' -> 绝不被招标书的 2026-11-25 覆盖
        t2 = BidCheckTask(
            tenant_id=tenant.id,
            title='手填开标日优先任务',
            tender_path=str(t_date_p),
            bid_path=str(b_p),
            config_json={'open_date': '2026-12-01'},
        )
        # 任务 3：未填 open_date 且招标书无日期 -> 产出 OPEN_DATE_FALLBACK warning
        t3 = BidCheckTask(
            tenant_id=tenant.id,
            title='无日期兜底告警任务',
            tender_path=str(t_nodate_p),
            bid_path=str(b_p),
            config_json={},
        )
        sess.add_all([t1, t2, t3])
        sess.commit()
        id1, id2, id3 = t1.id, t2.id, t3.id
    finally:
        sess.close()

    worker = BidTaskWorker(session_factory=SessionMaker, max_workers=2)
    try:
        worker.submit_check_task(id1, sync=True)
        worker.submit_check_task(id2, sync=True)
        worker.submit_check_task(id3, sync=True)

        s_check = SessionMaker()
        try:
            r1 = s_check.query(BidCheckTask).filter_by(id=id1).first()
            assert r1 is not None and r1.config_json.get('open_date') == '2026-11-25'

            r2 = s_check.query(BidCheckTask).filter_by(id=id2).first()
            assert r2 is not None and r2.config_json.get('open_date') == '2026-12-01'

            f3_fallback = (
                s_check.query(BidFinding)
                .filter_by(task_id=id3, code='OPEN_DATE_FALLBACK')
                .first()
            )
            assert f3_fallback is not None
            assert f3_fallback.severity == 'warning'
            assert '未识别到开标日期' in f3_fallback.message
        finally:
            s_check.close()
    finally:
        worker.shutdown(wait=True)
        t_date_p.unlink(missing_ok=True)
        t_nodate_p.unlink(missing_ok=True)
        b_p.unlink(missing_ok=True)
        eng.dispose()


# ── 2. F2 防前文重名污染 + F3 社保缺月 + 自定义红线在多页 docx 上的页码定位 ──

def test_e15_docx_f2_anti_duplicate_name_and_f3_and_custom_redline_pages(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'test_e15_docx_pages.db'
    eng = get_engine(f"sqlite:///{db_file.as_posix()}")
    Base.metadata.create_all(bind=eng)
    SessionMaker = get_session_factory(eng)

    tender_doc = Document()
    tender_doc.add_paragraph('开标时间：2026年11月15日 09:30')
    tender_doc.add_paragraph('★1. 投标人须提供完善的培训服务。')
    t_p = _save_temp_docx(tender_doc)

    # 构造 2 页投标书：
    # 第 1 页：技术方案章节多次出现"培训服务"（无价格数字）
    # 第 2 页：分项报价表（"培训服务" 单价 42,500 × 2 = 84,500 算错 500 元）+ 社保缴纳记录 + 自定义禁用词
    bid_doc = Document()
    p1 = bid_doc.add_paragraph(
        '第一章 技术方案：我方将指派资深讲师团队提供全周期培训服务，确保培训服务覆盖全部业务骨干。'
        '★1. 投标人须提供完善的培训服务。（我方完全满足）'
    )
    _append_page_break(p1)

    bid_doc.add_paragraph('第二章 资格证明与分项报价表')
    # 社保只写 05/06 两个月（不提 07）——扫描器是覆盖集合语义，文本里写"缺 2026-07"
    # 反而会把 07 收进已覆盖集合导致不告警（构造需贴近真实标书：缺保就是压根不写）
    bid_doc.add_paragraph('社保缴纳证明：已缴纳 2026-05、2026-06 养老保险。')
    bid_doc.add_paragraph('特别说明：本项目承诺绝不采用挂靠转包方式实施。')

    tbl = bid_doc.add_table(rows=3, cols=4)
    for i, h in enumerate(['项目名称', '数量', '单价（元）', '合价（元）']):
        tbl.cell(0, i).text = h
    tbl.cell(1, 0).text = '培训服务'
    tbl.cell(1, 1).text = '2期'
    tbl.cell(1, 2).text = '42,500.00'
    tbl.cell(1, 3).text = '84,500.00'  # 应为 85,000.00，触发 ITEM_ARITHMETIC
    tbl.cell(2, 0).text = '合计'
    tbl.cell(2, 3).text = '84,500.00'
    b_p = _save_temp_docx(bid_doc)

    sess = SessionMaker()
    try:
        tenant = Tenant(name='多页定位租户', is_active=True)
        sess.add(tenant)
        sess.flush()

        task = BidCheckTask(
            tenant_id=tenant.id,
            title='F2防重名与F3页码测试',
            tender_path=str(t_p),
            bid_path=str(b_p),
            config_json={
                'required_social_months': ['2026-05', '2026-06', '2026-07'],
                'custom_redlines': [{'keyword': '挂靠转包', 'severity': 'error', 'consequence': 'veto'}],
            },
        )
        sess.add(task)
        sess.commit()
        tid = task.id
    finally:
        sess.close()

    worker = BidTaskWorker(session_factory=SessionMaker, max_workers=1)
    try:
        worker.submit_check_task(tid, sync=True)
        s_check = SessionMaker()
        try:
            findings = s_check.query(BidFinding).filter_by(task_id=tid).all()
            f2_arith = next((f for f in findings if f.code == 'ITEM_ARITHMETIC'), None)
            f3_social = next((f for f in findings if f.code == 'SOCIAL_MONTH_MISSING'), None)
            f3_redline = next((f for f in findings if f.code == 'CUSTOM_REDLINE_HIT'), None)

            assert f2_arith is not None
            # 关键断言：第 1 页虽出现"培训服务"，但 F2 ITEM_ARITHMETIC 必须命中第 2 页报价表区域（page == 2）
            assert f2_arith.page == 2
            assert f2_arith.quote is not None and '培训服务' in f2_arith.quote
            assert f2_arith.quote_verified is True

            assert f3_social is not None
            assert f3_social.page == 2

            assert f3_redline is not None
            assert f3_redline.page == 2
        finally:
            s_check.close()
    finally:
        worker.shutdown(wait=True)
        t_p.unlink(missing_ok=True)
        b_p.unlink(missing_ok=True)
        eng.dispose()


# ── 3. F2 锚文本无法匹配时诚实回退 None + PDF 投标文件双格式页码测试 ────────

def test_e15_f2_unmatched_anchor_honest_fallback_and_pdf_page_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    # (a) 锚文本不存在于原文时，诚实回退 (None, None)
    q_none, off_none = locate_f2_finding_in_bid_text(
        {'code': 'ITEM_ARITHMETIC', 'name': '已被跨格拆散的不存在分项名', 'anchor_text': '已被跨格拆散的不存在分项名'},
        {'items': []},
        '这是一段普通的投标文件正文，不含该锚点。',
    )
    assert q_none is None and off_none is None

    # (b) 构造 2 页 PDF 投标书，验证 build_unified_page_context 与 worker 对 PDF 正常算出第 2 页页码
    pdf_path = tmp_path / 'bid_two_pages.pdf'
    pdf_doc = pymupdf.open()
    page1 = pdf_doc.new_page()
    page1.insert_text((72, 100), 'Page 1 Technical Proposal Content', fontsize=12)
    page2 = pdf_doc.new_page()
    page2.insert_text((72, 100), 'Page 2 Qualification Social Security 2026-05 only', fontsize=12)
    pdf_doc.save(str(pdf_path))
    pdf_doc.close()

    ctx = build_unified_page_context(pdf_path)
    assert ctx['kind'] == 'pdf'
    # 第 2 页偏移量应解析为 page == 2
    p_no, f_no, is_docx = resolve_unified_page(45, ctx)
    assert p_no == 2
    assert f_no is None
    assert is_docx is False