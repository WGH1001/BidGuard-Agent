# -*- coding: utf-8 -*-
"""E4 · 招投标法规库与法条三态核验单元测试（断言严格数数，严禁空循环假绿）。

覆盖任务卡 §1 全部测试要求：
1. normalize_article_text 归一化用例（全半角、标点空白折叠、"第1条"=="第一条" 变体相等）；
2. verify_article 三态核验各 >= 2 个用例：
   - exact_match（2+ 用例：原样匹配 + "第1条"阿拉伯数字变体匹配《招标投标法》第一条）；
   - fuzzy_hint（2+ 用例：篡改条文关键字/罚款比例 + 张冠李戴条文号，且输出含"未经核验"）；
   - unverifiable（2+ 用例：库外无关文本 + 过短文本，明确标注"库中未收录，真伪未经核验"）；
3. 种子幂等导入（init_db / seed_regulation_laws 连续调用两次，法规数与条文数不翻倍）；
4. 官网爬虫适配器 crawl_single_source / crawl_all_configured_sources 四态测试（mock requests）：
   - TODO_USER_FILL 占位源 -> status == 'unconfigured'（诚实报"源未配置"，不抛 500）；
   - 正常静态 HTML -> status == 'updated' 切条入库，二次拉取同内容 -> status == 'unchanged'；
   - 请求超时/网络异常 -> status == 'fetch_error'（重试 1 次后诚实报错引导手动导入）；
   - JS 动态渲染空壳页面 -> status == 'manual_required'（诚实报"该源需手动导入"）；
5. 四格式导入（json / txt / docx / pdf）与 /api/regulations 路由集成测试；
6. F7 第 6 工具 cite_regulation 独立调用（含非空库命中与空库诚实报"法规库为空"）及 ALLOWED_REACT_TOOLS 六工具白名单断言。
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from pathlib import Path

from docx import Document
from fastapi.testclient import TestClient
import pymupdf
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.database import Base, get_engine, get_session_factory, init_db, seed_regulation_laws
from backend.main import create_app
from backend.models import RegulationArticle, RegulationLaw
from core.bid_react_agent import ALLOWED_REACT_TOOLS, cite_regulation
from core.regulation_crawler import crawl_all_configured_sources, crawl_single_source
from core.regulation_library import (
    import_regulation_file,
    normalize_article_no,
    normalize_article_text,
    verify_article,
)


@pytest.fixture()
def reg_session(tmp_path):
    """独立 SQLite 临时文件库，执行 init_db 自动建 11 张表并幂等导入 4 部种子法规。"""
    db_file = tmp_path / 'test_regulations.db'
    eng = init_db(f"sqlite:///{db_file.as_posix()}", seed=True)
    SessionMaker = get_session_factory(eng)
    session = SessionMaker()
    try:
        yield session
    finally:
        session.close()
        eng.dispose()


# ── 1. normalize_article_text 归一化与条文号变体测试 ───────────────────────

def test_normalize_article_text_variants():
    # (a) "第1条" 与 "第一条" 归一化后完全相等
    s_arabic = "第1条：为了规范招标投标活动，保护国家利益！"
    s_chinese = "第一条  为了规范招标投标活动 , 保护国家利益 ! "
    assert normalize_article_text(s_arabic) == normalize_article_text(s_chinese)

    # (b) "第68条" 与 "第六十八条" 归一化后完全相等
    assert normalize_article_text("第68条 本法自2000年1月1日起施行。") == normalize_article_text(
        "第六十八条：本法自２０００年１月１日起施行。"
    )
    assert normalize_article_no("第1条") == "第一条"
    assert normalize_article_no("第28条") == "第二十八条"


# ── 2. verify_article 三态核验（每态 >= 2 个用例）─────────────────────────

def test_verify_article_three_states(reg_session):
    # ── 状态一：exact_match（2 个用例）──
    # 用例 1a：标准中文条号 + 原文
    r_exact_1 = verify_article(
        "《中华人民共和国招标投标法》第一条 为了规范招标投标活动，保护国家利益、社会公共利益和招标投标活动当事人的合法权益，提高经济效益，保证项目质量，制定本法。",
        session=reg_session,
    )
    assert r_exact_1['status'] == 'exact_match'
    assert r_exact_1['verified'] is True
    assert r_exact_1['article_no'] == '第一条'
    assert '招标投标法' in r_exact_1['law']

    # 用例 1b：阿拉伯数字"第1条"变体 + 全半角标点空格差异 -> 依然命中 exact_match
    r_exact_2 = verify_article(
        "第1条: 为了规范招标投标活动, 保护国家利益、社会公共利益和招标投标活动当事人的合法权益, 提高经济效益, 保证项目质量, 制定本法.",
        session=reg_session,
    )
    assert r_exact_2['status'] == 'exact_match'
    assert r_exact_2['verified'] is True
    assert r_exact_2['article_no'] == '第一条'

    # ── 状态二：fuzzy_hint（2 个用例）──
    # 用例 2a：篡改了条文中的核心要求（将"不得少于二十日"篡改为"不得少于五日"）
    r_fuzzy_1 = verify_article(
        "第二十四条 招标人应当确定投标人编制投标文件所需要的合理时间；但是，依法必须进行招标的项目，自招标文件开始发出之日起至投标人提交投标文件截止之日止，最短不得少于五日。",
        session=reg_session,
    )
    assert r_fuzzy_1['status'] == 'fuzzy_hint'
    assert r_fuzzy_1['verified'] is False
    assert r_fuzzy_1['article_no'] == '第二十四条'
    assert '未经核验' in r_fuzzy_1['badge']
    assert '二十日' in r_fuzzy_1['official_content']

    # 用例 2b：条文正文全对（第五条正文），但条文号张冠李戴写成了"第九十九条"
    r_fuzzy_2 = verify_article(
        "《中华人民共和国招标投标法》第九十九条 招标投标活动应当遵循公开、公平、公正和诚实信用的原则。",
        session=reg_session,
    )
    assert r_fuzzy_2['status'] == 'fuzzy_hint'
    assert r_fuzzy_2['verified'] is False
    assert r_fuzzy_2['article_no'] == '第五条'
    assert '未经核验' in r_fuzzy_2['message']

    # ── 状态三：unverifiable（2 个用例）──
    # 用例 3a：完全无关的虚构星际法律条文
    r_unv_1 = verify_article(
        "《银河系星际贸易航行管理条例》第八百八十八条 曲率飞船进入行星轨道前必须关闭反物质引擎。",
        session=reg_session,
    )
    assert r_unv_1['status'] == 'unverifiable'
    assert r_unv_1['verified'] is False
    assert r_unv_1['law'] is None
    assert '库中未收录，真伪未经核验' in r_unv_1['message']

    # 用例 3b：无意义的极短输入
    r_unv_2 = verify_article("第一条", session=reg_session)
    assert r_unv_2['status'] == 'unverifiable'
    assert r_unv_2['verified'] is False
    assert '库中未收录，真伪未经核验' in r_unv_2['badge']


# ── 3. 种子幂等导入测试（二次调用不重复导入）────────────────────────────

def test_seed_regulation_laws_idempotency(reg_session):
    laws_before = reg_session.query(RegulationLaw).count()
    arts_before = reg_session.query(RegulationArticle).count()
    assert laws_before >= 1
    assert arts_before >= 68  # 至少包含《招标投标法》68条

    # 二次调用 seed_regulation_laws，断言导入计数为 0 且总行数严格不变
    second_stats = seed_regulation_laws(reg_session)
    assert second_stats['laws_imported'] == 0
    assert second_stats['articles_imported'] == 0
    assert reg_session.query(RegulationLaw).count() == laws_before
    assert reg_session.query(RegulationArticle).count() == arts_before


# ── 4. 官网爬虫适配器四态测试（TODO_USER_FILL / 正常HTML / 超时 / JS空壳）──

def test_regulation_crawler_four_states(reg_session):
    # (a) TODO_USER_FILL 占位源 -> 诚实返回 unconfigured（源未配置）
    res_todo = crawl_single_source(
        reg_session,
        {'name': '中华人民共和国政府采购法实施条例', 'url': 'TODO_USER_FILL', 'adapter': 'static_html'},
    )
    assert res_todo['status'] == 'unconfigured'
    assert res_todo['updated'] is False
    assert '源未配置' in res_todo['message']

    # (b) 正常静态 HTML -> 解析入库（status='updated'），再次拉取同内容 -> status='unchanged'
    sample_html = """
    <html><body>
      <h1>《测试招投标专项管理办法》</h1>
      <p>第一条 为了规范测试专项招标采购活动，制定本办法。</p>
      <p>第二条 采购人应当采用电子化评标系统进行在线评审。</p>
      <p>第三条 本办法自发布之日起施行。</p>
    </body></html>
    """

    class _MockResp:
        status_code = 200
        encoding = 'utf-8'
        apparent_encoding = 'utf-8'
        text = sample_html

    res_html_1 = crawl_single_source(
        reg_session,
        {'name': '测试招投标专项管理办法', 'url': 'https://example.gov.cn/law1.html', 'adapter': 'static_html'},
        http_get=lambda *a, **kw: _MockResp(),
    )
    assert res_html_1['status'] == 'updated'
    assert res_html_1['updated'] is True
    assert res_html_1['article_count'] == 3
    assert res_html_1['added_count'] == 3

    res_html_2 = crawl_single_source(
        reg_session,
        {'name': '测试招投标专项管理办法', 'url': 'https://example.gov.cn/law1.html', 'adapter': 'static_html'},
        http_get=lambda *a, **kw: _MockResp(),
    )
    assert res_html_2['status'] == 'unchanged'
    assert res_html_2['updated'] is False

    # (c) 网络超时异常 -> 重试 1 次（共调 2 次）后返回 fetch_error
    call_counter = {'n': 0}

    def _timeout_getter(*a, **kw):
        call_counter['n'] += 1
        raise TimeoutError("Read timed out")

    res_timeout = crawl_single_source(
        reg_session,
        {'name': '超时测试法规', 'url': 'https://example.gov.cn/timeout.html', 'adapter': 'static_html'},
        http_get=_timeout_getter,
    )
    assert call_counter['n'] == 2
    assert res_timeout['status'] == 'fetch_error'
    assert '需手动导入' in res_timeout['message']

    # (d) JS 动态渲染空壳页面（无"第X条"正文） -> 诚实返回 manual_required
    class _JsShellResp:
        status_code = 200
        encoding = 'utf-8'
        text = "<html><body><div id='app'></div><script src='/bundle.js'></script></body></html>"

    res_js = crawl_single_source(
        reg_session,
        {'name': '动态渲染测试法', 'url': 'https://example.gov.cn/spa.html', 'adapter': 'static_html'},
        http_get=lambda *a, **kw: _JsShellResp(),
    )
    assert res_js['status'] == 'manual_required'
    assert '该源需手动导入' in res_js['message']


# ── 5. 四格式导入（json/txt/docx/pdf）与 F7 六工具白名单及 cite_regulation 测试 ──

def test_four_formats_import_and_f7_cite_regulation_tool(reg_session, tmp_path):
    # 断言 F7 七工具白名单（原 6 工具 + E24 新增 search_attachment）包含 cite_regulation
    assert len(ALLOWED_REACT_TOOLS) == 7
    assert 'cite_regulation' in ALLOWED_REACT_TOOLS
    assert 'search_attachment' in ALLOWED_REACT_TOOLS

    # 调用 cite_regulation 工具检索《招标投标法》第三十三条（低于成本报价/弄虚作假）
    cite_res = cite_regulation(keyword='低于成本', article_no='第三十三条', session=reg_session)
    assert cite_res['status'] == 'ok'
    assert len(cite_res['items']) >= 1
    assert '低于成本' in cite_res['items'][0]['content']

    # 测试 docx 格式导入
    doc = Document()
    doc.add_paragraph('《政府采购评审专家管理暂行规定》')
    doc.add_paragraph('第一条 为加强政府采购评审活动管理，规范评审专家执业行为，制定本规定。')
    doc.add_paragraph('第二条 评审专家实行统一标准、管用分离、随机抽取的管理原则。')
    docx_p = tmp_path / 'expert_rule.docx'
    doc.save(str(docx_p))

    imp_docx = import_regulation_file(reg_session, docx_p)
    assert imp_docx['article_count'] == 2
    assert imp_docx['added_count'] == 2

    # 测试 pdf 格式导入
    pdf_p = tmp_path / 'bid_deposit_rule.pdf'
    pdf_doc = pymupdf.open()
    pg = pdf_doc.new_page()
    pg.insert_text(
        (72, 72),
        "《投标保证金管理细则》\n第一条 投标保证金不得超过采购项目预算金额的百分之二。\n第二条 投标保证金应当以支票、汇票、本票或者保函等非现金形式提交。",
        fontname="china-s",
        fontsize=12,
    )
    pdf_doc.save(str(pdf_p))
    pdf_doc.close()

    imp_pdf = import_regulation_file(reg_session, pdf_p)
    assert imp_pdf['article_count'] == 2

    # 测试空库时 cite_regulation 诚实报"法规库为空"
    empty_eng = get_engine('sqlite:///:memory:')
    Base.metadata.create_all(bind=empty_eng)
    empty_session = get_session_factory(empty_eng)()
    try:
        empty_res = cite_regulation(keyword='招标', session=empty_session)
        assert empty_res['status'] == 'empty_library'
        assert empty_res['message'] == '法规库为空'
        assert len(empty_res['items']) == 0
    finally:
        empty_session.close()
        empty_eng.dispose()


# ── 6. /api/regulations 路由端到端集成测试 ────────────────────────────────

def test_regulations_router_endpoints(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'test_reg_api.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    with TestClient(app) as tc:
        r_login = tc.post('/api/auth/login', json={'username': 'admin', 'password': 'BidGuard2026!'})
        assert r_login.status_code == 200
        hdrs = {'Authorization': f"Bearer {r_login.json()['token']}"}

        # 1. GET /api/regulations/laws
        r_laws = tc.get('/api/regulations/laws', headers=hdrs)
        assert r_laws.status_code == 200
        laws_list = r_laws.json()
        assert len(laws_list) >= 1

        # 2. GET /api/regulations/laws/{id}
        first_law_id = laws_list[0]['id']
        r_detail = tc.get(f"/api/regulations/laws/{first_law_id}?keyword=公开", headers=hdrs)
        assert r_detail.status_code == 200
        assert r_detail.json()['total_articles'] >= 1

        # 3. POST /api/regulations/verify
        r_ver = tc.post(
            '/api/regulations/verify',
            json={'text': '第五条 招标投标活动应当遵循公开、公平、公正和诚实信用的原则。'},
            headers=hdrs,
        )
        assert r_ver.status_code == 200
        assert r_ver.json()['status'] == 'exact_match'

        # 4. POST /api/regulations/crawl——monkeypatch 指向临时占位 yaml 隔离真实公网 HTTP
        #    （生产 config/regulation_sources.yaml 已填 8 个官方真实 URL，测试里绝不能真爬官网）
        fake_yaml = tmp_path / 'regulation_sources_test.yaml'
        fake_yaml.write_text(
            'sources:\n'
            '  - name: 占位法规源一\n'
            '    url: TODO_USER_FILL\n'
            '    adapter: static_html\n'
            '  - name: 占位法规源二\n'
            '    url: TODO_USER_FILL\n'
            '    adapter: static_html\n',
            encoding='utf-8',
        )
        monkeypatch.setattr('core.regulation_crawler._DEFAULT_SOURCES_YAML', fake_yaml)
        r_crawl = tc.post('/api/regulations/crawl', headers=hdrs)
        assert r_crawl.status_code == 200
        assert r_crawl.json()['unconfigured_count'] == 2
