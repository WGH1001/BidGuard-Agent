# -*- coding: utf-8 -*-
"""E41 · 双业务线专精 Skills 与常见指标标准应答库全链路验收测试。"""
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

from docx import Document
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, inspect

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.main import create_app
from backend.models import Base, ComposerSkill, ensure_composer_skills_columns
from backend.routers.composer import ensure_builtin_composer_skills
from core.bid_composer import compose_bid_blueprint, export_composed_bid_docx
from core.composer_skills import BUILTIN_WRITING_SKILLS, parse_skill_markdown_or_json
from core.indicator_library import (
    DEFAULT_INDICATOR_LIBRARY_DIR,
    CompositeKnowledgeSnippetProvider,
    IndicatorLibraryProvider,
    _BID_MONEY_RE,
    compute_library_content_hash,
    get_biz_line_indicator_overview,
    load_indicator_library,
)
from scripts.build_indicator_library import build_draft_bundle


def _login_headers(client: TestClient, username: str = 'admin', password: str = 'BidGuard2026!') -> dict[str, str]:
    resp = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert resp.status_code == 200, resp.text
    return {'Authorization': f"Bearer {resp.json()['token']}"}


# ── 1. 0-Token 指标库三层叠加、跨线隔离、评分参考与报价硬锁 ──────────────────────

def test_e41_indicator_library_three_tier_isolation_and_price_lock(tmp_path: Path):
    # 校验三份正式库 JSON 哈希幂等、含 scoring_ref 且绝不含具体人民币报价金额
    for line, fname in (
        ('common', 'common_indicators.json'),
        ('security', 'security_indicators.json'),
        ('maas', 'maas_indicators.json'),
    ):
        fpath = DEFAULT_INDICATOR_LIBRARY_DIR / fname
        assert fpath.is_file(), f"缺少正式指标库文件：{fpath}"
        raw = json.loads(fpath.read_text(encoding='utf-8'))
        assert raw['schema_version'] == '1.0'
        assert raw['line'] == line
        assert len(raw['entries']) >= 5
        assert raw['content_hash'] == compute_library_content_hash(raw['entries'])
        for entry in raw['entries']:
            assert entry['scoring_ref']['typical_points']
            assert entry['scoring_ref']['weight_hint']
            assert not _BID_MONEY_RE.search(entry['standard_response']), (
                f"指标库禁止包含具体报价金额：{entry['indicator_id']}"
            )

    # 三层加载规则：general=仅 common；security=security+common；maas=maas+common
    gen_entries = load_indicator_library('general')
    sec_entries = load_indicator_library('security')
    maas_entries = load_indicator_library('maas')
    assert {e['line'] for e in gen_entries} == {'common'}
    assert {e['line'] for e in sec_entries} == {'security', 'common'}
    assert {e['line'] for e in maas_entries} == {'maas', 'common'}

    # 跨业务线严格隔离 + 通用底座叠加命中
    sec_prov = IndicatorLibraryProvider('security')
    maas_prov = IndicatorLibraryProvider('maas')
    gen_prov = IndicatorLibraryProvider('general')
    assert sec_prov.has_documents and maas_prov.has_documents and gen_prov.has_documents

    sec_iso = sec_prov.retrieve('将普通环境与安全工作环境相隔离，防范信息外泄', top_k=2)
    assert sec_iso and sec_iso[0]['indicator_id'] == 'sec_dual_env_isolation'
    assert sec_iso[0]['is_indicator_ref'] is True
    assert '[公司标准应答参考｜' in sec_iso[0]['snippet']
    assert '[评分标准参考]' in sec_iso[0]['snippet']

    # security 线不得命中 maas 专精指标
    sec_cross = sec_prov.retrieve('DeepSeek Token 计费 缓存单价 并行MaaS', top_k=3)
    assert all(h['line'] != 'maas' for h in sec_cross)
    assert sec_cross == []

    # maas 线命中 Token/多模型 且同时可命中 common 售后 SLA，不得命中 security 专用逻辑磁盘隔离
    maas_hits = maas_prov.retrieve('线上大模型底座接入 DeepSeek 本地私有模型 知识库', top_k=2)
    assert maas_hits and maas_hits[0]['indicator_id'] == 'maas_multi_model_gateway'
    maas_sla = maas_prov.retrieve('售后服务方案 7×24 故障响应时间 应急处理', top_k=2)
    assert maas_sla and maas_sla[0]['indicator_id'] == 'common_sla_support'
    assert '不超过 1 小时' in maas_sla[0]['snippet']
    maas_cross = maas_prov.retrieve('逻辑隔离磁盘访问控制 禁止虚拟机远程桌面登录', top_k=3)
    assert all(h['line'] != 'security' for h in maas_cross)
    assert maas_cross == []

    # CompositeKnowledgeSnippetProvider：指标库片段优先 + 本地参考片段互补去重
    class _FakeLocalProv:
        has_documents = True

        def retrieve(self, query: str, *, top_k: int = 2, min_score: float = 0.35):
            return [{'source_filename': '历史标书.docx', 'snippet': '本地补充案例架构描述。', 'score': 0.8}]

    comp = CompositeKnowledgeSnippetProvider([maas_prov, _FakeLocalProv()])
    merged = comp.retrieve('售后服务方案 7×24 故障响应时间', top_k=2)
    assert len(merged) >= 2
    assert merged[0].get('is_indicator_ref') is True
    assert merged[-1]['source_filename'] == '历史标书.docx'

    # 提炼脚本禁止写正式库，写草稿目录正常生成
    with pytest.raises(RuntimeError):
        build_draft_bundle([], tmp_path, DEFAULT_INDICATOR_LIBRARY_DIR)

    draft_src = tmp_path / 'sample_bid.docx'
    d = Document()
    d.add_paragraph('我方提供 7×24 小时电话支持，故障响应不超过 1 小时，到达现场不超过 24 小时。')
    d.add_paragraph('采用逻辑隔离磁盘访问控制技术防止木马破坏，并支持并行MaaS平台 DeepSeek 1M 上下文调用，含税价￥46000。')
    d.save(str(draft_src))
    draft_res = build_draft_bundle([draft_src], tmp_path / 'no_contracts', tmp_path / 'draft_out')
    assert draft_res['line_counts']['common'] >= 1
    assert (tmp_path / 'draft_out' / 'REVIEW_CHECKLIST.md').is_file()
    maas_draft_txt = (tmp_path / 'draft_out' / 'maas_indicators.draft.json').read_text(encoding='utf-8')
    assert '46000' not in maas_draft_txt
    assert '[待人工填报：' in maas_draft_txt


# ── 2. 老库 composer_skills.biz_line 平滑加列与 2 套新内置预设自动补种 ─────────────

def test_e41_builtin_skills_upgrade_seeding_and_schema_migration(tmp_path: Path):
    assert len(BUILTIN_WRITING_SKILLS) == 5
    db_file = tmp_path / 'legacy_skills.db'

    # 构造一张缺少 biz_line 列、仅有 E38 旧 3 套预设的老库表
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        '''
        CREATE TABLE composer_skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL,
            user_id INTEGER,
            name VARCHAR(128) NOT NULL,
            description VARCHAR(512) NOT NULL DEFAULT '',
            source_type VARCHAR(32) NOT NULL DEFAULT 'custom',
            source_url VARCHAR(512),
            writing_instructions TEXT NOT NULL DEFAULT '',
            layout_spec_json JSON NOT NULL DEFAULT '{}',
            is_builtin BOOLEAN NOT NULL DEFAULT 0,
            is_default BOOLEAN NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        '''
    )
    for idx, old_item in enumerate(BUILTIN_WRITING_SKILLS[:3]):
        conn.execute(
            '''
            INSERT INTO composer_skills (tenant_id, name, description, source_type, writing_instructions, layout_spec_json, is_builtin, is_default)
            VALUES (?, ?, ?, 'builtin', ?, ?, 1, ?)
            ''',
            (
                1,
                old_item['name'],
                old_item['description'],
                old_item['writing_instructions'],
                json.dumps(old_item['layout_spec'], ensure_ascii=False),
                1 if idx == 1 else 0,  # 模拟租户把第 2 个设为默认
            ),
        )
    conn.commit()
    conn.close()

    engine = create_engine(f"sqlite:///{db_file.as_posix()}", connect_args={'check_same_thread': False})
    added = ensure_composer_skills_columns(engine)
    assert 'biz_line' in added
    cols = {c['name'] for c in inspect(engine).get_columns('composer_skills')}
    assert 'biz_line' in cols

    from sqlalchemy.orm import sessionmaker
    Session = sessionmaker(bind=engine)
    sess = Session()
    try:
        rows = ensure_builtin_composer_skills(sess, 1)
        assert len(rows) == 5
        by_name = {r.name: r for r in rows}
        assert by_name['网络安全与数据安全专精版'].biz_line == 'security'
        assert by_name['并行MaaS算力服务专精版'].biz_line == 'maas'
        # 不覆盖租户原本选择的默认 Skill
        assert by_name['技术架构深度攻坚版'].is_default is True
        assert by_name['网络安全与数据安全专精版'].is_default is False
        # 幂等再次调用不重复插行
        rows2 = ensure_builtin_composer_skills(sess, 1)
        assert len(rows2) == 5
    finally:
        sess.close()

    # Skill Markdown frontmatter 带 biz_line 解析兼容
    parsed_sk = parse_skill_markdown_or_json(
        '---\nname: 政采安全测试\nbiz_line: security\ntone: technical_deep\n---\n严格响应隔离与审计要求。\n'
    )
    assert parsed_sk['biz_line'] == 'security'


# ── 3. API：/indicators 预览与 /generate（含非 V2 模式下的指标库接线）───────────────

def test_e41_api_indicators_preview_and_generate_non_v2_wiring(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'api_e41.db'
    db_url = f"sqlite:///{db_file.as_posix()}"

    import backend.database as db_mod
    from sqlalchemy.orm import sessionmaker

    new_engine = create_engine(db_url, connect_args={'check_same_thread': False})
    from backend.models import apply_sqlite_pragmas
    apply_sqlite_pragmas(new_engine)
    Base.metadata.create_all(bind=new_engine)
    NewSession = sessionmaker(autocommit=False, autoflush=False, bind=new_engine)

    monkeypatch.setattr(db_mod, 'engine', new_engine)
    monkeypatch.setattr(db_mod, 'SessionLocal', NewSession)
    db_mod.init_db()

    tender_doc = tmp_path / 'mini_maas_tender.docx'
    d = Document()
    d.add_paragraph('项目名称：智算云 MaaS 平台接入与运维采购项目')
    d.add_paragraph('★1. 须提供 7×24 小时售后服务电话，故障响应时间不超过 1 小时。')
    d.add_paragraph('评分细则（技术部分 60 分）：')
    d.add_paragraph('1. 多模型统一网关接入与 DeepSeek/GLM 大模型调度能力（25 分）：支持线上大模型与本地模型切换及知识库 RAG 对接。')
    d.add_paragraph('2. 售后服务方案与故障应急 SLA（15 分）：明确响应时间、到达现场时间与备品备件保障。')
    d.save(str(tender_doc))

    app = create_app()
    with TestClient(app) as tc:
        headers = _login_headers(tc)

        # 3a) /api/composer/skills 含 5 套内置（含 2 套业务线专精）
        r_sk = tc.get('/api/composer/skills', headers=headers)
        assert r_sk.status_code == 200
        skills = r_sk.json()['items']
        assert len(skills) >= 5
        maas_sk = next(s for s in skills if s.get('biz_line') == 'maas')
        gen_sk = next(s for s in skills if s.get('biz_line') == 'general' and s.get('is_default'))

        # 3b) /api/composer/indicators 透出评分标准参考与标准应答
        r_ind_maas = tc.get('/api/composer/indicators', headers=headers, params={'biz_line': 'maas'})
        assert r_ind_maas.status_code == 200
        ind_body = r_ind_maas.json()
        assert ind_body['biz_line'] == 'maas'
        assert len(ind_body['line_scoring_summary']) >= 3
        assert ind_body['total_indicators'] >= 10
        assert any(it['indicator_id'] == 'maas_multi_model_gateway' for it in ind_body['indicators'])

        # 3c) 非 V2 骨架模式 (compose_mode='skeleton_v1') + general + use_indicator_library='false' -> 不注入指标库
        r_off = tc.post(
            '/api/composer/generate',
            headers=headers,
            files={'tender_file': ('mini_maas_tender.docx', tender_doc.read_bytes(), 'application/octet-stream')},
            data={
                'compose_mode': 'skeleton_v1',
                'skill_id': str(gen_sk['id']),
                'use_ai': 'false',
                'use_indicator_library': 'false',
                'async_run': 'false',
            },
        )
        assert r_off.status_code == 200, r_off.text
        off_body = r_off.json()
        assert off_body['use_indicator_library'] is False
        assert off_body['stats']['use_indicator_library'] is False
        assert off_body['scoring_fill_mode'] == 'outline_stub'

        # 3d) 非 V2 骨架模式 (compose_mode='skeleton_v1') + MaaS Skill -> 必须生效并注入公司标准指标片段！
        r_on = tc.post(
            '/api/composer/generate',
            headers=headers,
            files={'tender_file': ('mini_maas_tender.docx', tender_doc.read_bytes(), 'application/octet-stream')},
            data={
                'compose_mode': 'skeleton_v1',
                'skill_id': str(maas_sk['id']),
                'use_ai': 'false',
                'use_indicator_library': 'true',
                'async_run': 'false',
            },
        )
        assert r_on.status_code == 200, r_on.text
        on_body = r_on.json()
        assert on_body['biz_line'] == 'maas'
        assert on_body['use_indicator_library'] is True
        assert on_body['stats']['indicator_matched_topics'] >= 1
        assert on_body['scoring_fill_mode'] == 'rag_knowledge'
        sec_scoring = next(s for s in on_body['blueprint']['sections'] if s['section_id'] == 'sec_scoring')
        all_notices = [
            sn.get('notice', '')
            for blk in sec_scoring['content_blocks']
            for sn in (blk.get('reference_snippets') or [])
        ]
        assert any('公司标准指标应答参考' in n for n in all_notices)

        for did in (off_body['id'], on_body['id']):
            assert tc.delete(f"/api/composer/drafts/{did}", headers=headers).status_code == 200


# ── 4. 实弹探针：安易 04 包（MaaS 线）+ 安易 02 包（安全线）端到端验证 ─────────────

def test_e41_live_probe_04_maas_and_02_security(tmp_path: Path):
    tender_04 = Path(r'E:\agent\2.新agent数据法律\标记版本（发售版）04包-北京交通大学数据库稳定性运行测试与优化 _招标文件10.23 (1).pdf')
    tender_02 = Path(r'E:\agent\2.新agent数据法律\（发售版）02包-北京交通大学数据库稳定性运行测试与优化 _招标文件10.23.pdf')
    if not tender_04.is_file() or not tender_02.is_file():
        pytest.skip('真实 02/04 包招标 PDF 不在本地测试路径')

    # Mock LLM 客户端：直接模拟大模型消化指标库参考片段后输出正文，验证端到端注入、渲染与无造假价格锁
    class _EchoIndicatorLLM:
        def chat(self, messages, model_tier='flash', **kwargs):
            prompt = str(messages[-1].get('content') or '')
            if '"chapters"' in prompt:
                payload = {
                    'chapters': [
                        {
                            'chapter_no': '第一章',
                            'title': '平台架构、核心指标响应与保障方案',
                            'chapter_kind': 'technical',
                            'focus_topic_ids': [f"topic_{i}" for i in range(1, 19)],
                            'writing_brief': '围绕技术指标、业务线专精口径及售后 SLA 展开响应',
                            'recommended_table': '关键指标响应对照表',
                        }
                    ]
                }
            elif '"responses"' in prompt:
                payload = {'responses': []}
            else:
                has_maas = ('并行' in prompt and 'Token' in prompt)
                has_sec = ('隔离' in prompt and '磁盘' in prompt)
                if has_maas:
                    para = (
                        '依托并行 MaaS 平台（AI 智算云平台），我方构建支持 DeepSeek/GLM 等多系列大模型统一接入、'
                        'Tokens 输入/输出/上下文缓存精确计量（具体单价 [待人工填报：Token 单价 ___ ￥/M Tokens]），'
                        '并支持敏感数据本地私有化不出域部署与 7×24 小时服务电话、1 小时故障响应、24 小时到场。'
                    )
                elif has_sec:
                    para = (
                        '围绕安全流转核心需求，我方采用普通与安全工作环境双域隔离、防另存/截屏审计、'
                        '逻辑隔离磁盘防木马控制（禁止虚拟机及远程桌面登录）及专用磁盘加密防勒索与数据不落地灾备；'
                        '具体等保备案编号及国密支持情况保留 [待人工确认：等保/商密证书编号]。'
                    )
                else:
                    para = '按标准指标提供完整响应与运维保障。'
                payload = {
                    'paragraphs': [para],
                    'table': {'title': '核心指标承诺矩阵', 'headers': ['指标项', '我方响应口径'], 'rows': [['运维响应', '7×24 电话 / ≤1 小时响应']]},
                    'needs_manual': ['人工核实项目具体参数槽位'],
                }
            return SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))

    maas_skill = next(s for s in BUILTIN_WRITING_SKILLS if s.get('biz_line') == 'maas')
    sec_skill = next(s for s in BUILTIN_WRITING_SKILLS if s.get('biz_line') == 'security')

    bp_04 = compose_bid_blueprint(
        tender_04,
        compose_mode='skeleton_v1',
        knowledge_provider=IndicatorLibraryProvider('maas'),
        client=_EchoIndicatorLLM(),
        skill=maas_skill,
        use_ai=True,
    )
    d_04 = bp_04.to_dict()
    assert d_04['ai_status'] == 'done'
    assert d_04['stats']['use_indicator_library'] is True
    assert d_04['stats']['indicator_matched_topics'] >= 3
    out_04 = tmp_path / 'probe_04_maas.docx'
    export_composed_bid_docx(d_04, out_04)
    doc_04_txt = '\n'.join(p.text for p in Document(str(out_04)).paragraphs)
    assert '并行 MaaS' in doc_04_txt
    assert '[待人工填报：Token 单价' in doc_04_txt

    bp_02 = compose_bid_blueprint(
        tender_02,
        compose_mode='skeleton_v1',
        knowledge_provider=IndicatorLibraryProvider('security'),
        client=_EchoIndicatorLLM(),
        skill=sec_skill,
        use_ai=True,
    )
    d_02 = bp_02.to_dict()
    assert d_02['ai_status'] == 'done'
    assert d_02['stats']['use_indicator_library'] is True
    assert d_02['stats']['indicator_matched_topics'] >= 3
    out_02 = tmp_path / 'probe_02_sec.docx'
    export_composed_bid_docx(d_02, out_02)
    doc_02_txt = '\n'.join(p.text for p in Document(str(out_02)).paragraphs)
    assert '双域隔离' in doc_02_txt and '逻辑隔离磁盘' in doc_02_txt
    assert '[待人工确认：等保/商密证书编号]' in doc_02_txt
