# -*- coding: utf-8 -*-
"""E43 · 暗标信号识别与暗标防废标双模式全链路验收测试。

覆盖 E43 全部功能点与架构侧复核修订约束：
1. F1 0-Token 暗标要求提取（正文 + 否定句过滤 + 逗号混合句 + 禁全图 vs 仅禁Logo区分 + quote_verified）；
2. scan_blind_bid_compliance 确定性合规审计（身份词/自定义屏蔽词/手机邮箱 + .docx 底纹/粗斜体/TOC/页眉页脚 + 图片 NEED_MANUAL 降级 vs 全图禁令一票否决）；
3. Worker 体检流水线（`source_module='BLIND_BID'`，不污染 `'F3'` 语义，error -> veto / NEED_MANUAL -> warning）；
4. compose_bid_blueprint + export_composed_bid_docx 暗标双模式：
   - 暗标模式导出 `.docx`：零 `w:shd`、零粗斜体/下划线/高亮/非黑字体、禁目录时不写 `TOC \\o`、清空页眉页脚、零 `word/media/`、精确清洗机构线索与自定义词、保守保留非明确编号/业绩并进人工清单；并能通过自身 `scan_blind_bid_compliance` 零违规自检；
   - 通用模式（`blind_bid_mode=False`）：保留独立封面、目录 `TOC \\o`、表格底纹与 cert 附页图片，零回归；
5. API `/api/extract` + `/api/composer/generate`（传 `blind_bid_mode` + `blind_rules_json` + `blind_custom_words_json`，审计日志只记屏蔽词个数不记明文）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
import zipfile

from docx import Document
from docx.shared import Inches, RGBColor
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.database import Base, get_engine, get_session_factory
from backend.main import create_app
from backend.models import AuditLog, BidCheckTask, BidFinding, Tenant
from backend.worker import BidTaskWorker
from core.bid_composer import compose_bid_blueprint, export_composed_bid_docx
from core.parser import parse_document
from core.qualification_scanner import scan_blind_bid_compliance
from core.quote_verifier import verify_quote
from core.tender_extractor import build_review_checklist, extract_blind_bid_requirements

_W_NS = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def _login_headers(client: TestClient, username: str = 'admin', password: str = 'BidGuard2026!') -> dict[str, str]:
    resp = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert resp.status_code == 200, resp.text
    return {'Authorization': f"Bearer {resp.json()['token']}"}


def _make_png(path: Path) -> Path:
    img = Image.new('RGB', (48, 48), color=(30, 80, 160))
    img.save(str(path), format='PNG')
    return path


def _build_blind_tender_docx(path: Path, *, strict_all_images: bool = False) -> Path:
    doc = Document()
    doc.add_paragraph('项目名称：某市算力调度与暗标采购测试项目')
    doc.add_paragraph('项目编号：ZB-2026-E43')
    doc.add_paragraph('采购人名称：某市政务服务和数据管理局')
    doc.add_paragraph('条款 15：本项目技术标采用暗标评审。')
    if strict_all_images:
        doc.add_paragraph('15.1 技术标暗标部分不得出现投标人名称、人员真实姓名、联系方式，严禁插入任何图片、图像或照片。')
    else:
        doc.add_paragraph('15.1 技术标暗标部分不得出现投标人名称、公司Logo、商标水印、人员真实姓名或联系方式。')
    doc.add_paragraph('15.2 禁止设置目录，不得使用底纹、阴影、加粗、斜体、下划线、边框等特殊样式标记。')
    doc.add_paragraph('★1. 须提供 7×24 小时本地化技术支持与故障响应承诺。')
    doc.add_paragraph('评分细则（技术部分 40 分）：')
    doc.add_paragraph('1. 整体技术架构设计（20 分）：架构高可用、模块化及自动化运维能力。')
    doc.add_paragraph('2. 实施方案与售后 SLA（20 分）：明确交付里程碑与应急处置流程。')
    doc.save(str(path))
    return path


def _inspect_ooxml_blind_violations(docx_path: Path) -> dict[str, object]:
    with zipfile.ZipFile(docx_path, 'r') as zf:
        names = set(zf.namelist())
        media = [nm for nm in names if nm.startswith('word/media/') and not nm.endswith('/')]
        doc_root = ET.fromstring(zf.read('word/document.xml'))
        styles_root = ET.fromstring(zf.read('word/styles.xml')) if 'word/styles.xml' in names else None

        def _count_tags(root: ET.Element | None) -> dict[str, int]:
            if root is None:
                return {'shd': 0, 'bold': 0, 'italic': 0, 'underline': 0, 'highlight': 0, 'non_black': 0}
            shd = len(list(root.iter(f'{_W_NS}shd')))
            bold = len(list(root.iter(f'{_W_NS}b'))) + len(list(root.iter(f'{_W_NS}bCs')))
            italic = len(list(root.iter(f'{_W_NS}i'))) + len(list(root.iter(f'{_W_NS}iCs')))
            underline = len(list(root.iter(f'{_W_NS}u')))
            highlight = len(list(root.iter(f'{_W_NS}highlight')))
            non_black = 0
            for col in root.iter(f'{_W_NS}color'):
                val = str(col.attrib.get(f'{_W_NS}val') or '').strip().lower()
                if val and val not in ('000000', 'auto'):
                    non_black += 1
            return {
                'shd': shd,
                'bold': bold,
                'italic': italic,
                'underline': underline,
                'highlight': highlight,
                'non_black': non_black,
            }

        toc_fields = [
            instr.text
            for instr in doc_root.iter(f'{_W_NS}instrText')
            if instr.text and 'TOC' in instr.text.upper()
        ]
        hf_texts = []
        for nm in sorted(names):
            if nm.lower().startswith(('word/header', 'word/footer')) and nm.lower().endswith('.xml'):
                hf_root = ET.fromstring(zf.read(nm))
                txt = ''.join((t.text or '') for t in hf_root.iter(f'{_W_NS}t')).strip()
                if txt:
                    hf_texts.append(txt)

        return {
            'media': media,
            'toc_fields': toc_fields,
            'hf_texts': hf_texts,
            'doc_tags': _count_tags(doc_root),
            'style_tags': _count_tags(styles_root),
        }


# ── 1. F1 0-Token 暗标识别、否定过滤与 Quote 溯源 ─────────────────────────────

def test_e43_blind_bid_signal_extraction_positive_negative_and_quote_verified(tmp_path: Path):
    pos_text = (
        "第三章 投标人须知\n"
        "条款 15：本项目技术标采用暗标评审。\n"
        "15.1 技术标暗标部分不得出现投标人名称、Logo、人员真实姓名、电话或电子邮箱。\n"
        "15.2 暗标部分禁止设置目录。\n"
        "15.3 技术标暗标部分不得使用底纹、阴影、加粗、斜体、下划线、边框等特殊样式标记。\n"
    )
    info = extract_blind_bid_requirements(pos_text)
    assert info['is_blind_bid'] is True
    assert info['toc_forbidden'] is True
    assert info['style_forbidden'] is True
    assert info['no_images_strict'] is False
    assert info['image_ban_scope'] == 'logo_only'
    assert len(info['requirements']) >= 3
    for req in info['requirements']:
        assert req['quote_verified'] is True
        assert verify_quote(req['quote'], pos_text)
    assert {'hide_bidder_name', 'hide_logo_media', 'hide_person_name', 'hide_contact_info'} <= set(info['recommended_rules'])

    # 严格全图禁令区分
    strict_img_text = "本项目技术暗标采用盲审，严禁在技术标正文中插入任何图片、图像或照片。"
    strict_info = extract_blind_bid_requirements(strict_img_text)
    assert strict_info['is_blind_bid'] is True
    assert strict_info['no_images_strict'] is True
    assert strict_info['image_ban_scope'] == 'all_images'

    # 否定句过滤（不得误报）
    for neg_text in (
        "本项目为非暗标项目，采用公开综合评分法。",
        "本项目不采用暗标评审，投标人正常装订封面与目录即可。",
        "前附表：是否采用暗标：否。",
        "前附表\n是否采用暗标评审\n否\n开标地点：第一开标室",
    ):
        neg_info = extract_blind_bid_requirements(neg_text)
        assert neg_info['is_blind_bid'] is False, f"否定句误触发：{neg_text}"
        assert neg_info['requirements'] == []
        assert neg_info['toc_forbidden'] is False
        assert neg_info['style_forbidden'] is False

    # 逗号混合句（前半非暗标、后半暗标）
    mixed = "商务标为非暗标部分，技术标采用暗标，不得使用底纹、加粗。"
    mixed_info = extract_blind_bid_requirements(mixed)
    assert mixed_info['is_blind_bid'] is True
    assert mixed_info['style_forbidden'] is True

    # build_review_checklist 挂载 blind_bid_info
    t_docx = _build_blind_tender_docx(tmp_path / 'tender_checklist.docx')
    checklist = build_review_checklist(str(t_docx))
    assert checklist['blind_bid_info']['is_blind_bid'] is True
    assert checklist['blind_bid_info']['toc_forbidden'] is True
    assert checklist['blind_bid_info']['style_forbidden'] is True


# ── 2. scan_blind_bid_compliance + 图片 NEED_MANUAL 诚实降级 vs 全禁一票否决 ────

def test_e43_scan_blind_bid_compliance_identity_ooxml_and_honest_image_fallback(tmp_path: Path):
    png_path = _make_png(tmp_path / 'logo.png')
    dirty_docx = tmp_path / 'dirty_bid.docx'
    d = Document()
    d.sections[0].header.paragraphs[0].add_run('并行算力科技有限公司 · 机密标书')
    d.add_heading('一、总体设计方案', level=1)
    p_toc = d.add_paragraph()
    from core.bid_composer import _insert_toc_field, _shade_cell
    _insert_toc_field(p_toc)
    p_leak = d.add_paragraph()
    r_leak = p_leak.add_run('我方并行算力科技有限公司（联系人电话：13800138000，邮箱 bid@anyitech.cn，含超级保密品牌）承诺全满足。')
    r_leak.bold = True
    r_leak.italic = True
    r_leak.font.color.rgb = RGBColor(0x1D, 0x4E, 0xD8)
    tbl = d.add_table(rows=1, cols=1)
    _shade_cell(tbl.rows[0].cells[0], 'EFF6FF')
    tbl.rows[0].cells[0].text = '表头底纹测试'
    d.add_picture(str(png_path), width=Inches(1.0))
    d.save(str(dirty_docx))

    # 2a) 仅禁 Logo -> 图片降级为 NEED_MANUAL（warning + consequence='none'），其余身份/样式/TOC 判 veto
    info_logo_only = extract_blind_bid_requirements(
        "技术标采用暗标评审，不得出现投标人名称、Logo、联系方式；禁止设置目录；不得使用底纹、加粗、斜体。"
    )
    findings_a = scan_blind_bid_compliance(
        dirty_docx,
        info_logo_only,
        bidder_hints=['并行算力科技有限公司'],
        custom_words=['超级保密品牌'],
    )
    codes_a = {f['code'] for f in findings_a}
    assert 'BLIND_BID_IDENTITY_LEAK' in codes_a
    assert 'BLIND_BID_STYLE_VIOLATION' in codes_a
    assert 'BLIND_BID_TOC_VIOLATION' in codes_a
    assert 'BLIND_BID_NEED_MANUAL' in codes_a
    for f in findings_a:
        assert f['source_module'] == 'BLIND_BID'
    manual_f = next(f for f in findings_a if f['code'] == 'BLIND_BID_NEED_MANUAL')
    assert manual_f['severity'] == 'warning'
    assert manual_f['consequence'] == 'none'
    assert manual_f['detail_json'].get('needs_manual') is True

    # 2b) 招标明令“不得出现任何图片” -> word/media 非空确定性判 BLIND_BID_IDENTITY_LEAK（error/veto），不再降级
    info_no_img = extract_blind_bid_requirements(
        "技术标采用暗标评审，严禁插入任何图片或图像。"
    )
    findings_b = scan_blind_bid_compliance(dirty_docx, info_no_img)
    media_leaks = [
        f for f in findings_b
        if f['code'] == 'BLIND_BID_IDENTITY_LEAK' and (f.get('detail_json') or {}).get('check_type') == 'media_strict_ban'
    ]
    assert len(media_leaks) == 1
    assert media_leaks[0]['severity'] == 'error'
    assert media_leaks[0]['consequence'] == 'veto'
    assert not any(f['code'] == 'BLIND_BID_NEED_MANUAL' for f in findings_b)

    # 2c) 非暗标招标不触发扫描
    assert scan_blind_bid_compliance(dirty_docx, {'is_blind_bid': False}) == []


# ── 3. 双模式导出 OOXML 零痕迹验尸 + 通用模式零回归 ─────────────────────────────

def test_e43_dual_mode_docx_ooxml_plain_vs_normal_mode_zero_regression(tmp_path: Path):
    tender_path = _build_blind_tender_docx(tmp_path / 'blind_tender.docx')
    cert_img = _make_png(tmp_path / 'iso_scan.png')
    knowledge_doc = tmp_path / 'ref_knowledge.docx'
    kd = Document()
    kd.add_paragraph(
        '高可用模块化架构参考：由并行算力科技有限公司研发的秘密商标词平台于2024某省政务云项目(合同HT-998)稳定落地，'
        '自动化运维值班手机 13911112222，联系邮箱 ops@demo.cn，支持多活容灾切换与7×24保障。'
    )
    kd.save(str(knowledge_doc))

    class FakeCert:
        id = 7
        cert_name = 'ISO27001信息安全管理体系认证'
        cert_no = 'CERT-2026-7788'
        valid_until = '2028-12-31'
        is_long_term = False
        warn_days = 30
        attachment_name = 'iso_scan.png'
        attachment_path = str(cert_img)
        has_attachment = True

    # 3a) 暗标模式生成与导出
    bp_blind = compose_bid_blueprint(
        tender_path,
        ledger_certs=[FakeCert()],
        compose_mode='knowledge_draft_v2',
        knowledge_paths=[knowledge_doc],
        use_ai=False,
        blind_bid_mode=True,
        blind_custom_words=['秘密商标词'],
        bidder_hints=['并行算力科技有限公司'],
    )
    assert bp_blind.blind_bid_mode is True
    assert bp_blind.stats['blind_bid_applied'] is True
    assert bp_blind.stats['blind_toc_forbidden'] is True

    blind_out = export_composed_bid_docx(bp_blind, tmp_path / 'out_blind.docx')
    xml_blind = _inspect_ooxml_blind_violations(blind_out)
    assert xml_blind['media'] == []
    assert xml_blind['toc_fields'] == []
    assert xml_blind['hf_texts'] == []
    for key in ('shd', 'bold', 'italic', 'underline', 'highlight', 'non_black'):
        assert xml_blind['doc_tags'][key] == 0, f"暗标正文 document.xml 发现违规样式 {key}"
        assert xml_blind['style_tags'][key] == 0, f"暗标样式表 styles.xml 发现违规样式 {key}"

    exported_text = parse_document(str(blind_out), ocr_images=False).text
    for leaked in ('秘密商标词', '并行算力科技有限公司', '13911112222', 'ops@demo.cn'):
        assert leaked not in exported_text, f"暗标导出文档残留屏蔽词/线索：{leaked}"

    # 保守清理边界核验：不强改非明确外部项目/历史业绩字样，且人工清单保留提示
    manual_fields = ' '.join(m.get('field', '') for m in bp_blind.manual_checklist)
    assert '非本项目编号人工复核' in manual_fields
    assert '历史业绩案例匿名复核' in manual_fields
    assert '2024某省政务云项目(合同HT-998)' in exported_text

    # 闭环自检：拿同一份招标暗标规则审自己导出的文档，必须 0 违规
    tender_info = build_review_checklist(str(tender_path))['blind_bid_info']
    self_audit = scan_blind_bid_compliance(
        blind_out,
        tender_info,
        bidder_hints=['并行算力科技有限公司'],
        custom_words=['秘密商标词'],
    )
    assert self_audit == [], f"暗标模式生成产物未能通过自身暗标审计：{self_audit}"

    # 3b) 通用模式（blind_bid_mode=False）零回归：有页眉页脚、有底纹、有 TOC 域、保留普通版式
    bp_norm = compose_bid_blueprint(
        tender_path,
        compose_mode='skeleton_v1',
        use_ai=False,
        blind_bid_mode=False,
    )
    assert bp_norm.blind_bid_mode is False
    norm_out = export_composed_bid_docx(bp_norm, tmp_path / 'out_normal.docx')
    xml_norm = _inspect_ooxml_blind_violations(norm_out)
    assert xml_norm['doc_tags']['shd'] > 0
    assert xml_norm['doc_tags']['bold'] > 0
    assert len(xml_norm['toc_fields']) >= 1
    assert len(xml_norm['hf_texts']) >= 1

    # 3c) Mock AI 暗标模式：验证“暗标编写铁律”写入提示词 + AI 产物经确定性脱敏清理
    seen_prompts: list[str] = []

    class _MockBlindLLM:
        def chat(self, messages, model_tier='flash', **_kwargs):
            txt = str(messages[-1].get('content') or '')
            seen_prompts.append(txt)
            from types import SimpleNamespace
            if '"chapters"' in txt:
                return SimpleNamespace(content=json.dumps({
                    'chapters': [{
                        'chapter_no': '第一章',
                        'title': '整体技术架构设计',
                        'chapter_kind': 'technical',
                        'focus_topic_ids': ['topic_1'],
                        'writing_brief': '整体架构',
                        'recommended_table': '',
                    }]
                }, ensure_ascii=False))
            if '"responses"' in txt:
                return SimpleNamespace(content=json.dumps({
                    'responses': [{'seq': 1, 'response': '并行算力科技有限公司完全响应，联系电话 13500135000。'}]
                }, ensure_ascii=False))
            return SimpleNamespace(content=json.dumps({
                'paragraphs': ['由并行算力科技有限公司提供秘密商标词引擎支持，值班邮箱 ai@demo.cn。'],
                'table': None,
                'needs_manual': [],
            }, ensure_ascii=False))

    bp_ai_blind = compose_bid_blueprint(
        tender_path,
        client=_MockBlindLLM(),
        use_ai=True,
        blind_bid_mode=True,
        blind_custom_words=['秘密商标词'],
        bidder_hints=['并行算力科技有限公司'],
    )
    assert any('暗标' in p and '禁止出现投标人全称' in p for p in seen_prompts)
    ai_docx = export_composed_bid_docx(bp_ai_blind, tmp_path / 'out_ai_blind.docx')
    ai_txt = parse_document(str(ai_docx), ocr_images=False).text
    for leaked in ('秘密商标词', '并行算力科技有限公司', '13500135000', 'ai@demo.cn'):
        assert leaked not in ai_txt
    assert scan_blind_bid_compliance(
        ai_docx,
        tender_info,
        bidder_hints=['并行算力科技有限公司'],
        custom_words=['秘密商标词'],
    ) == []


# ── 4. Worker 体检流水线注入 source_module='BLIND_BID' ─────────────────────────

def test_e43_worker_pipeline_emits_blind_bid_source_module(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'worker_e43.db'
    eng = get_engine(f"sqlite:///{db_file.as_posix()}")
    Base.metadata.create_all(bind=eng)
    SessionMaker = get_session_factory(eng)

    t_path = _build_blind_tender_docx(tmp_path / 't_worker.docx')
    b_path = tmp_path / 'b_worker.docx'
    bd = Document()
    bd.add_heading('投标文件正文', level=1)
    bd.add_paragraph('★1. 我方完全满足 7×24 小时本地化技术支持。')
    r = bd.add_paragraph().add_run('投标人：某星辰算力股份有限公司，联系电话：13600136000。')
    r.bold = True
    bd.save(str(b_path))

    sess = SessionMaker()
    try:
        tenant = Tenant(name='某星辰算力股份有限公司', is_active=True)
        sess.add(tenant)
        sess.flush()
        task = BidCheckTask(
            tenant_id=tenant.id,
            task_type='pre_check',
            title='E43暗标体检流水线测试',
            status='created',
            progress=0,
            tender_filename='t_worker.docx',
            bid_filename='b_worker.docx',
            tender_path=str(t_path),
            bid_path=str(b_path),
            config_json={'blind_custom_words': ['星辰算力']},
        )
        sess.add(task)
        sess.commit()
        tid = task.id
    finally:
        sess.close()

    worker = BidTaskWorker(session_factory=SessionMaker, max_workers=2)
    worker.submit_check_task(tid, sync=True)

    check_sess = SessionMaker()
    try:
        refreshed = check_sess.query(BidCheckTask).filter_by(id=tid).first()
        assert refreshed is not None and refreshed.status == 'delivered'
        assert (refreshed.stats_json or {}).get('is_blind_bid') is True
        assert int((refreshed.stats_json or {}).get('blind_bid_finding_count') or 0) >= 2

        rows = check_sess.query(BidFinding).filter_by(task_id=tid).all()
        blind_rows = [f for f in rows if f.source_module == 'BLIND_BID']
        assert len(blind_rows) >= 2
        assert all(f.code.startswith('BLIND_BID_') for f in blind_rows)
        assert any(f.consequence == 'veto' and f.severity == 'error' for f in blind_rows)
        # 确保不污染 F3 语义模块
        f3_blind = [f for f in rows if f.source_module == 'F3' and 'BLIND' in f.code]
        assert f3_blind == []
    finally:
        check_sess.close()


# ── 5. API：/api/extract 与 /api/composer/generate（含审计日志脱敏约束）──────────

def test_e43_api_extract_and_composer_generate_blind_mode_audit_redacted(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'api_e43.db'
    db_url = f"sqlite:///{db_file.as_posix()}"
    import backend.database as db_mod

    tender_path = _build_blind_tender_docx(tmp_path / 'api_blind_tender.docx')
    k_doc = tmp_path / 'api_k.docx'
    kd = Document()
    kd.add_paragraph('本项目引入绝密代号Alpha架构引擎，联系手机 13700137000。')
    kd.save(str(k_doc))

    app = create_app(database_url=db_url)
    created_ids: list[int] = []
    with TestClient(app) as tc:
        headers = _login_headers(tc)

        # 5a) F1 /api/extract 返回 blind_bid_info
        r_ext = tc.post(
            '/api/extract',
            headers=headers,
            files={'file': ('api_blind_tender.docx', tender_path.read_bytes(), 'application/octet-stream')},
        )
        assert r_ext.status_code == 200, r_ext.text
        ext_body = r_ext.json()
        assert ext_body['blind_bid_info']['is_blind_bid'] is True
        assert ext_body['blind_bid_info']['toc_forbidden'] is True
        assert ext_body['blind_bid_info']['style_forbidden'] is True
        assert len(ext_body['blind_bid_info']['requirements']) >= 2

        try:
            # 5b) /api/composer/generate 暗标模式
            r_gen = tc.post(
                '/api/composer/generate',
                headers=headers,
                files=[
                    ('tender_file', ('api_blind_tender.docx', tender_path.read_bytes(), 'application/octet-stream')),
                    ('knowledge_files', ('api_k.docx', k_doc.read_bytes(), 'application/octet-stream')),
                ],
                data={
                    'compose_mode': 'knowledge_draft_v2',
                    'use_ai': 'false',
                    'blind_bid_mode': 'true',
                    'blind_rules_json': json.dumps([
                        'hide_bidder_name',
                        'hide_logo_media',
                        'hide_person_name',
                        'hide_contact_info',
                        'hide_external_project',
                        'hide_past_cases',
                    ]),
                    'blind_custom_words_json': json.dumps(['绝密代号Alpha']),
                    'async_run': 'false',
                },
            )
            assert r_gen.status_code == 200, r_gen.text
            gen_body = r_gen.json()
            created_ids.append(gen_body['id'])
            assert gen_body['blind_bid_mode'] is True
            assert gen_body['blueprint']['blind_bid_mode'] is True
            assert gen_body['stats']['blind_bid_applied'] is True
            assert gen_body['stats']['blind_custom_word_count'] == 1

            # 下载导出的暗标 .docx 检验 XML 洁净与屏蔽词擦除
            r_dl = tc.get(gen_body['download_url'], headers=headers)
            assert r_dl.status_code == 200
            dl_docx = tmp_path / 'downloaded_blind.docx'
            dl_docx.write_bytes(r_dl.content)
            xml_info = _inspect_ooxml_blind_violations(dl_docx)
            assert xml_info['toc_fields'] == []
            assert xml_info['media'] == []
            assert xml_info['hf_texts'] == []
            assert xml_info['doc_tags']['shd'] == 0
            assert xml_info['doc_tags']['bold'] == 0
            dl_txt = parse_document(str(dl_docx), ocr_images=False).text
            assert '绝密代号Alpha' not in dl_txt
            assert '13700137000' not in dl_txt

            # 审计日志只记开关与屏蔽词数量，严禁将自定义屏蔽词明文写入日志
            sess = db_mod.SessionLocal()
            try:
                audit_row = (
                    sess.query(AuditLog)
                    .filter_by(action='composer_generate', target=f"composer:{gen_body['id']}")
                    .order_by(AuditLog.id.desc())
                    .first()
                )
                assert audit_row is not None
                detail = audit_row.detail_json or {}
                assert detail.get('blind_bid_mode') is True
                assert detail.get('blind_custom_word_count') == 1
                assert '绝密代号Alpha' not in json.dumps(detail, ensure_ascii=False)
            finally:
                sess.close()
        finally:
            for did in created_ids:
                tc.delete(f"/api/composer/drafts/{did}", headers=headers)
