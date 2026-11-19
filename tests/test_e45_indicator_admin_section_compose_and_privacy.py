# -*- coding: utf-8 -*-
"""E45 · 企业指标库终审工作台、F8 单章专项补写与合规脱敏扩展卡 全链路验收测试。

重点验证三大防线：
1. 防线 ①：指标库在线写入（新增/编辑条目、评分参考、参数槽、关键词）强制经过 `_BID_MONEY_RE` 防造假锁（防绕过），加上业务线终审签批与内容变更自动打回待审；
2. 防线 ②：F8 单章专项补写 `compose_scope='section'` 与暗标模式（`blind_bid_mode=True`）、企业指标库注入（`use_indicator_library=True`）及多参考附件（`knowledge_files`）的叠加行为；
3. 防线 ③：`PRIVACY_MASK_CONTACT_ENABLED` 默认关闭（`'0'`）时保持 2 键 `{'id_masked', 'bank_masked'}` 零回归，显式开启时对手机号与邮箱执行 100% 等长幂等掩码。
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

from docx import Document
from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.main import create_app
import core.indicator_library as indicator_lib_mod
from core.indicator_library import _BID_MONEY_RE
from core.llm_gateway import LLMClient
from core.privacy_sanitizer import (
    is_contact_privacy_mask_enabled,
    sanitize_text,
    scan_document_text,
)


def _login_headers(client: TestClient, username: str, password: str = 'BidGuard2026!') -> dict[str, str]:
    resp = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert resp.status_code == 200, resp.text
    return {'Authorization': f"Bearer {resp.json()['token']}"}


def _build_docx_bytes(paragraphs: list[str], tables: list[list[list[str]]] | None = None) -> bytes:
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    for tbl_rows in tables or []:
        if not tbl_rows:
            continue
        tbl = doc.add_table(rows=len(tbl_rows), cols=len(tbl_rows[0]))
        for r_idx, row in enumerate(tbl_rows):
            for c_idx, val in enumerate(row):
                tbl.rows[r_idx].cells[c_idx].text = val
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ── 防线 ①：企业指标库在线 CRUD、_BID_MONEY_RE 防造假锁（防绕过）与人工终审签批流 ──────

def test_e45_indicator_library_admin_crud_anti_forgery_lock_and_approval_flow(tmp_path, monkeypatch):
    # 隔离正式库目录，确保测试零污染 data/rules/indicator_library/
    real_lib_dir = indicator_lib_mod.DEFAULT_INDICATOR_LIBRARY_DIR
    isolated_dir = tmp_path / 'indicator_library'
    isolated_dir.mkdir(parents=True, exist_ok=True)
    for src in real_lib_dir.glob('*_indicators.json'):
        shutil.copyfile(src, isolated_dir / src.name)
    monkeypatch.setattr(indicator_lib_mod, 'DEFAULT_INDICATOR_LIBRARY_DIR', isolated_dir)

    app = create_app()
    with TestClient(app) as client:
        admin_headers = _login_headers(client, 'admin')
        member_headers = _login_headers(client, 'liming')

        # 1. 列出业务线：默认包含 common / security / maas
        r_list = client.get('/api/composer/indicators/lines', headers=member_headers)
        assert r_list.status_code == 200, r_list.text
        lines_data = r_list.json()
        assert lines_data['total'] >= 3
        line_keys = [it['line'] for it in lines_data['items']]
        assert 'common' in line_keys and 'security' in line_keys and 'maas' in line_keys

        # 2. 权限门禁：member 调用写接口与终审接口一律 403
        r_forbid_line = client.post(
            '/api/composer/indicators/lines',
            headers=member_headers,
            json={'line': 'cloud_ops', 'display_name': '政务云专精线'},
        )
        assert r_forbid_line.status_code == 403

        r_forbid_entry = client.post(
            '/api/composer/indicators/entries',
            headers=member_headers,
            json={
                'line': 'common',
                'category': '技术方案',
                'indicator_name': '越权写入测试',
                'standard_response': '不应成功写入的正文内容。',
            },
        )
        assert r_forbid_entry.status_code == 403

        r_forbid_approve = client.post(
            '/api/composer/indicators/approve',
            headers=member_headers,
            json={'line': 'common'},
        )
        assert r_forbid_approve.status_code == 403

        # 3. Admin 新增自定义业务线 cloud_ops（及非法路径穿越拒绝）
        r_bad_line = client.post(
            '/api/composer/indicators/lines',
            headers=admin_headers,
            json={'line': '../evil', 'display_name': '非法路径'},
        )
        assert r_bad_line.status_code == 422

        r_create_line = client.post(
            '/api/composer/indicators/lines',
            headers=admin_headers,
            json={
                'line': 'cloud_ops',
                'display_name': '政务云与算力运维专精线',
                'description': '面向政企云原生底座、容器集群与驻场运维项目。',
            },
        )
        assert r_create_line.status_code == 200, r_create_line.text
        assert r_create_line.json()['line'] == 'cloud_ops'
        assert r_create_line.json()['review_status'] == 'pending_human_review'

        # 4. 防线 ① 核心测试：在线新增包含具体人民币报价数字的指标（覆盖正文、评分参考、参数槽、关键词）
        dirty_payload = {
            'line': 'cloud_ops',
            'category': '售后服务',
            'indicator_name': '云平台驻场运维与报价优惠承诺（含报价46000元）',
            'standard_response': (
                '我方提供 7×24 小时驻场工程师响应，基础平台许可报价46000元，'
                '年度维保服务费￥12.8万元/年，大模型算力调用单价低至15元/M Tokens。'
            ),
            'scoring_ref': {
                'weight_hint': '6~10分',
                'typical_points': ['驻场运维 SLA 响应承诺', '三年延保费用不超过8.5万元'],
                'high_score_tips': '严禁在技术册暴露总价1,250,000.00元等商务数字。',
            },
            'param_slots': ['[待人工确认：驻场人数]', '备件池专项预算：98500元'],
            'keywords': ['驻场运维', 'SLA', '报价46000元'],
            'source_ref': '管理员在线录入',
        }
        r_add = client.post(
            '/api/composer/indicators/entries',
            headers=admin_headers,
            json=dirty_payload,
        )
        assert r_add.status_code == 200, r_add.text
        add_out = r_add.json()
        assert add_out['money_sanitized'] is True
        entry = add_out['entry']
        entry_id = entry['indicator_id']

        # 验证返回对象与落盘 JSON 均 100% 不含任何具体金额数字，且已转化为 [待人工填报：...] 槽位
        serialized_entry = json.dumps(entry, ensure_ascii=False)
        assert '46000' not in serialized_entry
        assert '12.8万' not in serialized_entry
        assert '15元/M' not in serialized_entry
        assert '8.5万' not in serialized_entry
        assert '1,250,000' not in serialized_entry
        assert '98500' not in serialized_entry
        assert '[待人工填报：' in entry['standard_response']
        assert any('[待人工填报：' in s for s in entry['param_slots'])

        disk_text = (isolated_dir / 'cloud_ops_indicators.json').read_text(encoding='utf-8')
        assert _BID_MONEY_RE.search(disk_text) is None

        # 5. 签署人工终审通过 -> 状态跃迁为 approved 并记录签批人、时间与 SHA256
        r_approve = client.post(
            '/api/composer/indicators/approve',
            headers=admin_headers,
            json={'line': 'cloud_ops', 'review_note': '已完成全部槽位终审核对'},
        )
        assert r_approve.status_code == 200, r_approve.text
        approved_detail = r_approve.json()
        assert approved_detail['review_status'] == 'approved'
        assert approved_detail['reviewed_by'] == 'admin'
        assert approved_detail['reviewed_at']
        assert len(approved_detail['content_hash']) >= 16

        # 6. 在线编辑已签批业务线中的指标（再次注入具体金额测试二次防绕过 + 状态自动打回待审）
        r_upd = client.put(
            f"/api/composer/indicators/entries/{entry_id}",
            headers=admin_headers,
            json={
                'line': 'cloud_ops',
                'category': '售后服务',
                'indicator_name': '云平台驻场运维与应急保障方案',
                'standard_response': (
                    '我方承诺核心故障 [待人工确认：响应分钟数] 内到场，'
                    '巡检增值包收费35000元/次，提供双活容灾演练。'
                ),
                'scoring_ref': {
                    'weight_hint': '8分',
                    'typical_points': ['故障分级响应与双活演练'],
                    'high_score_tips': '附上 SLA 响应等级矩阵表',
                },
                'param_slots': ['[待人工确认：响应分钟数]'],
                'keywords': ['驻场运维', '双活演练'],
            },
        )
        assert r_upd.status_code == 200, r_upd.text
        upd_out = r_upd.json()
        assert upd_out['money_sanitized'] is True
        assert '35000' not in json.dumps(upd_out['entry'], ensure_ascii=False)
        # 内容哈希改变后自动从 approved 打回 pending_human_review
        assert upd_out['review_status'] == 'pending_human_review'

        # 7. 删除指标条目
        r_del = client.delete(
            f"/api/composer/indicators/entries/{entry_id}",
            params={'line': 'cloud_ops'},
            headers=admin_headers,
        )
        assert r_del.status_code == 200, r_del.text


# ── 防线 ②：单章补写 compose_scope='section' × 暗标模式 × 指标库注入 × 多参考附件 ──

def test_e45_single_section_compose_with_blind_mode_indicator_lib_and_multi_refs(tmp_path):
    tender_bytes = _build_docx_bytes(
        paragraphs=[
            '项目名称：某市政务云安全运营与培训服务采购项目',
            '项目编号：ZFCG-2026-E4501',
            '采购人：某市大数据发展管理局',
            '投标最高限价：1800000.00元',
            '开标时间：2026年12月15日 09:30',
            '本技术标采用暗标评审，投标文件中不得出现投标人名称、人员姓名、联系电话及历史项目业绩。',
            '★1. 投标人须提供至少3名驻场安全运维工程师承诺。',
            '▲2. 提供完整的实操培训课程表与考核闭环方案。',
        ],
        tables=[
            [
                ['评分因素', '分值', '评分标准'],
                ['技术方案', '25分', '总体架构设计合理、安全防护体系完备得满分。'],
                ['培训方案', '10分', '培训课程体系完整、配备3天实操演练课表与考核发证机制得10分。'],
                ['售后服务方案', '10分', '提供7×24小时应急响应与定期巡检服务得10分。'],
            ]
        ],
    )

    ref_doc_1 = _build_docx_bytes(
        paragraphs=[
            '培训方案与实操演练课程设计：由华信安泰科技有限公司安全学院讲师张伟负责授课，联系电话 13812345678。',
            '课程涵盖安全攻防演练、平台运维实操、应急处置桌面推演三大模块，培训结束后组织闭卷笔试与上机实操双考核。',
        ]
    )
    ref_doc_2 = _build_docx_bytes(
        paragraphs=[
            '培训师资与教材交付保障：华信安泰科技有限公司编制全套《政务云安全运维实操手册》与实验指导书，客服邮箱 support@huaxinantai.com。',
        ]
    )

    app = create_app()
    with TestClient(app) as client:
        headers = _login_headers(client, 'admin')

        resp = client.post(
            '/api/composer/generate',
            headers=headers,
            files=[
                ('tender_file', ('某市政务云招标文件.docx', tender_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')),
                ('knowledge_files', ('参考培训课表1.docx', ref_doc_1, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')),
                ('knowledge_files', ('参考培训教材2.docx', ref_doc_2, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')),
            ],
            data={
                'compose_mode': 'knowledge_draft_v2',
                'compose_scope': 'section',
                'target_section_topic': '培训方案',
                'target_section_guidance': '补充3天实操演练课表与上机考核闭环机制',
                'density_preset': 'detailed',
                'table_density': 'none',
                'use_ai': 'false',
                'use_indicator_library': 'true',
                'blind_bid_mode': 'true',
                'blind_custom_words_json': json.dumps(['华信安泰'], ensure_ascii=False),
                'async_run': 'false',
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data['compose_scope'] == 'section'
        assert data['target_section_topic'] == '培训方案'
        assert data['density_preset'] == 'detailed'
        assert data['table_density'] == 'none'
        assert data['blind_bid_mode'] is True
        assert data['use_indicator_library'] is True

        bp = data['blueprint']
        assert bp['compose_scope'] == 'section'
        assert bp['target_section_topic'] == '培训方案'
        assert bp['target_section_guidance'] == '补充3天实操演练课表与上机考核闭环机制'

        # 单章节专项模式绝不生成整书封面(sec_cover)、资格附页(sec_qual)、报价表(sec_price)或整书偏离表(sec_deviation)
        sec_ids = [s['section_id'] for s in bp['sections']]
        assert sec_ids == ['sec_section_anchor', 'sec_scoring', 'sec_appendix']

        # 评分细则原文对照聚焦于目标章节「培训方案」
        focus_items = bp.get('tender_scoring_focus') or []
        assert len(focus_items) >= 1
        assert any('培训' in it.get('chapter', '') for it in focus_items)

        # 检查 sec_scoring 同时注入了企业指标库（CMN-004 培训赋能体系）与上传的多参考附件片段
        sec_scoring = next(s for s in bp['sections'] if s['section_id'] == 'sec_scoring')
        blocks = sec_scoring['content_blocks']
        assert len(blocks) >= 1
        all_snippets = []
        for blk in blocks:
            # table_density='none' 下绝不生成内嵌表格
            assert blk.get('table') is None
            all_snippets.extend(blk.get('reference_snippets') or [])
        assert len(all_snippets) >= 2
        sources_blob = ' '.join(str(sn.get('source_filename', '')) for sn in all_snippets)
        assert '公司指标标准库' in sources_blob
        assert 'knowledge_' in sources_blob or '参考培训' in sources_blob

        # 暗标叠加校验：蓝图 JSON 与导出的 .docx 均已彻底隐去「华信安泰」「13812345678」「support@huaxinantai.com」
        bp_raw_str = json.dumps(bp, ensure_ascii=False)
        assert '华信安泰' not in bp_raw_str
        assert '13812345678' not in bp_raw_str
        assert 'support@huaxinantai.com' not in bp_raw_str

        # 下载导出的单章专项 Word 初稿并检验段落结构
        draft_id = data['id']
        r_dl = client.get(f"/api/composer/drafts/{draft_id}/download", headers=headers)
        assert r_dl.status_code == 200
        exported_doc = Document(io.BytesIO(r_dl.content))
        doc_full_text = '\n'.join(p.text for p in exported_doc.paragraphs)
        assert '专项章节' in doc_full_text
        assert '培训方案' in doc_full_text
        assert '正本 / 副本' not in doc_full_text
        assert '华信安泰' not in doc_full_text
        assert '13812345678' not in doc_full_text

        # 清理测试草稿
        client.delete(f"/api/composer/drafts/{draft_id}", headers=headers)


# ── 防线 ③：PRIVACY_MASK_CONTACT_ENABLED 默认关零回归 + 开启后手机号/邮箱等长幂等脱敏 ──

def test_e45_contact_privacy_mask_default_off_and_opt_in_equal_length_idempotent(monkeypatch):
    valid_id = '11010519491231002X'
    sample = (
        f"授权代表身份证号：{valid_id}，开户银行账号：6222 0210 0111 2223 334。\n"
        "项目联系人手机号：13812345678，备用手机：+86-18698765432，电子邮箱：zhangsan.bid@gov-cloud.com.cn。\n"
        "干扰项：国家标准 GB/T 19001-2016，投标报价 13800000000.00元，项目编号：13912345678，12位流水号 135001122334。"
    )

    # 1. 默认关闭（PRIVACY_MASK_CONTACT_ENABLED 未设或为 '0'）：保持 E40 双键结构，手机号与邮箱原样不动
    monkeypatch.delenv('PRIVACY_MASK_CONTACT_ENABLED', raising=False)
    assert is_contact_privacy_mask_enabled() is False
    masked_default, stats_default = sanitize_text(sample)
    assert stats_default == {'id_masked': 1, 'bank_masked': 1}
    assert len(masked_default) == len(sample)
    assert '13812345678' in masked_default
    assert 'zhangsan.bid@gov-cloud.com.cn' in masked_default
    assert scan_document_text(sample) == {'id_masked': 1, 'bank_masked': 1}

    # 2. 开启 PRIVACY_MASK_CONTACT_ENABLED=1：手机号与邮箱严格等长掩码 + 幂等 + 干扰项零误伤
    monkeypatch.setenv('PRIVACY_MASK_CONTACT_ENABLED', '1')
    assert is_contact_privacy_mask_enabled() is True
    masked_on, stats_on = sanitize_text(sample)
    assert stats_on == {
        'id_masked': 1,
        'bank_masked': 1,
        'phone_masked': 2,
        'email_masked': 1,
    }
    assert len(masked_on) == len(sample)
    assert '13812345678' not in masked_on
    assert '138****5678' in masked_on
    assert '+86-186****5432' in masked_on
    assert 'zhangsan.bid@gov-cloud.com.cn' not in masked_on
    assert 'z***********@gov-cloud.com.cn' in masked_on

    # 干扰项（标准号、带元金额、项目编号语境、12位数字）完全不误伤
    assert 'GB/T 19001-2016' in masked_on
    assert '13800000000.00元' in masked_on
    assert '项目编号：13912345678' in masked_on
    assert '135001122334' in masked_on

    # 二次脱敏严格幂等
    masked_on_2, stats_on_2 = sanitize_text(masked_on)
    assert masked_on_2 == masked_on
    assert stats_on_2 == {
        'id_masked': 0,
        'bank_masked': 0,
        'phone_masked': 0,
        'email_masked': 0,
    }

    # 3. LLMClient 三出口统计自动累计 privacy_phone_masked 与 privacy_email_masked
    monkeypatch.setenv('PRIVACY_SANITIZE_ENABLED', '1')
    sent_payloads: list[str] = []

    class _FakeCompletions:
        def create(self, **kwargs):
            sent_payloads.append(json.dumps(kwargs['messages'], ensure_ascii=False))
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='ok'))],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2),
            )

    gw = LLMClient(
        {
            'base_url': 'http://127.0.0.1:8000/v1',
            'models': {'flash': 'mock-flash', 'flagship': 'mock-flagship'},
            'supports_vision': False,
            'pricing': {},
        },
        'test-key',
    )
    gw.use_openai = True
    gw.client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions()))

    gw.chat(messages=[{'role': 'user', 'content': '联系电话 13911223344，邮箱 lisi@example.org'}])
    assert '13911223344' not in sent_payloads[0]
    assert '139****3344' in sent_payloads[0]
    assert 'l***@example.org' in sent_payloads[0]
    usage = gw.total_usage()
    assert usage['privacy_phone_masked'] == 1
    assert usage['privacy_email_masked'] == 1
