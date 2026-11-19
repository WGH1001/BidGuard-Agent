# -*- coding: utf-8 -*-
"""E44 · E42 评分预估精度精修、Rerank 向量通道与 F2 跨处总价校验回归测试。"""
from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

from docx import Document
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import auth as auth_mod
from backend.auth import create_token, hash_password
from backend.database import get_db
from backend.main import app
from backend.models import Base, Tenant, User, UserModelConfig, ensure_user_model_configs_e44_columns
from backend.routers import model_config as model_config_router
from backend.worker import resolve_effective_llm_client
from core.bid_scorer import (
    _extract_docx_table_blocks,
    _retrieve_verified_local_quotes,
    collect_scoring_criteria_items,
    estimate_bid_score,
)
from core import hybrid_retriever as hybrid_retriever_mod
from core.hybrid_retriever import get_hybrid_stats, reset_hybrid_stats
from core.interfaces import ParsedDoc
from core.price_extractor import run_price_check


def _make_docx(
    path: Path,
    paragraphs: list[str],
    tables: list[list[list[str]]] | None = None,
    *,
    merge_opening_header: bool = False,
) -> Path:
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    for ti, rows in enumerate(tables or []):
        if not rows:
            continue
        tb = doc.add_table(rows=len(rows), cols=len(rows[0]))
        for ri, rvals in enumerate(rows):
            for ci, val in enumerate(rvals):
                tb.cell(ri, ci).text = val
        if merge_opening_header and ti == 0 and len(rows[0]) >= 3:
            tb.cell(0, 1).merge(tb.cell(0, 2))
            tb.cell(0, 1).text = rows[0][1]
    doc.save(str(path))
    return path


def _make_valid_cert_png_bytes() -> bytes:
    """构造满足 E1 双重尺寸过滤（>20KB、>=200x200）的合法 PNG 字节流，用于 .media.ocr.json 缓存测试。"""
    import struct
    import zlib

    width, height = 240, 240
    raw_rows = bytearray()
    for y in range(height):
        raw_rows.append(0)  # filter type 0
        for x in range(width):
            # 确定性伪随机纹理，保证压缩后稳定 > 25KB
            v = ((x * 73 + y * 137 + (x ^ y) * 29) * 31) & 0xFF
            raw_rows.append(v)
            raw_rows.append((v ^ 0x5A) & 0xFF)
            raw_rows.append(((x * 19 + y * 43) ^ v) & 0xFF)

    def _chunk(ctype: bytes, data: bytes) -> bytes:
        return struct.pack('>I', len(data)) + ctype + data + struct.pack('>I', zlib.crc32(ctype + data) & 0xFFFFFFFF)

    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(raw_rows), level=1)
    return b'\x89PNG\r\n\x1a\n' + _chunk(b'IHDR', ihdr) + _chunk(b'IDAT', idat) + _chunk(b'IEND', b'')


def test_e44_rerank_client_passthrough_and_silent_l1_fallback(tmp_path: Path, monkeypatch):
    """验收 1：bid_scorer 透传 client 激活 L2 向量精排；无 embedding 模型时 0 次调用静默降级 L1 BM25。"""
    monkeypatch.setattr(hybrid_retriever_mod, '_CACHE_DIR', tmp_path / 'emb_cache')
    tender_doc = ParsedDoc(
        text=(
            "第五章 评分办法及评分标准\n"
            "总分100分\n"
            "价格\n（30 分）\n有效价格得分=（评标基准价/评标价格）×30\n"
            "技术方案（70 分）\n"
            "实施方案（40 分）：方案内容完整详细、量化可控得40分。\n"
            "售后服务方案（30 分）：服务体系、应急响应与到场时间明确得30分。\n"
        ),
        source_type='txt',
        pages=1,
    )
    bid_doc = ParsedDoc(
        text=(
            "第一章 实施方案\n"
            "我方将分调研、架构设计、系统部署、联调验收四个阶段推进项目实施，明确各阶段里程碑与交付物。\n"
            "第二章 售后服务方案\n"
            "我方提供7×24小时技术支持热线，一级故障1小时响应，重大故障8小时到场，并配备专职应急预案团队。\n"
        ),
        source_type='txt',
        pages=1,
    )
    t_path = tmp_path / 'tender.txt'
    b_path = tmp_path / 'bid.txt'
    t_path.write_text(tender_doc.text, encoding='utf-8', newline='\n')
    b_path.write_text(bid_doc.text, encoding='utf-8', newline='\n')

    # (A) 配置了 embedding 模型 -> 透传 client 激活 L2 向量精排
    embed_calls: list[int] = []

    class _MockEmbedClient:
        models = {'flash': 'mock-flash', 'embedding': 'bge-test-e44'}

        def embed(self, texts):
            embed_calls.append(len(texts))
            return [[0.6, 0.8, 0.1, 0.2] for _ in texts]

        def chat(self, messages, model_tier='flash', **kwargs):
            return SimpleNamespace(
                content=json.dumps(
                    {
                        'estimated_score': 35,
                        'deduction_reasons': ['方案覆盖核心阶段'],
                        'improvement_suggestions': ['补充甘特图'],
                        'evidence_quotes': ['我方将分调研、架构设计、系统部署、联调验收四个阶段推进项目实施'],
                    },
                    ensure_ascii=False,
                )
            )

    reset_hybrid_stats()
    res_l2 = estimate_bid_score(
        t_path,
        b_path,
        client=_MockEmbedClient(),
        tender_parsed_doc=tender_doc,
        bid_parsed_doc=bid_doc,
        f2_res={'status': 'OK', 'findings': []},
    )
    assert res_l2['status'] == 'completed'
    assert get_hybrid_stats()['level2_calls'] >= 1
    assert len(embed_calls) >= 1

    # (B) 未配置 embedding 键 -> 严禁调用 embed()，零报错静默回退 Level 1 BM25
    class _MockNoEmbedClient:
        models = {'flash': 'mock-flash'}

        def embed(self, texts):
            raise AssertionError('无 embedding 配置时绝不应发起 embed 请求')

        def chat(self, messages, model_tier='flash', **kwargs):
            return SimpleNamespace(
                content=json.dumps(
                    {
                        'estimated_score': 30,
                        'deduction_reasons': ['基本满足'],
                        'improvement_suggestions': ['细化交付物'],
                        'evidence_quotes': ['我方提供7×24小时技术支持热线'],
                    },
                    ensure_ascii=False,
                )
            )

    reset_hybrid_stats()
    res_l1 = estimate_bid_score(
        t_path,
        b_path,
        client=_MockNoEmbedClient(),
        tender_parsed_doc=tender_doc,
        bid_parsed_doc=bid_doc,
        f2_res={'status': 'OK', 'findings': []},
    )
    assert res_l1['status'] == 'completed'
    assert get_hybrid_stats()['level2_calls'] == 0


def test_e44_model_embedding_config_api_migration_and_worker_injection(monkeypatch):
    """验收 2：model_embedding 老库平滑加列（14表不变）、API 读写、非阻断 /test 探活与 worker 注入。"""
    assert len(Base.metadata.tables) == 14

    # 1. 验证老库缺 model_embedding 列时平滑补齐
    legacy_engine = create_engine('sqlite:///:memory:')
    with legacy_engine.begin() as conn:
        conn.execute(
            text(
                'CREATE TABLE user_model_configs ('
                'id INTEGER PRIMARY KEY AUTOINCREMENT, '
                'user_id INTEGER NOT NULL UNIQUE, '
                'base_url VARCHAR(256) NOT NULL, '
                'api_key_encrypted TEXT NOT NULL, '
                'model_flash VARCHAR(128) NOT NULL, '
                'model_flagship VARCHAR(128), '
                'supports_reasoning_effort BOOLEAN NOT NULL DEFAULT 0, '
                'supports_vision BOOLEAN NOT NULL DEFAULT 0'
                ')'
            )
        )
    added = ensure_user_model_configs_e44_columns(legacy_engine)
    assert added == ['model_embedding']
    assert ensure_user_model_configs_e44_columns(legacy_engine) == []

    # 2. 端到端 API 与 resolve_effective_llm_client 验证
    engine = create_engine(
        'sqlite:///:memory:',
        connect_args={'check_same_thread': False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    db = SessionLocal()
    try:
        tenant = Tenant(name='E44测试部门', is_active=True)
        db.add(tenant)
        db.flush()
        user = User(
            tenant_id=tenant.id,
            username='e44_user',
            password_hash=hash_password('Pass1234!'),
            display_name='E44测试员',
            role='admin',
            is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        uid = int(user.id)
        token = create_token(
            uid,
            tenant.id,
            user.role,
            username=user.username,
        )
    finally:
        db.close()

    def _override_db():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    monkeypatch.setenv('STRICT_INNER_MODEL_ONLY', '0')
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[auth_mod._get_db_dep] = _override_db
    try:
        client = TestClient(app)
        headers = {'Authorization': f'Bearer {token}'}

        # PUT 保存含 model_embedding
        r_save = client.put(
            '/api/model-config',
            headers=headers,
            json={
                'base_url': 'http://10.0.2.81:3000/v1',
                'api_key': 'sk-e44-secret-9988',
                'model_flash': 'GLM-5.3-Flash',
                'model_flagship': 'GLM-5.3-Flash',
                'model_embedding': 'BAAI/bge-m3',
                'supports_reasoning_effort': True,
                'supports_vision': False,
            },
        )
        assert r_save.status_code == 200, r_save.text
        data_save = r_save.json()
        assert data_save['model_embedding'] == 'BAAI/bge-m3'
        assert data_save['api_key_last4'] == '9988'

        # 仅修改 model_embedding（不重填 api_key）
        r_upd = client.put(
            '/api/model-config',
            headers=headers,
            json={
                'base_url': 'http://10.0.2.81:3000/v1',
                'model_flash': 'GLM-5.3-Flash',
                'model_flagship': 'GLM-5.3-Flash',
                'model_embedding': 'embedding-3',
                'supports_reasoning_effort': True,
                'supports_vision': False,
            },
        )
        assert r_upd.status_code == 200
        assert r_upd.json()['model_embedding'] == 'embedding-3'
        assert r_upd.json()['api_key_last4'] == '9988'

        # GET 回显
        r_get = client.get('/api/model-config', headers=headers)
        assert r_get.status_code == 200
        assert r_get.json()['model_embedding'] == 'embedding-3'

        # resolve_effective_llm_client 注入验证
        s_chk = SessionLocal()
        try:
            eff_client, eff_st = resolve_effective_llm_client(s_chk, uid)
            assert eff_st == 'done'
            assert eff_client is not None
            assert eff_client.models.get('embedding') == 'embedding-3'
        finally:
            s_chk.close()

        # POST /api/model-config/test：embedding 失败不连坐 Chat（ok 仍为 True，embedding_ok=False）
        class _FakeTestClient:
            use_openai = False
            models = {'flash': 'GLM-5.3-Flash', 'embedding': 'bad-emb'}

            def chat(self, *a, **kw):
                return SimpleNamespace(content='OK')

            def embed(self, texts):
                raise RuntimeError('404 Model bad-emb not found')

        monkeypatch.setattr(model_config_router, 'build_client_from_user_config', lambda **kw: _FakeTestClient())
        monkeypatch.setattr(model_config_router, '_fetch_available_models', lambda _c: ['GLM-5.3-Flash'])

        r_test_fail_emb = client.post(
            '/api/model-config/test',
            headers=headers,
            json={
                'base_url': 'http://10.0.2.81:3000/v1',
                'model_flash': 'GLM-5.3-Flash',
                'model_flagship': 'GLM-5.3-Flash',
                'model_embedding': 'bad-emb',
                'supports_reasoning_effort': True,
            },
        )
        assert r_test_fail_emb.status_code == 200
        body_fail = r_test_fail_emb.json()
        assert body_fail['ok'] is True
        assert body_fail['embedding_ok'] is False
        assert 'BM25' in (body_fail['embedding_message'] or '')

        # POST /api/model-config/test：embedding 成功时 embedding_ok=True
        class _FakeTestClientOk(_FakeTestClient):
            def embed(self, texts):
                return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

        monkeypatch.setattr(model_config_router, 'build_client_from_user_config', lambda **kw: _FakeTestClientOk())
        r_test_ok = client.post(
            '/api/model-config/test',
            headers=headers,
            json={
                'base_url': 'http://10.0.2.81:3000/v1',
                'model_flash': 'GLM-5.3-Flash',
                'model_flagship': 'GLM-5.3-Flash',
                'model_embedding': 'BAAI/bge-m3',
                'supports_reasoning_effort': True,
            },
        )
        assert r_test_ok.status_code == 200
        body_ok = r_test_ok.json()
        assert body_ok['ok'] is True
        assert body_ok['embedding_ok'] is True
        assert '维度 4' in (body_ok['embedding_message'] or '')
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(auth_mod._get_db_dep, None)


def test_e44_f2_bid_letter_total_mismatch_and_no_false_positive(tmp_path: Path):
    """验收 3：F2 跨处总价不一致（投标函 140万 vs 开标一览表 139.5万）精准预警；一致时零误报。"""
    tender_txt = tmp_path / 'tender_02.txt'
    tender_txt.write_text('总预算金额：400万元。其中：02包分包预算：140万元。\n', encoding='utf-8', newline='\n')

    # 1. 不一致场景：投标函 1400000 vs 开标一览表 1395000（复刻真实 02 包合并表头开标一览表）
    bid_mismatch = _make_docx(
        tmp_path / 'bid_mismatch_02.docx',
        paragraphs=[
            '1 投标函',
            '1）所附投标价格表中规定的应提交和交付的投标总价为人民币（壹佰肆拾万1400000）。',
            '2）以转账形式出具的投标保证金，金额为人民币（28000元）。',
            '2 开标一览表',
        ],
        tables=[
            [
                ['投标人名称', '投标总价（大写/小写，单位：元）', ''],
                ['测试公司', '人民币小写：', '1395000'],
                ['测试公司', '人民币大写：', '壹佰叁拾玖万伍仟元整'],
            ],
            [
                ['序号', '品目名称', '数量', '单价（元）', '合价（元）'],
                ['1', '数据安全流转平台', '1套', '1395000', '1395000'],
                ['合计', '', '', '', '1395000'],
            ],
        ],
        merge_opening_header=True,
    )
    res_mm = run_price_check(bid_mismatch, tender_txt)
    assert res_mm['status'] == 'OK'
    assert res_mm['bid_total'] == '1395000.00'
    codes_mm = [f['code'] for f in res_mm['findings']]
    assert 'BID_LETTER_TOTAL_MISMATCH' in codes_mm
    mm_finding = next(f for f in res_mm['findings'] if f['code'] == 'BID_LETTER_TOTAL_MISMATCH')
    assert mm_finding['severity'] == 'warning'
    assert '1400000.00' in mm_finding['message'] and '1395000.00' in mm_finding['message']
    assert '5000.00' in mm_finding['message']

    # 2. 一致场景：投标函 470000 vs 开标一览表 470000（同时含招标限价 50万元 干扰句）
    tender_04 = tmp_path / 'tender_04.txt'
    tender_04.write_text('04包分包预算：50万元。最高限价：50万元。\n', encoding='utf-8', newline='\n')
    bid_ok = _make_docx(
        tmp_path / 'bid_ok_04.docx',
        paragraphs=[
            '1 投标函',
            '本项目最高限价为50万元，投标报价不得超过最高限价。',
            '1）所附投标价格表中规定的应提交和交付的投标总价为人民币（肆拾柒万元470000）。',
            '2 开标一览表',
        ],
        tables=[
            [
                ['投标人名称', '投标总价（大写/小写，单位：元）', ''],
                ['测试公司', '人民币小写：', '470000'],
                ['测试公司', '人民币大写：', '肆拾柒万元整'],
            ],
            [
                ['序号', '品目名称', '数量', '单价（元）', '合价（元）'],
                ['1', '工业物性数据平台', '1套', '470000', '470000'],
                ['合计', '', '', '', '470000'],
            ],
        ],
        merge_opening_header=True,
    )
    res_ok = run_price_check(bid_ok, tender_04)
    assert res_ok['status'] == 'OK'
    assert 'BID_LETTER_TOTAL_MISMATCH' not in [f['code'] for f in res_ok['findings']]


def test_e44_toc_exclusion_section_table_aggregation_ocr_cache_and_dirty_factor_clean(tmp_path: Path):
    """验收 4：PDF 价格分 30 分补全 + 脏前缀清洗 + 目录行过滤 + 整表聚合 + .media.ocr.json 缓存识别 ISO9001。"""
    # 1. 招标评分细则文本（复刻 04/02 包跨行价格分 + 粘连大类与脏右括号前缀 + 三体系 3 分）
    tender_text = (
        "第五章 评分办法及评分标准\n"
        "评分细则：\n"
        "序号\n评分因素\n评价指标和分值\n"
        "1\n价格\n（30 分）\n评标基准价=满足招标文件要求且最低的评标价格\n"
        "合格投标人的有效价格得分=（评标基准价/评标价格)×30\n"
        "2\n技术\n（55 分）\n"
        "实施方案\n0-20 分\n实施方案内容完整详细，可行性高得20分。\n"
        "售后服务方案\n0-20 分\n提供服务体系、故障解决方案、响应时间、应急处理方案得20分。\n"
        "项目进度计划\n0-15 分\n各阶段时间节点清晰、量化可控得15分。\n"
        "3\n商务要求\n（13 分）\n"
        "相关业绩\n0-10 分\n近三年同类项目业绩每提供1个得2分，最高10分。须附合同复印件（包含内容清单、签署页，复印件加盖公章）\n"
        "企业管理体系\n0-3 分\n提供有效期内的质量管理体系认证证书1分；环境管理体系认证证书1分；职业安全健康管理体系认证证书1分；满分3分。\n"
        "4\n政策分\n（2 分）\n"
        "节能环保\n0-2 分\n节能产品认证证书加1分，环境标志产品认证证书加1分。\n"
        "合计100 分\n"
    )
    tender_path = tmp_path / 'tender_e44.txt'
    tender_path.write_text(tender_text, encoding='utf-8', newline='\n')
    collected = collect_scoring_criteria_items(
        tender_path,
        f1_res={
            'scoring_tables': [],
            'scoring_prose': [],
            'scoring_hits': [{'excerpt': tender_text}],
        },
        tender_parsed_doc=ParsedDoc(text=tender_text, source_type='txt', pages=1),
    )
    assert collected['items_sum'] == Decimal('100.00')
    assert collected['score_scale_warning'] is None
    factors = [c['factor'] for c in collected['criteria']]
    assert '价格分' in factors
    assert '企业管理体系' in factors
    assert '相关业绩' in factors
    assert not any('容清单' in f or '商务要求（13' in f or '政策分（2' in f for f in factors)

    # 2. 构造含目录页 + 正文章节 + 业绩表/响应表/进度表 + 内嵌图片（预置 .media.ocr.json ISO9001 缓存）的投标 .docx
    png_path = tmp_path / 'iso9001_cert.png'
    png_bytes = _make_valid_cert_png_bytes()
    assert len(png_bytes) > 20480
    png_path.write_bytes(png_bytes)
    img_sha1 = hashlib.sha1(png_bytes).hexdigest()

    bid_docx = tmp_path / 'bid_e44.docx'
    doc = Document()
    # 目录页噪声（应被过滤）
    doc.add_paragraph('目录')
    doc.add_paragraph('6 业绩案例一览表\t76')
    doc.add_paragraph('8. 实施方案\t150')
    doc.add_paragraph('9. 售后服务方案\t161')
    doc.add_paragraph('10. 项目进度计划\t174')
    # 资格证书章节内嵌扫描图
    doc.add_paragraph('10.2 资格证书')
    doc.add_picture(str(png_path))
    doc.add_paragraph('10.3 业绩证明')
    doc.add_paragraph('10.3.1 工业数据传输安全管理平台的开发技术服务采购协议')
    # 实质正文
    doc.add_paragraph('实施方案')
    doc.add_paragraph('我方针对工业物性数据应用开发平台制定了分阶段实施部署、测试优化与联调验收方案，确保交付可控。')
    doc.add_paragraph('售后服务方案')
    doc.add_paragraph('我方提供7×24小时应急响应服务与备品备件保障，确保系统连续稳定运行。')
    doc.add_paragraph('项目进度计划')
    doc.add_paragraph('注：T为项目启动时间。我方按T+5启动会、T+15调研、T+25部署上线执行里程碑管控。')
    # 内嵌关键表格（业绩一览表、四级故障响应表、进度里程碑表）
    t_perf = doc.add_table(rows=3, cols=4)
    for ri, rvals in enumerate([
        ['序号', '项目名称', '用户名称', '合同金额'],
        ['1', '工业数据传输安全管理平台', '湖南华辰智通科技有限公司', '2642500'],
        ['2', '开发运维一体化智能管理平台', '上海数腾软件科技股份有限公司', '1585200'],
    ]):
        for ci, v in enumerate(rvals):
            t_perf.cell(ri, ci).text = v
    t_sla = doc.add_table(rows=3, cols=4)
    for ri, rvals in enumerate([
        ['故障级别', '故障现象', '响应时间', '执行响应人'],
        ['一级', '系统停机严重影响业务', '1小时', '资深工程师'],
        ['二级', '性能严重降级', '4小时', '资深工程师'],
    ]):
        for ci, v in enumerate(rvals):
            t_sla.cell(ri, ci).text = v
    t_plan = doc.add_table(rows=3, cols=3)
    for ri, rvals in enumerate([
        ['序号', '计划内容', '时间'],
        ['1', '召开项目启动会并同步详细实施计划', 'T+5日内'],
        ['2', '完成系统部署与联调验收', 'T+25日内'],
    ]):
        for ci, v in enumerate(rvals):
            t_plan.cell(ri, ci).text = v
    doc.save(str(bid_docx))

    # 预写与 Stage 1.5 一致的 .media.ocr.json 缓存（验证 bid_scorer 复用缓存识别 ISO9001）
    cache_path = Path(str(bid_docx) + '.media.ocr.json')
    cache_path.write_text(
        json.dumps(
            {
                'images': {
                    img_sha1: {
                        'text': '质量管理体系认证证书 安易科技（北京）有限公司 符合GB/T19001-2016 idt ISO9001:2015 证书有效期至：2026年10月22日',
                        'paragraph_index': 6,
                    }
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
        newline='\n',
    )

    est = estimate_bid_score(
        tender_path,
        bid_docx,
        f1_res={
            'scoring_tables': [],
            'scoring_prose': [],
            'scoring_hits': [{'excerpt': tender_text}],
        },
        f2_res={'status': 'OK', 'findings': []},
        client=None,
        tender_parsed_doc=ParsedDoc(text=tender_text, source_type='txt', pages=1),
    )
    assert est['score_scale_warning'] is None
    assert est['price_pending_max_score'] == '30.00'
    assert est['items_sum_max_score'] == '100.00'

    # 企业管理体系（3分：质量/环境/职业安全健康）应通过 OCR 缓存精准认出 ISO9001 得 1.00 分
    cert_item = next(it for it in est['items'] if it['factor'] == '企业管理体系')
    assert cert_item['estimated_score'] == '1.00'
    assert '质量管理体系' in cert_item['deduction_reasons'][0]
    assert '环境管理体系' in cert_item['deduction_reasons'][0]
    assert any('ISO9001' in q['quote'] or '质量管理体系' in q['quote'] for q in cert_item['evidence_quotes'])

    # 校验结构化检索窗口：不含目录页行 \t150，且召回完整业绩表/响应表/进度表
    from core.parser import parse_document
    bid_parsed = parse_document(str(bid_docx), ocr_images=True)
    tables = _extract_docx_table_blocks(bid_docx)
    wins_perf, _ = _retrieve_verified_local_quotes(
        '相关业绩 同类项目合同', bid_parsed.text, None, factor='相关业绩', category='business', docx_tables=tables
    )
    assert any('湖南华辰智通科技有限公司' in w for w in wins_perf)
    assert not any('\t76' in w or '\t150' in w for w in wins_perf)

    wins_sla, _ = _retrieve_verified_local_quotes(
        '售后服务方案 故障响应时间', bid_parsed.text, None, factor='售后服务方案', category='service', docx_tables=tables
    )
    assert any('故障级别' in w and '1小时' in w for w in wins_sla)

    wins_plan, _ = _retrieve_verified_local_quotes(
        '项目进度计划 里程碑时间节点', bid_parsed.text, None, factor='项目进度计划', category='technical', docx_tables=tables
    )
    assert any('T+5日内' in w and 'T+25日内' in w for w in wins_plan)
