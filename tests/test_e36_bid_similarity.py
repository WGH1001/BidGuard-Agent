# -*- coding: utf-8 -*-
"""E36 · 多标书交叉比对与相似度风险分析全链路验收测试。

覆盖 E36 §4 全部验收标准：
1. 剔除招标公共文本防误报：照抄招标 400 字要求但各自独立应答 -> 未传 tender 时相似度虚高，传 tender 后净相似度 < 15%；
2. 高相似度文本精准捕获与 ≥25 字连续句溯源：精确匹配 quote 与原文偏移，且 quote 为原文逐字连续子串；
3. Word / PDF 元数据（author / lastModifiedBy）与内嵌图片二进制 SHA256 碰撞检出；
4. Decimal 规律性报价检出：4 项以上对齐分项单价保持精确 1.05 倍比例（REGULAR_PRICE_RATIO）及固定差额（REGULAR_PRICE_DIFF）；
5. 跨单位人员/手机/身份证号/合同编号碰撞 + 敏感号脱敏（全链路及导出的 .docx 绝不泄露未脱敏 18 位身份证号或 11 位手机号）；
6. API 全链路集成与租户隔离：<2 份 422、3 家比对、列表/详情回放、导出 .docx、删单级联清理磁盘、跨租户 404；
7. 合规锁验证：绝无自动洗稿改写或伪造作者指纹端点/能力。
"""
from io import BytesIO
import os
from pathlib import Path
import sys
import zipfile

from docx import Document
from fastapi.testclient import TestClient
import pymupdf
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.main import create_app
from core.bid_similarity import analyze_bid_similarity, export_similarity_report_docx, extract_file_fingerprint

PROJECT_ROOT = Path(__file__).resolve().parents[1]

def _make_test_png_bytes() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=120, height=60)
    page.insert_text((10, 35), 'SEAL-IMG', fontsize=14)
    raw = page.get_pixmap(dpi=100).tobytes('png')
    doc.close()
    return raw


_FAKE_PNG_BYTES = _make_test_png_bytes()


def _make_docx_file(
    path: Path,
    paragraphs: list[str],
    *,
    author: str = '',
    last_modified_by: str = '',
    table_rows: list[list[str]] | None = None,
    embed_png: bytes | None = None,
) -> Path:
    doc = Document()
    if author:
        doc.core_properties.author = author
    if last_modified_by:
        doc.core_properties.last_modified_by = last_modified_by
    for para in paragraphs:
        doc.add_paragraph(para)
    if table_rows:
        tbl = doc.add_table(rows=len(table_rows), cols=len(table_rows[0]))
        for r_idx, row in enumerate(table_rows):
            for c_idx, val in enumerate(row):
                tbl.cell(r_idx, c_idx).text = val
    if embed_png:
        from docx.shared import Inches
        doc.add_picture(BytesIO(embed_png), width=Inches(1.0))
    doc.save(str(path))
    return path


def _make_pdf_file(path: Path, text: str, *, author: str = '', embed_png: bytes | None = None) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()
    font_p = Path(r'C:\Windows\Fonts\msyh.ttc')
    if font_p.is_file():
        page.insert_font(fontname='cjk', fontfile=str(font_p))
        page.insert_text((72, 90), text, fontsize=11, fontname='cjk')
    else:
        page.insert_text((72, 90), text, fontsize=11)
    if embed_png:
        page.insert_image(pymupdf.Rect(72, 300, 140, 368), stream=embed_png)
    if author:
        doc.set_metadata({'author': author, 'creator': 'TestPDFGen'})
    doc.save(str(path))
    doc.close()
    return path


def _login_headers(tc: TestClient, username: str, password: str = 'BidGuard2026!') -> dict[str, str]:
    r = tc.post('/api/auth/login', json={'username': username, 'password': password})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


# ── 1 & 2. 招标底稿剥离防误报 + 高雷同捕获与连续句溯源 ──────────────────────────

def test_e36_tender_boilerplate_subtraction_and_high_similarity_passages(tmp_path: Path):
    tender_clause = (
        '★系统必须支持分布式高可用集群部署，确保单节点故障时核心业务在三十秒内自动切换恢复，'
        '支持国产麒麟高级服务器操作系统与达梦数据库，并提供五年原厂现场质保及七乘二十四小时应急响应服务。'
        '★投标人须具备电子与智能化工程专业承包一级资质及信息安全管理体系认证证书，'
        '项目实施期间需派驻不少于五名持有高级职称或信息系统项目管理师证书的专职技术工程师现场驻点运维，'
        '每季度提交一次完整系统巡检与安全加固报告并配合业主完成年度等保三级测评验收工作。'
        '★全部软硬件设备须具备合法知识产权及原厂售后服务承诺函，报价不得超出采购预算上限且须明确列出备品备件清单与培训方案。'
    )
    tender_doc = _make_docx_file(tmp_path / 'tender.docx', [tender_clause], author='buyer_org')

    resp_a = (
        '本公司独立技术方案：采用自主研发星云微服务引擎搭配轻量容器编排器，'
        '通过两地三中心异步镜像架构达成业务零感知接管，研发团队驻南京交付中心。'
    )
    resp_b = (
        '我方专属实施设计：依托海岳虚拟化平台及超融合存储阵列实现多租户物理隔离，'
        '运维服务站设立于合肥高新区，按月开展漏洞基线核查并出具专项审计日报。'
    )

    bid_a = _make_docx_file(tmp_path / 'bid_a.docx', [tender_clause, resp_a], author='user_a')
    bid_b = _make_docx_file(tmp_path / 'bid_b.docx', [tender_clause, resp_b], author='user_b')

    # 不传招标文件 -> 因同时照抄 260+ 字招标条款，原文相似度显著虚高
    res_no_tender = analyze_bid_similarity([
        {'label': '单位A', 'path': bid_a},
        {'label': '单位B', 'path': bid_b},
    ])
    pair_no = res_no_tender['pair_matrix'][0]
    assert pair_no['raw_similarity_pct'] > 45.0
    assert pair_no['net_similarity_pct'] > 45.0

    # 传入招标文件 -> 公共条款被精准剥离，净正文相似度显著降至 < 15% 低风险
    res_with_tender = analyze_bid_similarity(
        [{'label': '单位A', 'path': bid_a}, {'label': '单位B', 'path': bid_b}],
        tender_path=tender_doc,
    )
    pair_yes = res_with_tender['pair_matrix'][0]
    assert pair_yes['raw_similarity_pct'] > 45.0
    assert pair_yes['net_similarity_pct'] < 15.0
    assert pair_yes['risk_level'] == 'low'

    # 恶意高相似度：剔除招标条款后仍存在大段 ≥120 字完全一致自编方案
    copied_tech = (
        '在系统整体架构设计中，我方核心采集网关采取零拷贝环形缓冲区配合多线程异步持久化引擎，'
        '所有外部接口调用均强制执行双向数字证书国密算法验签与动态令牌防重放校验，'
        '日志审计模块每十五分钟自动计算区块摘要链路哈希并与异地只读归档节点完成增量同步备份。'
    )
    bid_c = _make_docx_file(tmp_path / 'bid_c.docx', [tender_clause, copied_tech], author='user_c')
    bid_d = _make_docx_file(tmp_path / 'bid_d.docx', [tender_clause, copied_tech], author='user_d')

    res_collude = analyze_bid_similarity(
        [{'label': '单位C', 'path': bid_c}, {'label': '单位D', 'path': bid_d}],
        tender_path=tender_doc,
    )
    pair_cd = res_collude['pair_matrix'][0]
    assert pair_cd['net_similarity_pct'] >= 90.0
    assert pair_cd['risk_level'] == 'high'
    assert len(pair_cd['shared_passages']) >= 1
    ps0 = pair_cd['shared_passages'][0]
    assert ps0['norm_chars'] >= 100
    assert '零拷贝环形缓冲区' in ps0['a_quote']
    assert ps0['a_quote'] == ps0['b_quote']


# ── 3. Word/PDF 元数据与内嵌图片 SHA256 碰撞 ─────────────────────────────────

def test_e36_docx_and_pdf_metadata_and_embedded_image_collision(tmp_path: Path):
    d1 = _make_docx_file(
        tmp_path / 'a.docx',
        ['单位甲自主技术响应正文内容，包含方案架构说明与售后服务承诺细则。'],
        author='zhangsan_pc',
        last_modified_by='zhangsan_pc',
        embed_png=_FAKE_PNG_BYTES,
    )
    d2 = _make_docx_file(
        tmp_path / 'b.docx',
        ['单位乙独立项目实施方案，涵盖项目进度安排与应急响应机制部署细节。'],
        author='zhangsan_pc',
        last_modified_by='lisi_pc',
        embed_png=_FAKE_PNG_BYTES,
    )
    res_docx = analyze_bid_similarity([
        {'label': '单位甲', 'path': d1},
        {'label': '单位乙', 'path': d2},
    ])
    codes = {f['code'] for f in res_docx['findings']}
    assert 'SAME_DOC_AUTHOR' in codes
    assert 'SAME_EMBEDDED_IMAGE' in codes
    assert res_docx['risk_level'] == 'high'

    # PDF 元数据与图片哈希碰撞（确保每份 PDF 超 120 字达到文本型 PDF 门槛）
    p1_txt = (
        '投标人一专属技术应答文本内容区域。我方在华北交付中心设立专职项目管理团队，'
        '采取自主研发分布式总线系统完成全部业务模块对接测试，并提供原厂工程师驻场支持，'
        '全面覆盖硬件巡检、数据链路校验、系统故障应急演练及季度运行审计分析报告交付任务。'
    )
    p2_txt = (
        '投标人二不同实施方案编制章节说明。本公司依托西南研发基地既有交付体系执行项目实施，'
        '围绕微服务网关与云原生容器编排架构制定月度阶段里程碑计划，同时配备二十四小时热线技术坐席，'
        '确保核心业务节点具备完善的异地容灾备份机制并严格遵守保密管理条例条款。'
    )
    p1 = _make_pdf_file(tmp_path / 'p1.pdf', p1_txt, author='tender_writer_99', embed_png=_FAKE_PNG_BYTES)
    p2 = _make_pdf_file(tmp_path / 'p2.pdf', p2_txt, author='tender_writer_99', embed_png=_FAKE_PNG_BYTES)
    fp1 = extract_file_fingerprint(p1)
    assert fp1['metadata']['author'] == 'tender_writer_99'
    assert fp1['media_count'] == 1
    res_pdf = analyze_bid_similarity([
        {'label': 'PDF甲', 'path': p1},
        {'label': 'PDF乙', 'path': p2},
    ])
    pdf_codes = {f['code'] for f in res_pdf['findings']}
    assert 'SAME_DOC_AUTHOR' in pdf_codes
    assert 'SAME_EMBEDDED_IMAGE' in pdf_codes


# ── 4. Decimal 规律性报价比例与等差检出 ──────────────────────────────────────

def test_e36_decimal_regular_price_ratio_and_diff(tmp_path: Path):
    # A/B 4 项分项单价精确 1.05 倍比例（Decimal 无 float 误差）
    rows_a = [
        ['序号', '项目名称', '数量', '单价(元)', '合价(元)'],
        ['1', '核心交换机', '1', '10000.00', '10000.00'],
        ['2', '汇聚交换机', '1', '20000.00', '20000.00'],
        ['3', '下一代防火墙', '1', '40000.00', '40000.00'],
        ['4', '运维审计平台', '1', '80000.00', '80000.00'],
    ]
    rows_b = [
        ['序号', '项目名称', '数量', '单价(元)', '合价(元)'],
        ['1', '核心交换机', '1', '10500.00', '10500.00'],
        ['2', '汇聚交换机', '1', '21000.00', '21000.00'],
        ['3', '下一代防火墙', '1', '42000.00', '42000.00'],
        ['4', '运维审计平台', '1', '84000.00', '84000.00'],
    ]
    # C 与 A 每项严格固定加差 +3000.00 元
    rows_c = [
        ['序号', '项目名称', '数量', '单价(元)', '合价(元)'],
        ['1', '核心交换机', '1', '13000.00', '13000.00'],
        ['2', '汇聚交换机', '1', '23000.00', '23000.00'],
        ['3', '下一代防火墙', '1', '43000.00', '43000.00'],
        ['4', '运维审计平台', '1', '83000.00', '83000.00'],
    ]
    da = _make_docx_file(tmp_path / 'pa.docx', ['投标总报价：150000.00 元'], table_rows=rows_a)
    db = _make_docx_file(tmp_path / 'pb.docx', ['投标总报价：157500.00 元'], table_rows=rows_b)
    dc = _make_docx_file(tmp_path / 'pc.docx', ['投标总报价：162000.00 元'], table_rows=rows_c)

    res = analyze_bid_similarity([
        {'label': '甲公司', 'path': da},
        {'label': '乙公司', 'path': db},
        {'label': '丙公司', 'path': dc},
    ])
    codes = {f['code'] for f in res['findings']}
    assert 'REGULAR_PRICE_RATIO' in codes
    assert 'REGULAR_PRICE_DIFF' in codes
    ratio_f = next(f for f in res['findings'] if f['code'] == 'REGULAR_PRICE_RATIO')
    assert '1.05' in ratio_f['detail']['ratio']


# ── 5. 跨单位人员/手机/身份证号碰撞 + 严格前 3 后 4 位脱敏 ───────────────────

def test_e36_entity_collision_strict_pii_masking(tmp_path: Path):
    raw_id = '310104199005121234'
    raw_phone = '13812345678'
    f1 = _make_docx_file(
        tmp_path / 'ent1.docx',
        [
            f'项目经理：王建国\n联系电话：{raw_phone}\n身份证号：{raw_id}\n合同编号：HT-2025-JS-0088\n外部参考：芜湖市某某大数据中心',
        ],
    )
    f2 = _make_docx_file(
        tmp_path / 'ent2.docx',
        [
            f'项目负责人：王建国\n手机：{raw_phone}\n公民身份号码：{raw_id}\n合同编号：HT-2025-JS-0088\n类似项目：芜湖市某某大数据中心',
        ],
    )
    res = analyze_bid_similarity([
        {'label': '单位1', 'path': f1},
        {'label': '单位2', 'path': f2},
    ])
    codes = {f['code'] for f in res['findings']}
    assert 'ENTITY_COLLISION' in codes
    assert 'FINGERPRINT_ERROR_COLLISION' in codes

    serialized = str(res)
    assert raw_id not in serialized
    assert raw_phone not in serialized
    assert '310***********1234' in serialized
    assert '138****5678' in serialized

    out_docx = export_similarity_report_docx(
        {'title': '脱敏检查', **res},
        tmp_path / 'mask_check.docx',
    )
    doc_check = Document(str(out_docx))
    full_docx_text = '\n'.join([p.text for p in doc_check.paragraphs] + [
        c.text for t in doc_check.tables for r in t.rows for c in r.cells
    ])
    assert raw_id not in full_docx_text
    assert raw_phone not in full_docx_text
    assert '310***********1234' in full_docx_text


# ── 6 & 7. API 全链路 + 多租户隔离 + 级联清盘 + 合规锁检查 ───────────────────

def test_e36_api_compare_export_delete_tenant_isolation_and_compliance(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('SEED_DEMO_DATA', '1')
    db_file = tmp_path / 'e36_api.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    tender_p = _make_docx_file(tmp_path / 'api_tender.docx', ['招标公共条款：系统须支持国密加密与多级权限审计。'])
    bid1_p = _make_docx_file(tmp_path / 'api_b1.docx', ['招标公共条款：系统须支持国密加密与多级权限审计。\n一厂独立技术实施方案。'], author='shared_writer')
    bid2_p = _make_docx_file(tmp_path / 'api_b2.docx', ['招标公共条款：系统须支持国密加密与多级权限审计。\n二厂自主研发技术响应。'], author='shared_writer')
    bid3_p = _make_docx_file(tmp_path / 'api_b3.docx', ['招标公共条款：系统须支持国密加密与多级权限审计。\n三厂系统部署规划细则。'], author='other_writer')

    with TestClient(app) as tc:
        admin_h = _login_headers(tc, 'admin')
        # 另建第 2 个部门用户验证严格租户隔离
        u_b = tc.post(
            '/api/auth/users',
            headers=admin_h,
            json={'username': 'tenant2_user', 'password': 'Password123!', 'tenant_id': 2, 'role': 'member'},
        )
        assert u_b.status_code == 200
        tenant2_h = _login_headers(tc, 'tenant2_user', 'Password123!')

        # <2 份标书必须 422 INVALID_BID_FILE_COUNT
        r_one = tc.post(
            '/api/similarity/compare',
            headers=admin_h,
            files=[('bid_files', ('only_one.docx', bid1_p.read_bytes(), 'application/octet-stream'))],
        )
        assert r_one.status_code == 422
        assert r_one.json()['detail']['code'] == 'INVALID_BID_FILE_COUNT'

        # 3 家对比 + 可选招标书
        r_cmp = tc.post(
            '/api/similarity/compare',
            headers=admin_h,
            data={'title': '三家交叉比对测试', 'labels_json': '["甲单位","乙单位","丙单位"]'},
            files=[
                ('tender_file', ('api_tender.docx', tender_p.read_bytes(), 'application/octet-stream')),
                ('bid_files', ('api_b1.docx', bid1_p.read_bytes(), 'application/octet-stream')),
                ('bid_files', ('api_b2.docx', bid2_p.read_bytes(), 'application/octet-stream')),
                ('bid_files', ('api_b3.docx', bid3_p.read_bytes(), 'application/octet-stream')),
            ],
        )
        assert r_cmp.status_code == 200, r_cmp.text
        detail = r_cmp.json()
        rid = detail['id']
        assert detail['bidder_count'] == 3
        assert len(detail['pair_matrix']) == 3
        assert detail['risk_level'] == 'high'  # SAME_DOC_AUTHOR = 'shared_writer'

        # 列表回放 + 详情查看
        r_list = tc.get('/api/similarity', headers=admin_h)
        assert r_list.status_code == 200
        assert r_list.json()['total'] == 1
        assert tc.get(f'/api/similarity/{rid}', headers=admin_h).status_code == 200

        # 跨租户访问、导出与删除严格返回 404
        assert tc.get('/api/similarity', headers=tenant2_h).json()['total'] == 0
        assert tc.get(f'/api/similarity/{rid}', headers=tenant2_h).status_code == 404
        assert tc.get(f'/api/similarity/{rid}/export', headers=tenant2_h).status_code == 404
        assert tc.delete(f'/api/similarity/{rid}', headers=tenant2_h).status_code == 404

        # 导出 .docx 报告成功
        r_exp = tc.get(f'/api/similarity/{rid}/export', headers=admin_h)
        assert r_exp.status_code == 200
        assert zipfile.is_zipfile(BytesIO(r_exp.content))

        # 确认磁盘上传目录存在，删除后级联清盘
        up_dir = PROJECT_ROOT / 'data' / 'uploads' / 'similarity' / '1' / str(rid)
        assert up_dir.is_dir()
        r_del = tc.delete(f'/api/similarity/{rid}', headers=admin_h)
        assert r_del.status_code == 200
        assert not up_dir.exists()
        assert tc.get('/api/similarity', headers=admin_h).json()['total'] == 0

        # 合规边界验证：OpenAPI 路由中不存在洗稿/改写/指纹伪造端点
        route_paths = {r.path for r in app.routes if hasattr(r, 'path')}
        for p in route_paths:
            assert 'wash' not in p and 'spoof' not in p and 'rewrite' not in p

        # 确保不影响大盘统计（BidCheckTask 未被污染）
        dash = tc.get('/api/dashboard', headers=admin_h).json()
        assert dash['total_projects'] == 0
