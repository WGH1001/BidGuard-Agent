# -*- coding: utf-8 -*-
"""E37 · 标书编制输出与无损修订交付全链路验收测试。

覆盖 E37 §4 全部验收标准：
1. V1 正向骨架生成（0 Token 零幻觉）：包含资格证照核对（含 E35 长期有效）、缺失标红、偏离表骨架、
   评分要点大纲，且报价栏严格使用 `[待人工填报金额...]` 占位符而非虚构数字；
2. V2 方案库插槽衔接验证：传参考方案 `.docx` + `compose_mode='knowledge_draft_v2'` 准确吸附相关参考片段
   （带来源标示且绝不混入无关段落）；未传参考文档时优雅回退至 `outline_stub`；
3. 原位无损修订导出 Clean 定稿：保留原 `.docx` 页眉/页脚与 Run 加粗/颜色样式，精确替换已采纳串名规则，
   彻底消除全文及 ZIP 内一切 `w:comment*` / `comments*.xml` 批注痕迹；大段漏应答诚实收入 `manual_pending_items`；
4. PDF 原件安全拦截：对 `.pdf` 任务调 `POST /api/reports/{id}/clean-bid` 返回 `422 PDF_CLEAN_EXPORT_UNSUPPORTED`；
5. `/api/composer` CRUD + 租户隔离 + 删除级联清理磁盘（`data/uploads/composer/` + `output/composer/`），且不污染大盘统计；
6. 关联 `tests/test_backend_models.py` 13 表全过。
"""
from datetime import date
from io import BytesIO
import os
from pathlib import Path
import sys
import zipfile

from docx import Document
from docx.shared import Pt, RGBColor
from fastapi.testclient import TestClient
import pymupdf
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.main import create_app
from core.bid_composer import compose_bid_blueprint, export_composed_bid_docx
from core.redline import export_clean_revised_docx, inject_word_comments

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _login_headers(client: TestClient, username: str, password: str = 'BidGuard2026!') -> dict[str, str]:
    resp = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert resp.status_code == 200, resp.text
    return {'Authorization': f"Bearer {resp.json()['token']}"}


def _build_sample_tender_docx(path: Path) -> Path:
    doc = Document()
    doc.add_heading('智慧城市应急指挥协同平台建设项目 招标文件', level=1)
    doc.add_paragraph('采购项目名称：智慧城市应急指挥协同平台建设项目')
    doc.add_paragraph('项目编号：ZC-2026-088')
    doc.add_paragraph('采购人名称：某市应急管理局')
    doc.add_paragraph('开标时间：2026年12月20日 09时30分')
    doc.add_paragraph('本项目最高限价为 2,400,000.00 元，超过最高限价作无效投标处理。')
    doc.add_paragraph(
        '第三章 资格审查要求：投标人须提供有效的企业法人营业执照、'
        'ISO27001信息安全管理体系认证证书以及涉密信息系统集成资质证书。'
    )
    doc.add_paragraph('第四章 实质性条款要求：')
    doc.add_paragraph('★1. 须承诺提供 7×24 小时本地化应急技术响应，故障发生后 2 小时内到达现场。')
    doc.add_paragraph('这是关于第一条响应格式的常规提示，不影响技术实质内容。')
    doc.add_paragraph('▲2. 核心数据库集群须全面适配达梦数据库 DM8 与银河麒麟 V10 操作系统。')

    doc.add_paragraph('第五章 评标办法与评分细则')
    tbl = doc.add_table(rows=4, cols=3)
    tbl.style = 'Table Grid'
    rows_data = [
        ['评分因素', '评分标准', '分值'],
        ['数据库集群高可用架构设计', '要求采用双活热备集群与国产数据库自动故障切换机制。', '25'],
        ['应急指挥多部门协同方案', '详细阐述跨部门联动调度流程与可视化大屏呈现能力。', '20'],
        ['本地化售后服务与培训计划', '提供完整培训方案、备品备件储备及应急演练规划。', '15'],
    ]
    for r_i, row_vals in enumerate(rows_data):
        for c_i, val in enumerate(row_vals):
            tbl.rows[r_i].cells[c_i].text = val

    doc.save(str(path))
    return path


def _build_pdf_file(path: Path, text: str) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((40, 60), text, fontsize=11)
    doc.save(str(path))
    doc.close()
    return path


def _collect_docx_all_text(docx_path: Path) -> str:
    doc = Document(str(docx_path))
    parts = [p.text for p in doc.paragraphs]
    for sec in doc.sections:
        parts.extend(p.text for p in sec.header.paragraphs)
        parts.extend(p.text for p in sec.footer.paragraphs)
    for t in doc.tables:
        for r in t.rows:
            for c in r.cells:
                parts.append(c.text)
    return '\n'.join(parts)


# ── 1. V1 正向骨架生成（0 Token 零幻觉 + 长期有效证照 + 报价锁）─────────────

def test_v1_compose_blueprint_and_docx_price_lock_and_long_term_cert(tmp_path: Path):
    tender_p = _build_sample_tender_docx(tmp_path / 'tender_v1.docx')
    ledger_certs = [
        {
            'id': 1,
            'cert_name': '企业法人营业执照',
            'cert_no': '91320000123456789X',
            'is_long_term': True,
            'valid_until': date(9999, 12, 31),
            'has_attachment': True,
            'attachment_name': '营业执照正本扫描件.pdf',
        },
        {
            'id': 2,
            'cert_name': 'ISO27001信息安全管理体系认证证书',
            'cert_no': 'ISO-2026-8899',
            'is_long_term': False,
            'valid_until': date(2028, 6, 30),
            'has_attachment': False,
        },
    ]

    bp = compose_bid_blueprint(
        tender_p,
        ledger_certs=ledger_certs,
        compose_mode='skeleton_v1',
    )
    assert '智慧城市应急指挥协同平台建设项目' in bp.project_name
    assert bp.project_code == 'ZC-2026-088'
    assert bp.open_date == '2026-12-20'
    assert bp.price_cap == '2,400,000.00'
    assert bp.compose_mode == 'skeleton_v1'
    assert len(bp.sections) == 6

    sec_by_id = {s.section_id: s for s in bp.sections}

    # 资格证照匹配：营业执照长期有效，ISO 有效，涉密甲级缺失标红
    qual_rows = [b for b in sec_by_id['sec_qual'].content_blocks if b.get('kind') == 'cert_row']
    assert len(qual_rows) >= 2
    biz_row = next(r for r in qual_rows if '营业执照' in r['cert_name'])
    assert biz_row['ledger_status'] == 'valid'
    assert biz_row['is_long_term'] is True
    assert biz_row['valid_until'] == '长期有效'
    assert '9999-12-31' not in str(biz_row)
    assert '营业执照正本扫描件.pdf' in biz_row['display_status']
    missing_rows = [r for r in qual_rows if r['ledger_status'] == 'not_in_ledger']
    assert any('涉密' in r['cert_name'] for r in missing_rows)

    # 报价栏强制人工填报占位，严禁出现虚构投标报价数字
    price_rows = {b['label']: b['value'] for b in sec_by_id['sec_price'].content_blocks if 'label' in b}
    total_val = price_rows['投标总报价（人民币小写）']
    assert total_val.startswith('[待人工填报金额')
    assert '2,400,000.00 元' in total_val

    # 实质条款与偏离表骨架包含 ★/▲ 条款
    dev_rows = [b for b in sec_by_id['sec_deviation'].content_blocks if b.get('kind') == 'deviation_row']
    dev_all_text = ' '.join(r['tender_requirement'] for r in dev_rows)
    assert '到达现场' in dev_all_text
    assert '达梦数据库' in dev_all_text

    # 评分大纲提取到 3 个评标主题且 fill_mode='outline_stub'
    sec_scoring = sec_by_id['sec_scoring']
    assert sec_scoring.fill_mode == 'outline_stub'
    topics_text = ' '.join(b['title'] for b in sec_scoring.content_blocks if b.get('kind') == 'scoring_topic')
    assert '数据库集群高可用架构设计' in topics_text

    # 导出 Word 初稿可打开且正文齐备
    out_docx = export_composed_bid_docx(bp, tmp_path / 'v1_draft.docx')
    assert out_docx.is_file()
    doc_text = _collect_docx_all_text(out_docx)
    assert '[待人工填报金额' in doc_text
    assert '长期有效' in doc_text
    assert '台账缺失' in doc_text
    assert '智慧城市应急指挥协同平台建设项目' in doc_text


# ── 2. V2 方案库插槽衔接与回退测试 ──────────────────────────────────────────

def test_v2_compose_knowledge_slot_adsorbs_relevant_snippets_and_falls_back(tmp_path: Path):
    tender_p = _build_sample_tender_docx(tmp_path / 'tender_v2.docx')

    ref_doc = Document()
    ref_doc.add_heading('历史优秀政务项目技术方案摘要', level=1)
    ref_doc.add_paragraph(
        '我司针对数据库集群高可用架构与双活热备设计，采用达梦数据库DM8主备集群自动故障切换方案，'
        '实现RPO=0与秒级容灾接管，保障政务平台连续运行。'
    )
    ref_doc.add_paragraph(
        '园区食堂每日食品安全快检及餐厨垃圾分类清运台账管理办法如下：每日早晚两次对灶台进行消杀巡检。'
    )
    ref_p = tmp_path / 'history_knowledge.docx'
    ref_doc.save(str(ref_p))

    bp_v2 = compose_bid_blueprint(
        tender_p,
        compose_mode='knowledge_draft_v2',
        knowledge_paths=[ref_p],
    )
    sec_scoring = next(s for s in bp_v2.sections if s.section_id == 'sec_scoring')
    assert sec_scoring.fill_mode == 'rag_knowledge'

    all_snips: list[str] = []
    all_notices: list[str] = []
    for block in sec_scoring.content_blocks:
        for sn in block.get('reference_snippets') or []:
            all_snips.append(sn['snippet'])
            all_notices.append(sn['notice'])
            assert sn['quote_verified'] is True

    joined_snips = '\n'.join(all_snips)
    assert '达梦数据库DM8主备集群自动故障切换方案' in joined_snips
    assert '食堂' not in joined_snips
    assert '餐厨垃圾' not in joined_snips
    assert any('history_knowledge.docx' in n for n in all_notices)

    # 未传参考文档时优雅回退为 outline_stub，不报错
    bp_fallback = compose_bid_blueprint(
        tender_p,
        compose_mode='knowledge_draft_v2',
        knowledge_paths=[],
    )
    sec_fb = next(s for s in bp_fallback.sections if s.section_id == 'sec_scoring')
    assert sec_fb.fill_mode == 'outline_stub'


# ── 3. 原位无损修订导出 Clean 定稿（含 Run 样式与全量剥离 comments）──────────

def test_export_clean_revised_docx_preserves_layout_and_strips_comments(tmp_path: Path):
    src_docx = tmp_path / 'bid_with_styles.docx'
    doc = Document()
    hdr = doc.sections[0].header.paragraphs[0]
    hdr.text = '投标正本 - 旧医疗大数据平台项目'
    ftr = doc.sections[0].footer.paragraphs[0]
    ftr.text = '第 1 页 · 保密文件'

    p = doc.add_paragraph()
    p.add_run('我方在前期成功实施过')
    styled_run = p.add_run('旧医疗大数据平台项目')
    styled_run.bold = True
    styled_run.font.size = Pt(14)
    styled_run.font.color.rgb = RGBColor(0x0F, 0x17, 0x2A)
    p.add_run('，本次将全力保障项目顺利验收。')

    doc.add_paragraph('★3. 须提供近三年经审计财务报告。')

    tbl = doc.add_table(rows=1, cols=2)
    tbl.rows[0].cells[0].text = '拟服务项目'
    tbl.rows[0].cells[1].text = '旧医疗大数据平台项目二期延续'
    doc.save(str(src_docx))

    # 先按 E5 逻辑写入 Word 右侧修订批注气泡，确保源文件确含 comments.xml 与 commentRangeStart/End
    class _FakeFinding:
        def __init__(self, severity, rule_name, message, suggestion, legal_basis, clause_no, quote):
            self.severity = severity
            self.rule_name = rule_name
            self.message = message
            self.suggestion = suggestion
            self.legal_basis = legal_basis
            self.clause_no = clause_no
            self.quote = quote

    commented_docx = tmp_path / 'bid_commented.docx'
    inject_word_comments(
        str(src_docx),
        [
            _FakeFinding('high', 'CROSS_PROJECT_NAME', '疑串项目名', '替换为当前项目名', None, '全文', '旧医疗大数据平台项目'),
            _FakeFinding('high', 'MISSING_RESPONSE', '漏应答财务报告', '需人工补充财报响应', None, '★3', '★3. 须提供近三年经审计财务报告。'),
        ],
        out_docx_path=str(commented_docx),
    )

    with zipfile.ZipFile(str(commented_docx), 'r') as zf:
        assert 'word/comments.xml' in zf.namelist()
        assert b'commentRangeStart' in zf.read('word/document.xml')

    accepted_findings = [
        {
            'id': 101,
            'code': 'CROSS_PROJECT_NAME',
            'severity': 'error',
            'clause_no': '全文',
            'bid_quote': '旧医疗大数据平台项目',
            'message': '发现疑似串名',
            'suggestion': '替换为智慧城市应急指挥平台项目',
        },
        {
            'id': 102,
            'code': 'MISSING_RESPONSE',
            'severity': 'error',
            'clause_no': '★3',
            'quote': '★3. 须提供近三年经审计财务报告。',
            'message': '未检出近三年审计报告响应',
            'suggestion': '请技术商务负责人线下补写并附财报附件',
        },
    ]

    clean_out = tmp_path / 'bid_clean_revised.docx'
    res = export_clean_revised_docx(
        commented_docx,
        accepted_findings,
        clean_out,
        replacements={101: '智慧城市应急指挥平台项目'},
    )

    assert res['applied_count'] == 1
    assert res['manual_pending_count'] == 1
    assert res['manual_pending_items'][0]['code'] == 'MISSING_RESPONSE'

    # 验证输出 .docx 零批注 XML 残留
    with zipfile.ZipFile(str(clean_out), 'r') as zf:
        names = zf.namelist()
        assert not any('comments' in n.lower() for n in names)
        for n in names:
            if n.endswith(('.xml', '.rels')):
                raw = zf.read(n).decode('utf-8', errors='ignore')
                assert 'commentRangeStart' not in raw
                assert 'commentRangeEnd' not in raw
                assert 'commentReference' not in raw
                assert '/word/comments' not in raw

    # 验证排版（页眉/页脚、加粗与字体大小）及内容替换完整保留
    out_doc = Document(str(clean_out))
    assert out_doc.sections[0].header.paragraphs[0].text == '投标正本 - 智慧城市应急指挥平台项目'
    assert out_doc.sections[0].footer.paragraphs[0].text == '第 1 页 · 保密文件'
    p_runs = out_doc.paragraphs[0].runs
    replaced_runs = [r for r in p_runs if '智慧城市应急指挥平台项目' in r.text]
    assert replaced_runs
    assert replaced_runs[0].bold is True
    assert replaced_runs[0].font.size == Pt(14)
    assert out_doc.tables[0].rows[0].cells[1].text == '智慧城市应急指挥平台项目二期延续'
    assert '旧医疗大数据平台项目' not in _collect_docx_all_text(clean_out)


# ── 4 & 5. API 全链路：PDF 422 拦截 + Clean 导出 + /api/composer CRUD 与租户隔离 ──

def test_composer_crud_isolation_and_clean_bid_api_with_pdf_guard(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('SEED_DEMO_DATA', '1')
    db_file = tmp_path / 'e37_api.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    tender_p = _build_sample_tender_docx(tmp_path / 'api_tender.docx')
    ref_p = tmp_path / 'api_ref.docx'
    ref_doc = Document()
    ref_doc.add_paragraph('我司具备成熟应急指挥多部门协同调度联动能力与可视化驾驶舱大屏方案。')
    ref_doc.save(str(ref_p))

    bid_docx = tmp_path / 'api_bid.docx'
    bdoc = Document()
    bdoc.add_paragraph('我司承接过旧市政项目系统，将投入完整团队服务本次招标。')
    bdoc.save(str(bid_docx))

    bid_pdf = _build_pdf_file(tmp_path / 'api_bid.pdf', '这是PDF格式投标书正文内容，超过一百个字符用于满足基本PDF文本长度验证要求。' * 4)

    created_deliver_files: list[Path] = []
    try:
        with TestClient(app) as tc:
            admin_h = _login_headers(tc, 'admin')
            assert tc.post(
                '/api/auth/users',
                headers=admin_h,
                json={'username': 'e37_t2', 'password': 'Password123!', 'tenant_id': 2, 'role': 'member'},
            ).status_code == 200
            t2_h = _login_headers(tc, 'e37_t2', 'Password123!')

            # ─ A. /api/composer 正向编制 CRUD + V2 参考方案 + 多租户隔离 + 级联删盘 ─
            r_gen = tc.post(
                '/api/composer/generate',
                headers=admin_h,
                data={'compose_mode': 'knowledge_draft_v2', 'title': '应急协同初稿测试'},
                files=[
                    ('tender_file', ('api_tender.docx', tender_p.read_bytes(), 'application/octet-stream')),
                    ('knowledge_files', ('api_ref.docx', ref_p.read_bytes(), 'application/octet-stream')),
                ],
            )
            assert r_gen.status_code == 200, r_gen.text
            draft = r_gen.json()
            did = draft['id']
            assert draft['compose_mode'] == 'knowledge_draft_v2'
            assert draft['scoring_fill_mode'] == 'rag_knowledge'

            r_list = tc.get('/api/composer/drafts', headers=admin_h)
            assert r_list.status_code == 200
            assert r_list.json()['total'] == 1

            r_dl = tc.get(f'/api/composer/drafts/{did}/download', headers=admin_h)
            assert r_dl.status_code == 200
            assert zipfile.is_zipfile(BytesIO(r_dl.content))

            # 跨租户严格 404
            assert tc.get('/api/composer/drafts', headers=t2_h).json()['total'] == 0
            assert tc.get(f'/api/composer/drafts/{did}', headers=t2_h).status_code == 404
            assert tc.get(f'/api/composer/drafts/{did}/download', headers=t2_h).status_code == 404
            assert tc.delete(f'/api/composer/drafts/{did}', headers=t2_h).status_code == 404

            up_dir = PROJECT_ROOT / 'data' / 'uploads' / 'composer' / '1' / str(did)
            out_docx = PROJECT_ROOT / 'output' / 'composer' / f'1_{did}_投标文件初稿骨架.docx'
            assert up_dir.is_dir()
            assert out_docx.is_file()

            r_del = tc.delete(f'/api/composer/drafts/{did}', headers=admin_h)
            assert r_del.status_code == 200
            assert not up_dir.exists()
            assert not out_docx.exists()

            # ─ B. /api/reports/{id}/clean-bid：PDF 422 拦截 + Word 导出 + 审计 ─
            import backend.database as db_mod
            from backend.models import AuditLog, BidCheckTask, BidFinding

            assert db_mod.SessionLocal is not None
            with db_mod.SessionLocal() as s:
                t_pdf = BidCheckTask(
                    tenant_id=1,
                    user_id=1,
                    task_type='pre_check',
                    title='PDF投标核验任务',
                    status='completed',
                    tender_filename='api_tender.docx',
                    bid_filename='api_bid.pdf',
                    tender_path=str(tender_p),
                    bid_path=str(bid_pdf),
                )
                t_doc = BidCheckTask(
                    tenant_id=1,
                    user_id=1,
                    task_type='pre_check',
                    title='Word投标核验任务',
                    status='completed',
                    tender_filename='api_tender.docx',
                    bid_filename='api_bid.docx',
                    tender_path=str(tender_p),
                    bid_path=str(bid_docx),
                )
                s.add_all([t_pdf, t_doc])
                s.flush()
                f_name = BidFinding(
                    task_id=t_doc.id,
                    source_module='F3',
                    code='CROSS_PROJECT_NAME',
                    severity='error',
                    clause_no='全文',
                    bid_quote='旧市政项目系统',
                    message='串名风险',
                    suggestion='替换为智慧城市应急指挥平台系统',
                    feedback='accepted',
                )
                f_miss = BidFinding(
                    task_id=t_doc.id,
                    source_module='F4',
                    code='MISSING_RESPONSE',
                    severity='error',
                    clause_no='★1',
                    quote='★1. 须承诺2小时到达现场',
                    message='漏应答',
                    suggestion='请人工补充承诺段落',
                    feedback='accepted',
                )
                s.add_all([f_name, f_miss])
                s.commit()
                t_pdf_id = t_pdf.id
                t_doc_id = t_doc.id
                f_name_id = f_name.id

            # PDF 原件必须拦截返回 422 PDF_CLEAN_EXPORT_UNSUPPORTED
            r_pdf = tc.post(f'/api/reports/{t_pdf_id}/clean-bid', headers=admin_h, json={})
            assert r_pdf.status_code == 422, r_pdf.text
            assert r_pdf.json()['detail']['code'] == 'PDF_CLEAN_EXPORT_UNSUPPORTED'

            # Word 原件执行安全替换 + 漏应答转入 manual_pending_items
            r_clean = tc.post(
                f'/api/reports/{t_doc_id}/clean-bid',
                headers=admin_h,
                json={'replacements': {str(f_name_id): '智慧城市应急指挥平台系统'}},
            )
            assert r_clean.status_code == 200, r_clean.text
            c_data = r_clean.json()
            assert c_data['applied_count'] == 1
            assert c_data['manual_pending_count'] == 1
            assert c_data['manual_pending_items'][0]['code'] == 'MISSING_RESPONSE'
            created_deliver_files.append(PROJECT_ROOT / c_data['output_path'])

            r_clean_dl = tc.get(f'/api/reports/{t_doc_id}/clean-bid/download', headers=admin_h)
            assert r_clean_dl.status_code == 200
            clean_doc = Document(BytesIO(r_clean_dl.content))
            full_clean_txt = '\n'.join(p.text for p in clean_doc.paragraphs)
            assert '智慧城市应急指挥平台系统' in full_clean_txt
            assert '旧市政项目系统' not in full_clean_txt

            # 跨租户访问 clean-bid 严格 404
            assert tc.post(f'/api/reports/{t_doc_id}/clean-bid', headers=t2_h, json={}).status_code == 404
            assert tc.get(f'/api/reports/{t_doc_id}/clean-bid/download', headers=t2_h).status_code == 404

            # ─ C. 验证基于已有 task_id 复用生成 skeleton_v1 草稿及删除 ─
            r_from_task = tc.post(
                '/api/composer/generate',
                headers=admin_h,
                data={'task_id': str(t_doc_id), 'compose_mode': 'skeleton_v1'},
            )
            assert r_from_task.status_code == 200, r_from_task.text
            td = r_from_task.json()
            assert td['compose_mode'] == 'skeleton_v1'
            assert tc.delete(f"/api/composer/drafts/{td['id']}", headers=admin_h).status_code == 200

            # 验证审计与大盘隔离
            with db_mod.SessionLocal() as s:
                actions = {r.action for r in s.query(AuditLog).filter_by(tenant_id=1).all()}
                assert 'composer_generate' in actions
                assert 'composer_delete' in actions
                assert 'export_clean_bid' in actions

            # 注意上述手工插入的 2 个 pre_check 任务计入大盘，而 /composer 生成的草稿不计入大盘
            dash = tc.get('/api/dashboard', headers=admin_h).json()
            assert dash['total_projects'] == 2
    finally:
        for fp in created_deliver_files:
            try:
                if fp.is_file():
                    fp.unlink()
            except Exception:
                pass
