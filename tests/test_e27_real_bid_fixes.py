# -*- coding: utf-8 -*-
"""E27 真标书实战排障与交互体验深化卡 — 专项回归与实弹探针测试。"""
from __future__ import annotations

from pathlib import Path
import pytest

from backend.worker import resolve_f3_suggestion, resolve_f4_category_and_suggestion
from core.llm_gateway import LLMClient, _format_gateway_error_hint
from core.parser import parse_document
from core.price_extractor import (
    _detect_target_package_no,
    extract_price_cap,
    extract_price_data,
)
from core.qualification_scanner import (
    _collect_social_covered_months,
    resolve_effective_social_requirement,
    scan_qualifications,
)
from core.response_checker import (
    _extract_warranty_mentions,
    _filter_valid_requirements,
    check_bid_self_contradictions,
    check_star_responses,
)
from core.tender_extractor import (
    extract_chapter4_procurement_requirements,
    extract_invalid_bid_clauses,
    locate_chapters,
)


def test_e27_social_security_window_and_dynamic_months():
    """§2.1：2024 年标书不得套用 2026-08~10 默认种子；身份证生日/ISO 效期/财报附注不得误吸为社保月。"""
    tender_text = (
        "第一章 投标邀请\n"
        "开标时间：2024年11月13日13点00分\n"
        "第六章 投标文件格式\n"
        "5、提供投标截止日前最近半年任意一个月的社会保障资金缴纳记录复印件。\n"
    )
    eff_months, min_count = resolve_effective_social_requirement(
        tender_text=tender_text,
        open_date_str='2024-11-13',
        configured_months=['2026-08', '2026-09', '2026-10'],
        is_user_explicit=False,
    )
    assert eff_months == ['2024-05', '2024-06', '2024-07', '2024-08', '2024-09', '2024-10']
    assert min_count == 1

    bid_text = (
        "法定代表人身份证：出生 1977-09-15。\n"
        "ISO9001 质量管理体系认证证书有效期至 2026-10-18。\n"
        "财务报表附注：组织实施的社会基本养老保险、失业保险等；在职工为本公司提供服务的会计期间，"
        "将根据设定提存计划计算的应缴存金额确认为负债，并计入当期损益或相关资产（2022-12, 2023-12）。\n"
        "北京市社会保险个人权益记录（单位缴费信息）\n"
        "费款所属期：2024-07 月 养老保险：24 医疗保险：24 失业保险：24 工伤保险：24 缴费人数 24 人\n"
        "打印日期：2024年11月02日\n"
    )
    covered, quote, _ = _collect_social_covered_months(bid_text, client=None)
    assert covered == {'2024-07'}
    assert '1977-09' not in covered
    assert '2026-10' not in covered
    assert '2024-11' not in covered
    assert '设定提存计划' not in quote

    findings = scan_qualifications(
        text=bid_text,
        open_date='2024-11-13',
        required_social_months=eff_months,
        social_min_count=min_count,
    )
    assert not any(f['code'] == 'SOCIAL_MONTH_MISSING' for f in findings)


def test_e27_f3_suggestion_mapping():
    """§2.1.2：F3 按发现类型映射专属整改建议，杜绝一刀切文案。"""
    s_social = resolve_f3_suggestion({'code': 'SOCIAL_MONTH_MISSING'})
    s_cert = resolve_f3_suggestion({'code': 'CERT_EXPIRED'})
    s_name = resolve_f3_suggestion({'code': 'FORBIDDEN_NAME'})
    s_place = resolve_f3_suggestion({'code': 'PLACEHOLDER'})
    assert '社保窗口期' in s_social
    assert '有效期内' in s_cert
    assert '外部项目' in s_name
    assert '空白占位符' in s_place


def test_e27_gateway_timeout_and_f4_contradiction_fallback():
    """§2.2.1 & §2.2.2：网关显式超时与内网 TUN 诊断提示；check_bid_self_contradictions 异常不炸流水线。"""
    client = LLMClient(
        env_config={
            'base_url': 'http://10.0.2.81:3000/v1',
            'timeout': 45,
            'models': {'flash': 'GLM-5.3-Flash', 'flagship': 'GLM-5.3-Flash'},
        },
        api_key='dummy-key',
    )
    assert client.timeout == 45.0
    hint = _format_gateway_error_hint('http://10.0.2.81:3000/v1', RuntimeError('Connection error.'))
    assert 'TUN 模式' in hint

    class _BrokenClient:
        def chat(self, *args, **kwargs):
            raise RuntimeError('API 请求失败重试耗尽: Connection error.')

    bid_text = (
        "售后服务承诺一：我方提供完善的质保与维保技术支持服务，保障系统稳定运行。\n"
        "售后服务承诺二：质保期内提供7×24小时售后服务与定期巡检支持。\n"
    )
    # 即使传入抛异常的 client，check_bid_self_contradictions 也必须平稳降级不抛异常
    out = check_bid_self_contradictions(bid_text, client=_BrokenClient())
    assert isinstance(out, list)


def test_e27_qa_withdraw_last_turn_endpoint(tmp_path):
    """§2.2.4：DELETE /api/qa/sessions/{session_id}/last-turn 撤回最后一轮问答。"""
    from fastapi.testclient import TestClient
    from backend.auth import AuthUser, get_current_user
    from backend.database import get_db, get_session_factory, init_db
    from backend.main import create_app
    from backend.models import QaSession

    db_file = tmp_path / 'test_e27_qa.db'
    engine = init_db(f"sqlite:///{db_file.as_posix()}", seed=True)
    session_factory = get_session_factory(engine)
    app = create_app()

    def _override_db():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        id=1, uid=1, username='admin', role='admin', tenant_id=1
    )
    tc = TestClient(app)
    headers = {}

    db = session_factory()
    try:
        qa_sess = QaSession(
            tenant_id=1,
            user_id=1,
            title='测试撤回会话',
            tender_path='data/corpus/m0_samples/sample.txt',
            bid_path='data/corpus/m0_samples/sample.txt',
            messages_json=[
                {'role': 'user', 'content': '第一轮问题', 'attachment_ids': [], 'quotes': [], 'steps': []},
                {'role': 'assistant', 'content': '第一轮回答', 'mode': 'react', 'quotes': [], 'steps': []},
                {'role': 'user', 'content': '第二轮待撤回问题', 'attachment_ids': ['att_1'], 'quotes': [], 'steps': []},
                {'role': 'assistant', 'content': '第二轮半截回答', 'mode': 'keyword_fallback', 'quotes': [], 'steps': []},
            ],
        )
        db.add(qa_sess)
        db.commit()
        db.refresh(qa_sess)
        sid = qa_sess.id
    finally:
        db.close()

    resp = tc.delete(f'/api/qa/sessions/{sid}/last-turn', headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data['session_id'] == sid
    assert data['withdrawn_question'] == '第二轮待撤回问题'
    assert data['withdrawn_attachment_ids'] == ['att_1']
    assert data['remaining_messages'] == 2


def test_e27_tender_clause_role_and_deviation_table_filter():
    """§2.3.1 & §2.3.2：评标办法过滤、递交份数/字迹降级为 info 且不进偏离表、无★标书提取第四章需求。"""
    sample_tender = (
        "第二章 投标人须知\n"
        "14.1 投标文件份数：开标一览表（1份）、正本（1份）、副本（4份）、投标文件电子版（1份），"
        "未按规定递交电子版投标文件的，按无效投标处理。\n"
        "14.8 投标文件内容不全或字迹模糊辨认不清，导致投标文件不满足招标文件实质性要求的，按无效投标处理。\n"
        "第三章 评标办法及标准\n"
        "三、评标办法\n"
        "1、评标委员会根据评分标准对照各投标人的投标文件内容进行比较和评价。\n"
        "6、评标委员会认为投标人的报价明显低于其他通过符合性审查投标人的报价，有可能影响产品质量或者不能诚信履约的，"
        "投标人不能证明其报价合理性的，评标委员会应当将其作为无效投标处理。\n"
        "一、如采购的产品属于强制采购的，投标人必须为投标产品出具由国家确定的认证机构出具的、处于有效期之内的认证证书，"
        "否则其投标将被视为无效投标。\n"
        "第四章采购需求及技术要求\n"
        "二、采购需求及技术要求\n"
        "\uf06c 工业物性数据知识库\n"
        "支持多源物性数据采集、清洗与结构化入库。\n"
        "六、质保期要求：硬件1年质保，软件2年质保。\n"
        "第五章 合同文本\n"
    )
    chapters = locate_chapters(sample_tender)
    ch4 = [c for c in chapters if c['index'] == '第四章']
    assert ch4 and ch4[0]['title'] == '采购需求及技术要求'

    clauses = extract_invalid_bid_clauses(sample_tender)
    roles_by_no = {c['clause_no']: c.get('clause_role') for c in clauses}
    assert roles_by_no.get('14.1') == 'submission_rule'
    assert roles_by_no.get('14.8') == 'submission_rule'
    assert roles_by_no.get('6、') == 'eval_rule'
    assert any(c.get('clause_role') == 'technical_or_commercial' for c in clauses)

    # F4 核验：eval_rule 被剔除，submission_rule 降级为 info 提醒而非 error 漏应答
    f4_reqs = _filter_valid_requirements(clauses, include_submission_rules=True)
    assert not any(c.get('clause_role') == 'eval_rule' for c in f4_reqs)
    assert any(c.get('clause_role') == 'submission_rule' for c in f4_reqs)

    star_out = check_star_responses(
        tender_text=sample_tender,
        bid_text="我方完全响应工业物性数据知识库建设，硬件1年质保，软件2年质保。",
        requirements=f4_reqs,
        client=None,
    )
    sub_findings = [f for f in star_out['findings'] if f.get('clause_no') in ('14.1', '14.8')]
    assert len(sub_findings) == 2
    assert all(f['severity'] == 'info' and f.get('consequence') == 'none' for f in sub_findings)
    cat, sug = resolve_f4_category_and_suggestion(sub_findings[0])
    assert cat == '封标递交与形式自检提醒'
    assert '无需在标书正文增补偏离说明' in sug

    # F5 偏离表过滤：submission_rule 不进入六列偏离表
    f5_reqs = _filter_valid_requirements(clauses, include_submission_rules=False)
    assert not any(c.get('clause_role') == 'submission_rule' for c in f5_reqs)
    assert any(c.get('clause_role') == 'technical_or_commercial' for c in f5_reqs)


def test_e27_compound_warranty_and_blank_template_hint():
    """§2.3.3：同句复合质保（硬件1年质保，软件2年质保）合并展示，并提示前文未填合同模板下划线。"""
    bid_text = (
        "合同通用条款：本合同服务项目的质保期为3年，自_验收合格之日起计算。\n"
        "售后服务承诺：针对本项目，我方承诺投标产品的硬件1年质保，软件2年质保，质保期内提供免费技术支持与售后服务。\n"
    )
    mentions = _extract_warranty_mentions(bid_text)
    assert len(mentions) == 2
    assert mentions[0]['value'] == 36
    assert mentions[1]['value'] == (12, 24)
    assert '12个月 / 24个月（分项复合质保）' in mentions[1]['display']

    findings = check_bid_self_contradictions(bid_text, client=None)
    assert len(findings) == 1
    msg = findings[0]['message']
    assert '12个月 / 24个月（分项复合质保）' in msg
    assert '空白下划线' in msg


def test_e27_multi_package_price_cap():
    """§2.4.2：多分包招标书按目标包号提取分包预算限价，严禁误取全项目总预算。"""
    tender_text = (
        "总预算金额：400万元（人民币）\n"
        "总最高限价：400万元（人民币）\n"
        "包号 分包名称 数量 分包预算（万元）\n"
        "01 石化行业催化过程流场仿真环境 1 110\n"
        "02 多模态聚合物物性智能预测软件 1 140\n"
        "03 特色物性数据库系统 1 100\n"
        "04 工业物性数据应用开发平台 1 50\n"
    )
    assert _detect_target_package_no("安易—04包-工业物性数据应用开发平台.docx") == 4
    assert _detect_target_package_no("安易—02包.docx") == 2
    assert str(extract_price_cap(tender_text, package_no=4)) == '500000.00'
    assert str(extract_price_cap(tender_text, package_no=2)) == '1400000.00'


def test_e27_real_corpus_bjtu_02_04_probe():
    """真标书实弹探针：对安易 04 包与 02 包验证限价、开标一览表扫描件总价与 F3 社保零误报。"""
    root = Path(__file__).resolve().parents[1]
    b04 = root / 'data' / 'corpus' / 'truebids' / '安易—04包-工业物性数据应用开发平台.docx'
    t04 = root / 'data' / 'corpus' / 'm0_samples' / '标记版本（发售版）04包-北京交通大学数据库稳定性运行测试与优化 _招标文件10.23 (1).pdf'
    b02 = root / 'data' / 'corpus' / 'truebids' / '安易—02包.docx'
    t02 = root / 'data' / 'corpus' / 'm0_samples' / '（发售版）02包-北京交通大学数据库稳定性运行测试与优化 _招标文件10.23.pdf'

    if not (b04.is_file() and t04.is_file() and b02.is_file() and t02.is_file()):
        pytest.skip('本地未放置北京交大 02/04 包真实语料，跳过实弹探针')

    p04 = extract_price_data(b04, tender_path=t04)
    assert p04['price_cap'] == '500000.00'
    assert p04['reference_bid_total'] == '470000.00'

    p02 = extract_price_data(b02, tender_path=t02)
    assert p02['price_cap'] == '1400000.00'
    assert p02['reference_bid_total'] == '1400000.00'

    b04_doc = parse_document(str(b04))
    t04_doc = parse_document(str(t04))
    eff_m, min_c = resolve_effective_social_requirement(
        t04_doc.text, '2024-11-13', ['2026-08', '2026-09', '2026-10']
    )
    f3_04 = scan_qualifications(
        b04_doc.text,
        open_date='2024-11-13',
        required_social_months=eff_m,
        social_min_count=min_c,
    )
    assert not any(f['code'] == 'SOCIAL_MONTH_MISSING' for f in f3_04)

    from core.tender_extractor import build_review_checklist

    ck04 = build_review_checklist(str(t04))
    ch4_04 = [c for c in ck04['invalid_bid_clauses'] if c.get('clause_role') == 'technical_or_commercial']
    assert len(ch4_04) == 12
    assert any('工业物性数据知识库' in c.get('text', '') for c in ch4_04)
    assert any(c.get('clause_no') == '六、' and '硬件1 年质保' in c.get('text', '') for c in ch4_04)
    assert any(c.get('clause_no') == '6、' and c.get('clause_role') == 'eval_rule' for c in ck04['invalid_bid_clauses'])

    ck02 = build_review_checklist(str(t02))
    ch4_02 = [c for c in ck02['invalid_bid_clauses'] if c.get('clause_role') == 'technical_or_commercial']
    assert len(ch4_02) == 11
    assert any(c.get('clause_no') == '采购需求-（1）' for c in ch4_02)
    assert any(c.get('clause_no') == '6、' and c.get('clause_role') == 'eval_rule' for c in ck02['invalid_bid_clauses'])


def test_e27_qa_ask_502_no_fallback_pollution_and_last_turn_alias(tmp_path, monkeypatch):
    """§2.2：/ask 网关故障返回 502 QA_GATEWAY_ERROR 且不污染会话历史；支持 DELETE /api/qa/{id}/last-turn 别名路由。"""
    from fastapi.testclient import TestClient
    import backend.database as db_mod
    import backend.routers.qa as qa_router
    from backend.main import create_app
    from backend.models import QaSession

    tender_txt = tmp_path / 'tender.txt'
    bid_txt = tmp_path / 'bid.txt'
    tender_txt.write_text('第一章 招标公告\n最高限价：100万元。\n', encoding='utf-8', newline='\n')
    bid_txt.write_text('第一章 投标函\n投标总价：95万元。\n', encoding='utf-8', newline='\n')

    db_file = tmp_path / 'qa_502_test.db'
    orig_engine, orig_sf = db_mod.engine, db_mod.SessionLocal
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    class _GatewayBrokenClient:
        def chat(self, *args, **kwargs):
            raise RuntimeError('Connection error.')

    try:
        with TestClient(app) as tc:
            r_login = tc.post(
                '/api/auth/login',
                json={'username': 'admin', 'password': 'BidGuard2026!'},
            )
            assert r_login.status_code == 200, r_login.text
            headers = {'Authorization': f"Bearer {r_login.json()['token']}"}

            r_sess = tc.post(
                '/api/qa/sessions',
                headers=headers,
                json={
                    'title': '网关502验证会话',
                    'tender_path': str(tender_txt),
                    'bid_path': str(bid_txt),
                },
            )
            assert r_sess.status_code == 200, r_sess.text
            sid = r_sess.json()['id']

            # 预置 1 轮历史问答，随后验证别名路由与 /ask 502 不污染历史
            with db_mod.SessionLocal() as s:
                row = s.query(QaSession).filter_by(id=sid).first()
                assert row is not None
                row.messages_json = [
                    {'role': 'user', 'content': '第一轮已保存问题', 'attachment_ids': []},
                    {'role': 'assistant', 'content': '第一轮已保存回答', 'quotes': [], 'steps': []},
                ]
                s.commit()

            # 网关故障时同步 /ask 必须显式返回 502 QA_GATEWAY_ERROR，不得降级为 keyword_fallback 并写入 DB
            monkeypatch.setattr(
                qa_router,
                'resolve_effective_llm_client',
                lambda _db, _uid: (_GatewayBrokenClient(), 'broken-test'),
            )
            r_ask = tc.post(
                f'/api/qa/sessions/{sid}/ask',
                headers=headers,
                json={'question': '本次追问遇到网关中断'},
            )
            assert r_ask.status_code == 502, r_ask.text
            detail = r_ask.json().get('detail') or {}
            assert detail.get('code') == 'QA_GATEWAY_ERROR'

            with db_mod.SessionLocal() as s:
                row = s.query(QaSession).filter_by(id=sid).first()
                assert row is not None
                assert len(row.messages_json or []) == 2
                assert row.messages_json[-1]['content'] == '第一轮已保存回答'

            # 隐藏别名路由 DELETE /api/qa/{sid}/last-turn 同样可用且清零剩余消息
            r_del = tc.delete(f'/api/qa/{sid}/last-turn', headers=headers)
            assert r_del.status_code == 200, r_del.text
            del_data = r_del.json()
            assert del_data['session_id'] == sid
            assert del_data['withdrawn_question'] == '第一轮已保存问题'
            assert del_data['remaining_messages'] == 0

            with db_mod.SessionLocal() as s:
                row = s.query(QaSession).filter_by(id=sid).first()
                assert row is not None
                assert (row.messages_json or []) == []
    finally:
        if db_mod.engine is not None and db_mod.engine is not orig_engine:
            db_mod.engine.dispose()
        db_mod.engine = orig_engine
        db_mod.SessionLocal = orig_sf


def test_e27_parse_document_backward_compat(tmp_path):
    """§2.4.2：parse_document 保持单参向后兼容，同时支持 ocr_images=False 与 progress_cb 可选参数。"""
    sample_txt = tmp_path / 'sample.txt'
    sample_txt.write_text('第一行文本\n第二行文本\n', encoding='utf-8', newline='\n')

    d1 = parse_document(str(sample_txt))
    assert '第一行文本' in d1.text
    assert d1.source_type == 'txt'

    progress_events: list[tuple[int, int, int]] = []
    d2 = parse_document(
        str(sample_txt),
        ocr_images=False,
        progress_cb=lambda done, total, blocks: progress_events.append((done, total, blocks)),
    )
    assert '第二行文本' in d2.text
    assert d2.pages == 1


def test_e27_scanned_pdf_incremental_flush_and_resume(tmp_path, monkeypatch):
    """§2.4.1：扫描 PDF 每 N 页（_PDF_PAGE_INCREMENTAL_FLUSH=5）增量写入 .ocr.json 断点缓存，中断后断点续扫不白算。"""
    import json
    import pymupdf
    import core.parser as parser_mod

    pdf_path = tmp_path / 'scanned_bid.pdf'
    doc = pymupdf.open()
    for _ in range(7):
        doc.new_page(width=595, height=842)
    doc.save(str(pdf_path))
    doc.close()

    json_cache = Path(str(pdf_path) + '.ocr.json')
    txt_cache = Path(str(pdf_path) + '.ocr.txt')

    # 第一轮：在第 5 页结束时已触发增量落盘（partial=True 且 pages=5），第 6 页模拟中断崩溃
    phase1_calls: list[int] = []

    def _crash_on_6th(_img_bgr):
        phase1_calls.append(1)
        idx = len(phase1_calls)
        if idx == 6:
            raise RuntimeError('simulated ocr crash on page 6')
        txt = f'扫描投标书第{idx}页响应内容，企业资质与投标总价500000元均完全符合招标文件要求。'
        return [([[0, 0], [100, 0], [100, 20], [0, 20]], txt, 0.98)], None

    monkeypatch.setattr(parser_mod, '_get_ocr_engine', lambda: _crash_on_6th)

    flushed_snapshot_at_5: dict | None = None

    def _cb_phase1(done: int, total: int, cached: int) -> None:
        nonlocal flushed_snapshot_at_5
        if done == 5 and json_cache.is_file():
            flushed_snapshot_at_5 = json.loads(json_cache.read_text(encoding='utf-8'))

    with pytest.raises(RuntimeError, match='simulated ocr crash on page 6'):
        parse_document(str(pdf_path), progress_cb=_cb_phase1)

    assert len(phase1_calls) == 6
    assert flushed_snapshot_at_5 is not None
    assert flushed_snapshot_at_5.get('partial') is True
    assert len(flushed_snapshot_at_5.get('pages') or []) == 5
    assert not txt_cache.exists()
    assert json_cache.is_file()

    # 第二轮：恢复正常 OCR 引擎断点续扫，必须跳过已落盘的前 5 页，只识别第 6、7 两页
    phase2_calls: list[int] = []
    phase2_events: list[tuple[int, int, int]] = []

    def _resume_engine(_img_bgr):
        phase2_calls.append(1)
        idx = 5 + len(phase2_calls)
        txt = f'扫描投标书第{idx}页响应内容，企业资质与投标总价500000元均完全符合招标文件要求。'
        return [([[0, 0], [100, 0], [100, 20], [0, 20]], txt, 0.99)], None

    monkeypatch.setattr(parser_mod, '_get_ocr_engine', lambda: _resume_engine)
    res = parse_document(
        str(pdf_path),
        progress_cb=lambda done, total, cached: phase2_events.append((done, total, cached)),
    )

    assert len(phase2_calls) == 2
    assert phase2_events == [(0, 2, 5), (1, 2, 5), (2, 2, 5)]
    assert res.pages == 7
    assert '扫描投标书第1页' in res.text
    assert '扫描投标书第7页' in res.text
    assert txt_cache.is_file()

    # 全部完成后 .ocr.json 转为完整 list[dict]，get_pdf_ocr_data 直接秒读不重复跑 OCR
    final_data = parser_mod.get_pdf_ocr_data(pdf_path)
    assert isinstance(final_data, list) and len(final_data) == 7
    assert len(phase2_calls) == 2

    # 源文件被不同字节大小的 PDF 原位覆盖时，旧断点缓存必须自动失效重建
    new_doc = pymupdf.open()
    for _ in range(3):
        p = new_doc.new_page(width=400, height=600)
        p.draw_rect(pymupdf.Rect(10, 10, 100, 100), color=(0, 0, 0))
    new_doc.save(str(pdf_path))
    new_doc.close()
    txt_cache.unlink(missing_ok=True)
    # 人造 3 页部分旧缓存并验证 size 变更时清空重建
    rebuilt_calls: list[int] = []

    def _fresh_engine(_img_bgr):
        rebuilt_calls.append(1)
        idx = len(rebuilt_calls)
        txt = f'新版扫描投标书第{idx}页完整内容，满足全部实质性资格要求与开标一览表要求，累计字数达标。'
        return [([[0, 0], [100, 0], [100, 20], [0, 20]], txt, 0.99)], None

    monkeypatch.setattr(parser_mod, '_get_ocr_engine', lambda: _fresh_engine)
    res_new = parse_document(str(pdf_path))
    assert len(rebuilt_calls) == 3
    assert '新版扫描投标书第3页' in res_new.text


def test_e27_cold_ocr_progress_callback_not_muted_by_zero_cached_hits(tmp_path, monkeypatch):
    """§2.4.3：冷启动 cached_hits=0 时 worker 阶段 1.5 不得静音跳过 SSE 进度推送，扫描 PDF 正确显示按页播报。"""
    import json
    import pymupdf
    import core.parser as parser_mod
    from docx import Document
    from backend.database import Base, get_engine, get_session_factory
    from backend.models import BidCheckTask, Tenant
    from backend.worker import BidTaskWorker, _cleanup_task_deviation_artifacts, deviation_stats_sidecar_path

    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')

    tender_docx = tmp_path / 'tender.docx'
    td = Document()
    td.add_paragraph('第一章 招标公告\n最高限价：100万元。\n★1. 质保期不少于1年。')
    td.save(str(tender_docx))

    bid_pdf = tmp_path / 'bid_scan.pdf'
    pd = pymupdf.open()
    for _ in range(6):
        pd.new_page(width=595, height=842)
    pd.save(str(bid_pdf))
    pd.close()

    call_no = [0]

    def _fake_pdf_ocr(_img_bgr):
        call_no[0] += 1
        txt = f'第{call_no[0]}页投标响应内容：★1. 质保期1年，投标总价为900000.00元，资格证明文件齐全有效。'
        return [([[0, 0], [100, 0], [100, 20], [0, 20]], txt, 0.99)], None

    monkeypatch.setattr(parser_mod, '_get_ocr_engine', lambda: _fake_pdf_ocr)

    db_file = tmp_path / 'worker_pdf_progress.db'
    eng = get_engine(f"sqlite:///{db_file.as_posix()}")
    Base.metadata.create_all(bind=eng)
    SessionMaker = get_session_factory(eng)

    s = SessionMaker()
    try:
        tenant = Tenant(name='冷启动进度测试部', is_active=True)
        s.add(tenant)
        s.flush()
        task = BidCheckTask(
            tenant_id=tenant.id,
            task_type='pre_check',
            title='扫描PDF进度验证任务',
            status='created',
            progress=0,
            tender_filename='tender.docx',
            bid_filename='bid_scan.pdf',
            tender_path=str(tender_docx),
            bid_path=str(bid_pdf),
        )
        s.add(task)
        s.commit()
        tid = task.id
    finally:
        s.close()

    emitted_details: list[tuple[str, int, str]] = []
    worker = BidTaskWorker(session_factory=SessionMaker, max_workers=1)
    orig_emit = worker._emit_stage

    def _spy_emit(session, task_obj, status, progress, detail):
        emitted_details.append((status, int(progress), str(detail)))
        return orig_emit(session, task_obj, status, progress, detail)

    monkeypatch.setattr(worker, '_emit_stage', _spy_emit)
    try:
        worker.submit_check_task(tid, sync=True)
        assert any('每 5 页增量落盘缓存' in d and '/ 6 页' in d for _, _, d in emitted_details)
        s2 = SessionMaker()
        try:
            done_row = s2.query(BidCheckTask).filter_by(id=tid).first()
            assert done_row is not None
            assert done_row.status == 'delivered'
            raw_dur = done_row.duration_json
            dur = json.loads(raw_dur) if isinstance(raw_dur, str) else raw_dur
            assert isinstance(dur, dict)
            assert 'f_parse_ms' in dur and dur['f_parse_ms'] >= 0
        finally:
            s2.close()
        assert deviation_stats_sidecar_path(tid).is_file()
    finally:
        _cleanup_task_deviation_artifacts(tid)
        worker.shutdown(wait=False)
        eng.dispose()


def test_e27_deviation_export_reuses_f4_result_zero_llm_with_submission_rule(tmp_path, monkeypatch):
    """§2.6：export_deviation_table 传入 f4_result 时跳过二次 run_response_check（0 次 LLM），且兼容含 submission_rule 的长度差异。"""
    import core.deviation_exporter as dev_mod
    from core.deviation_exporter import export_deviation_table
    from core.response_checker import run_response_check
    from core.tender_extractor import extract_invalid_bid_clauses

    tender_txt = tmp_path / 't.txt'
    bid_txt = tmp_path / 'b.txt'
    out_docx = tmp_path / 'out_dev.docx'

    t_body = (
        "第三章 投标人须知\n"
        "14.1 投标文件份数：正本1份，副本4份，未按要求递交电子版按无效投标处理。\n"
        "第四章 采购需求及技术要求\n"
        "二、采购需求及技术要求\n"
        "● 数据知识库模块\n"
        "支持工业物性数据接入与自动向量化检索。\n"
        "六、质保期要求：硬件1年质保，软件2年质保。\n"
        "第五章 评标办法\n"
    )
    b_body = (
        "响应内容：支持工业物性数据接入与自动向量化检索。\n"
        "承诺：硬件1年质保，软件2年质保。\n"
    )
    tender_txt.write_text(t_body, encoding='utf-8', newline='\n')
    bid_txt.write_text(b_body, encoding='utf-8', newline='\n')

    reqs = extract_invalid_bid_clauses(t_body)
    assert any(r.get('clause_role') == 'submission_rule' for r in reqs)
    f4_res = run_response_check(tender_txt, bid_txt, requirements=reqs, client=None)
    assert len(f4_res['star_results']) == 3  # 14.1 + 1条技术 + 六、质保

    def _forbid_second_run(*args, **kwargs):
        raise AssertionError('传入 f4_result 时严禁再次调用 run_response_check')

    monkeypatch.setattr(dev_mod, 'run_response_check', _forbid_second_run)

    res = export_deviation_table(
        tender_txt,
        bid_txt,
        out_docx,
        requirements=reqs,
        client=None,
        f4_result=f4_res,
    )
    assert out_docx.is_file()
    assert res['total_rows'] == 2  # 已剥除 14.1 submission_rule，仅留 2 条实质要求
    assert all(r['clause_no'] != '14.1' for r in res['rows'])


def test_e27_chapter4_stub_skip_and_truncated_eval_rule():
    """§2.6：第三章 13 字假章节头不得截胡真正第四章；长评标办法下 6、报价过低 必须完整切出并归为 eval_rule。"""
    from core.tender_extractor import classify_invalid_clause_role, extract_invalid_bid_clauses

    filler = '其他评标细则说明内容，此处填充字符以拉长前导段落确保超过六百字限制。' * 14
    sample = (
        "第三章 投标人须知\n"
        "本招标文件共五章：\n"
        "第三章投标人须知\n"
        "第四章采购需求及技术要求\n"
        "第五章评分办法及评分标准\n"
        "4.2 请投标人认真审阅上述全部章节条款。\n"
        "第四章采购需求及技术要求\n"
        "二、采购需求及技术要求\n"
        "（1）采用逻辑隔离技术实现工作环境与普通环境安全隔离。\n"
        "（2）支持不少于10个客户端并发访问及审计追溯。\n"
        "三、交付或者实施的时间和地点\n"
        "交付时间：自合同签订生效后3个月内。\n"
        "六、质保期要求：硬件1年质保，软件2年质保。\n"
        "第五章评分办法及评分标准\n"
        f"三、评标办法\n1、{filler}\n"
        "6、报价过低：\n"
        "评标委员会认为投标人的报价明显低于其他通过符合性审查投标人的报价，"
        "应当要求其在合理时间内提供说明，不能证明其报价合理性的，评标委员会应当将其作为无效投标处理。\n"
    )
    clauses = extract_invalid_bid_clauses(sample)
    tech_comm = [c for c in clauses if c.get('clause_role') == 'technical_or_commercial']
    assert len(tech_comm) == 4
    assert [c['clause_no'] for c in tech_comm] == ['采购需求-（1）', '采购需求-（2）', '三、', '六、']

    low_price = [c for c in clauses if c.get('clause_no') == '6、']
    assert low_price and low_price[0].get('clause_role') == 'eval_rule'
    assert '评标委员会应当将其作为无效投标处理' in low_price[0]['text']
    # 旧缓存中只截到"评标委员会认为投标人"的残留片段也能被识别为 eval_rule 失效清理
    assert classify_invalid_clause_role('6、报价过低：\n评标委员会认为投标人', '6、') == 'eval_rule'



