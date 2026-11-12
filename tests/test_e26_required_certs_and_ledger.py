# -*- coding: utf-8 -*-
"""E26 · 招标所需证照清单确定性抽取与企业台账联动对账专项单元测试（0 Token 离线可跑）。

覆盖任务卡 §3 全部 5 组验收标准：
1. 资格门槛 + 负面免交护栏：识别营业执照/财务报告/完税/社保/授权委托书等，拦截"投标文件中无需提供证明材料"与目录点线；
2. 政府强制节能产品认证提取：命中品目清单类说明中的强制节能产品认证证书并归类 mandatory + product_cert；
3. 评分细则表/正文证书提取：识别 ISO9001/ISO14001/ISO45001、软件著作权登记证书、信息系统项目管理师证书等并归类 bonus；
4. 台账四态保守对账与多租户隔离：准确识别 valid / expiring_soon / expired / not_in_ledger（按 open_date 为基准），绝不把 ISO9001 误配给 ISO27001；
5. /api/extract 接口联调闭环：验证租户 A 的台账证照正确对账而绝不串联租户 B 证照，支持 scoring_prose 扩展合并与向下兼容。
"""
from __future__ import annotations

from datetime import date
import io
import os
import sys

from docx import Document
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import backend.database as db_mod
from backend.main import create_app
from backend.models import CompanyCertificate, Tenant
from core.qualification_scanner import match_required_certificates_with_ledger
from core.tender_extractor import build_review_checklist, extract_required_certificates


def _make_docx_with_table(paragraphs: list[str], score_rows: list[list[str]]) -> bytes:
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    if score_rows:
        tbl = doc.add_table(rows=len(score_rows), cols=len(score_rows[0]))
        for ri, row in enumerate(score_rows):
            for ci, val in enumerate(row):
                tbl.rows[ri].cells[ci].text = val
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_e26_mandatory_certs_and_exempt_guard():
    sample = """目录
第一章 投标邀请 ............ 1

第二章 投标人须知
一、申请人的资格要求
1.1 投标人须具有有效的营业执照或事业单位法人证书；
1.2 提供上一年度财务状况报告；
1.3 提供依法缴纳税收和社会保障资金的相关证明材料；
2. 在“信用中国”网站（www.creditchina.gov.cn）查询信用记录（投标文件中无需提供证明材料，由采购人现场查询）；
3. 本项目的特定资格要求：无。
4. 投标人需办理 CA数字证书及电子签章参与电子投标，法定代表人授权委托书按格式提供。
"""
    items = extract_required_certificates(sample)
    names = {it['cert_name']: it for it in items}

    assert '营业执照 / 法人身份证明' in names
    assert '财务状况报告 / 审计报告' in names
    assert '依法缴纳税收证明' in names
    assert '社会保障资金缴纳证明' in names
    assert 'CA 数字证书与电子签章' in names
    assert '法定代表人授权委托书' in names
    assert '信用承诺函' not in names

    for cname in ('营业执照 / 法人身份证明', '财务状况报告 / 审计报告', 'CA 数字证书与电子签章'):
        it = names[cname]
        assert it['requirement_level'] == 'mandatory'
        assert it['quote_verified'] is True
        assert it['offset'] is not None


def test_e26_energy_saving_usage_note_product_cert(tmp_path):
    """即使在 ★ 计数口径被归类为 usage_note，涉及强制节能产品认证证书时仍需纳入必填清单。"""
    tender_docx = tmp_path / 'energy_tender.docx'
    content = _make_docx_with_table(
        paragraphs=[
            '第一章 采购需求',
            '开标时间：2026年11月20日09时30分',
            '第七十七条 依据财政部、发展改革委发布的《节能产品政府采购品目清单》，'
            '本项目中有政府强制采购节能产品的（属于清单中打★品目的），只能选择具有国家确定的认证机构出具的、'
            '处于有效期之内的节能产品认证证书的产品进行报价。',
        ],
        score_rows=[
            ['序号', '评审因素', '评审标准', '分值'],
            [
                '1',
                '企业资质',
                '投标人具有有效的ISO9001质量管理体系认证证书、ISO14001环境管理体系认证、ISO45001职业健康安全管理体系认证，每提供1项得3分。',
                '9',
            ],
            [
                '2',
                '软件能力与团队',
                '投标人具有自研软件著作权证书得3分；项目经理具有信息系统项目管理师证书得4分。',
                '7',
            ],
        ],
    )
    tender_docx.write_bytes(content)

    res = build_review_checklist(str(tender_docx))
    assert res['open_date'] == '2026-11-20'
    certs = res['required_certificates']
    by_name = {c['cert_name']: c for c in certs}

    # usage_note 中的节能产品认证证书必须被准确抽取
    assert '节能产品认证证书' in by_name
    assert by_name['节能产品认证证书']['requirement_level'] == 'mandatory'
    assert by_name['节能产品认证证书']['category'] == 'product_cert'
    assert by_name['节能产品认证证书']['quote_verified'] is True
    assert by_name['节能产品认证证书']['offset'] is not None

    # 评分细则表里的企业资质、软著与人员证书均应提取为 bonus
    for bonus_name in (
        'ISO 9001 质量管理体系认证',
        'ISO 14001 环境管理体系认证',
        'ISO 45001 职业健康安全管理体系认证',
        '软件著作权登记证书（软著）',
        '信息系统项目管理师证书',
    ):
        assert bonus_name in by_name, f'missing {bonus_name}'
        assert by_name[bonus_name]['requirement_level'] == 'bonus'


def test_e26_ledger_matching_statuses_and_no_cross_family():
    req_certs = [
        {
            'cert_name': 'ISO 9001 质量管理体系认证',
            'category': 'enterprise_qualification',
            'requirement_level': 'bonus',
            'source_section': '评分细则表',
            'quote': '具有ISO9001质量管理体系认证证书',
            'quote_verified': True,
            'offset': 10,
            'page': 1,
        },
        {
            'cert_name': 'ISO/IEC 27001 信息安全管理体系认证',
            'category': 'enterprise_qualification',
            'requirement_level': 'mandatory',
            'source_section': '资格审查',
            'quote': '具有ISO27001信息安全管理体系认证证书',
            'quote_verified': True,
            'offset': 20,
            'page': 1,
        },
        {
            'cert_name': '软件著作权登记证书（软著）',
            'category': 'enterprise_qualification',
            'requirement_level': 'bonus',
            'source_section': '评分细则表',
            'quote': '提供自研软件著作权证书',
            'quote_verified': True,
            'offset': 30,
            'page': 2,
        },
        {
            'cert_name': '信息系统项目管理师证书',
            'category': 'staff_cert',
            'requirement_level': 'bonus',
            'source_section': '评分细则表',
            'quote': '项目经理具有信息系统项目管理师证书',
            'quote_verified': True,
            'offset': 40,
            'page': 2,
        },
    ]
    ledger = [
        # 有效（距离 2026-11-20 超过 30 天）
        {
            'id': 1,
            'cert_name': 'ISO9001质量管理体系认证证书',
            'cert_no': 'ISO9001-01',
            'valid_until': date(2027, 6, 30),
            'warn_days': 30,
            'attachment_name': 'iso9001.pdf',
            'attachment_path': 'data/uploads/certs/1/1/iso9001.pdf',
        },
        # 已过期 1 张 + 临期 1 张：应择优保留 expiring_soon 且 matched_count=2
        {
            'id': 2,
            'cert_name': '智慧政务统一认证软件著作权登记证书（旧）',
            'cert_no': 'RZ-OLD',
            'valid_until': date(2026, 10, 1),
            'warn_days': 30,
        },
        {
            'id': 3,
            'cert_name': '数字政务平台软件著作权证书',
            'cert_no': 'RZ-NEW',
            'valid_until': date(2026, 12, 10),
            'warn_days': 30,
        },
        # 仅在开标日 2026-11-20 已过期
        {
            'id': 4,
            'cert_name': '信息系统项目管理师（高级）',
            'cert_no': 'PMP-CN-99',
            'valid_until': date(2026, 11, 1),
            'warn_days': 30,
        },
    ]

    matched, summary = match_required_certificates_with_ledger(
        req_certs, ledger, reference_date='2026-11-20',
    )
    by_name = {m['cert_name']: m for m in matched}

    assert by_name['ISO 9001 质量管理体系认证']['ledger_status'] == 'valid'
    assert by_name['ISO 9001 质量管理体系认证']['matched_cert']['has_attachment'] is True

    # ISO9001 绝不能误匹配给 ISO27001
    assert by_name['ISO/IEC 27001 信息安全管理体系认证']['ledger_status'] == 'not_in_ledger'
    assert by_name['ISO/IEC 27001 信息安全管理体系认证']['matched_cert'] is None

    # 2 条软著命中，择优为 expiring_soon
    assert by_name['软件著作权登记证书（软著）']['ledger_status'] == 'expiring_soon'
    assert by_name['软件著作权登记证书（软著）']['matched_count'] == 2
    assert by_name['软件著作权登记证书（软著）']['matched_cert']['cert_no'] == 'RZ-NEW'

    assert by_name['信息系统项目管理师证书']['ledger_status'] == 'expired'
    assert by_name['信息系统项目管理师证书']['matched_cert']['days_remaining'] == -19

    assert summary == {
        'total': 4,
        'mandatory_count': 1,
        'bonus_count': 3,
        'valid_count': 1,
        'expiring_soon_count': 1,
        'expired_count': 1,
        'not_in_ledger_count': 1,
        'reference_date': '2026-11-20',
        'reference_date_source': 'open_date',
    }


def test_e26_api_extract_ledger_integration_and_tenant_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'test_e26_extract.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    tender_bytes = _make_docx_with_table(
        paragraphs=[
            '第一章 投标人须知',
            '开标日期：2026年11月25日',
            '申请人的资格要求：1. 提供有效的企业营业执照；2. 提供财务状况报告；',
        ],
        score_rows=[
            ['序号', '评审因素', '评审标准', '分值'],
            ['1', '体系认证', '具有ISO/IEC 27001信息安全管理体系认证证书得3分。', '3'],
        ],
    )

    with TestClient(app) as tc:
        r_login = tc.post('/api/auth/login', json={'username': 'admin', 'password': 'BidGuard2026!'})
        assert r_login.status_code == 200
        hdrs = {'Authorization': f"Bearer {r_login.json()['token']}"}
        tenant_id = r_login.json()['user']['tenant_id']

        # 给当前租户录入有效的营业执照，给另一租户 B 录入 ISO 27001（验证绝不跨租户串台账）
        with db_mod.SessionLocal() as sess:
            sess.query(CompanyCertificate).filter_by(tenant_id=tenant_id).delete()
            other_tenant = Tenant(name='独立竞争部门B', is_active=True)
            sess.add(other_tenant)
            sess.flush()

            sess.add(CompanyCertificate(
                tenant_id=tenant_id,
                cert_name='企业法人营业执照（三证合一）',
                cert_no='91320000MA123456',
                valid_until=date(2028, 12, 31),
                warn_days=30,
            ))
            sess.add(CompanyCertificate(
                tenant_id=other_tenant.id,
                cert_name='ISO/IEC 27001 信息安全管理体系认证证书',
                cert_no='OTHER-27001',
                valid_until=date(2028, 12, 31),
                warn_days=30,
            ))
            sess.commit()

        r_ext = tc.post(
            '/api/extract',
            headers=hdrs,
            files={
                'file': (
                    'e26_tender.docx',
                    tender_bytes,
                    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                )
            },
        )
        assert r_ext.status_code == 200, r_ext.text
        data = r_ext.json()

        assert data['open_date'] == '2026-11-25'
        assert 'required_certificates' in data
        assert 'cert_ledger_summary' in data

        by_name = {c['cert_name']: c for c in data['required_certificates']}
        assert by_name['营业执照 / 法人身份证明']['ledger_status'] == 'valid'
        assert by_name['营业执照 / 法人身份证明']['matched_cert']['cert_no'] == '91320000MA123456'

        # 财务状况报告未收录；ISO 27001 虽在租户 B 存在但对租户 A 必须判定为 not_in_ledger
        assert by_name['财务状况报告 / 审计报告']['ledger_status'] == 'not_in_ledger'
        assert by_name['ISO/IEC 27001 信息安全管理体系认证']['ledger_status'] == 'not_in_ledger'

        summary = data['cert_ledger_summary']
        assert summary['reference_date'] == '2026-11-25'
        assert summary['reference_date_source'] == 'open_date'
        assert summary['valid_count'] == 1
        assert summary['not_in_ledger_count'] == 2
