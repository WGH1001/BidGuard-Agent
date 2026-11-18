# -*- coding: utf-8 -*-
"""E38 · F8 写作 Skills 与 AI 动态编排及 E36 异步深层研判全链路验收测试。

覆盖 E38 全部核心交付：
1. `core/composer_skills.py`：3 套内置预设 Skill、YAML Frontmatter+MD / JSON 双格式解析、
   版式数值安全钳位（字号/行距/颜色）、SSRF 主机白名单拦截、GitHub -> Gitee 自动优雅降级、
   离线无网可控异常（`EXTERNAL_SKILL_UNREACHABLE`）与 HTML 错误页拦截；
2. `core/bid_composer.py`：F8 AI 动态规划章节 + 分节撰稿（覆盖率兜底守卫、防金钱/合同幻觉锁、
   报价字段锁、修复 Part 4 `6、报价过低/评分细则` 串行污染、独立居中封面/Heading1-3/TOC、
   `append_checklist_at_end` 内外部清单分离开关）；
3. `/api/composer/skills` CRUD + 租户隔离（404）+ 最后 1 个 Skill 删除保护（409 `LAST_SKILL_PROTECTED`）+
   本地 `.md/.json` 文件导入 + 异步后台生成（`async_run=True` + `get_worker().wait_all()`）+ `/cancel` 中断；
4. `core/bid_similarity.py` & `/api/similarity`：解除 6 份上限（实测 7 份同时比对 → 21 对矩阵）、
   可选 AI 语义深层复核（双侧 `verify_quote` 严审剔除未溯源幻觉、禁输出洗稿规避文本、导出报告第五章）、
   异步执行 + `/cancel` 中断；
5. SQLite 老库自动补列迁移守卫与 `recover_stale_tasks()` 服务重启复位。
"""
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import threading
import zipfile

from docx import Document
from fastapi.testclient import TestClient
import pytest
import requests
from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.main import create_app, recover_stale_tasks
from backend.models import (
    BidComposeDraft,
    SimilarityReport,
    ensure_bid_compose_drafts_columns,
    ensure_similarity_reports_columns,
)
import backend.routers.composer as composer_router_mod
import backend.routers.similarity as similarity_router_mod
from backend.worker import get_worker
from core.bid_composer import compose_bid_blueprint, export_composed_bid_docx
from core.bid_similarity import analyze_bid_similarity, export_similarity_report_docx
from core.composer_skills import (
    BUILTIN_WRITING_SKILLS,
    SkillImportError,
    import_skill_from_git_url,
    normalize_layout_spec,
    parse_skill_markdown_or_json,
)
from core.llm_gateway import LLMResponse


def _login_headers(client: TestClient, username: str, password: str = 'BidGuard2026!') -> dict[str, str]:
    resp = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert resp.status_code == 200, resp.text
    return {'Authorization': f"Bearer {resp.json()['token']}"}


def _make_docx(path: Path, paragraphs: list[str], tables: list[list[list[str]]] | None = None, *, author: str | None = None) -> Path:
    doc = Document()
    if author:
        doc.core_properties.author = author
    for text_line in paragraphs:
        doc.add_paragraph(text_line)
    for tbl_rows in (tables or []):
        tbl = doc.add_table(rows=len(tbl_rows), cols=len(tbl_rows[0]))
        tbl.style = 'Table Grid'
        for ri, row in enumerate(tbl_rows):
            for ci, val in enumerate(row):
                tbl.rows[ri].cells[ci].text = val
    doc.save(str(path))
    return path


def _collect_docx_text(docx_path: Path) -> str:
    doc = Document(str(docx_path))
    chunks = [p.text for p in doc.paragraphs]
    for sec in doc.sections:
        chunks.extend(p.text for p in sec.header.paragraphs)
        chunks.extend(p.text for p in sec.footer.paragraphs)
    for t in doc.tables:
        for r in t.rows:
            for c in r.cells:
                chunks.append(c.text)
    return '\n'.join(chunks)


# ── 1. core/composer_skills：解析、预设、SSRF 白名单与 GitHub->Gitee 优雅降级 ───────

def test_composer_skills_builtin_parsing_ssrf_and_git_fallback(monkeypatch):
    assert len(BUILTIN_WRITING_SKILLS) >= 3
    names = {s['name'] for s in BUILTIN_WRITING_SKILLS}
    assert {'政采严谨全响应版', '技术架构深度攻坚版', '简洁高效商务精编版'}.issubset(names)

    # 1) 极端排版数值应被安全钳位
    clamped = normalize_layout_spec({
        'cover_style': 'navy_business',
        'font_size_pt': 99,
        'line_spacing': 0.2,
        'accent_color_hex': 'not_hex',
        'target_section_words': 999999,
        'tone': 'invalid_tone',
    })
    assert clamped['cover_style'] == 'navy_business'
    assert 9.0 <= clamped['font_size_pt'] <= 16.0
    assert 1.0 <= clamped['line_spacing'] <= 2.5
    assert len(clamped['accent_color_hex']) == 6 and all(c in '0123456789ABCDEF' for c in clamped['accent_color_hex'])
    assert 150 <= clamped['target_section_words'] <= 3000
    assert clamped['tone'] == 'formal_gov'

    # 2) SKILL.md YAML frontmatter + 正文解析
    md_skill = """---
name: 金融信创高并发投标规范
description: 强调信创适配与压测证明材料（绝不虚构数据）
cover_style: tech_modern
tone: technical_deep
font_zh: 微软雅黑
font_size_pt: 11
target_section_words: 700
append_checklist_at_end: false
---
# 写作规则
1. 每章先给出架构总览图说明，再逐条应答实质性指标。
2. 严禁虚构合同业绩或报价金额。
"""
    parsed_md = parse_skill_markdown_or_json(md_skill, source_name='fintech.md')
    assert parsed_md['name'] == '金融信创高并发投标规范'
    assert parsed_md['layout_spec']['cover_style'] == 'tech_modern'
    assert parsed_md['layout_spec']['tone'] == 'technical_deep'
    assert parsed_md['layout_spec']['append_checklist_at_end'] is False
    assert '每章先给出架构总览图说明' in parsed_md['writing_instructions']

    # JSON 格式导出物也可无缝重导入
    json_payload = json.dumps({
        'name': 'JSON导入规范',
        'description': 'JSON format check',
        'writing_instructions': '精简商务应答',
        'layout_spec': {'cover_style': 'gov_clean', 'tone': 'concise_direct'},
    }, ensure_ascii=False)
    parsed_json = parse_skill_markdown_or_json(json_payload, source_name='skill.json')
    assert parsed_json['name'] == 'JSON导入规范'
    assert parsed_json['layout_spec']['tone'] == 'concise_direct'

    # 3) SSRF 白名单拦截非法域名 / 非 HTTPS
    with pytest.raises(SkillImportError) as exc_unsafe:
        import_skill_from_git_url('http://127.0.0.1:8000/SKILL.md')
    assert exc_unsafe.value.code == 'UNSAFE_SKILL_URL'

    with pytest.raises(SkillImportError) as exc_host:
        import_skill_from_git_url('https://evil.example.org/owner/repo/SKILL.md')
    assert exc_host.value.code == 'UNSAFE_SKILL_URL'

    # 4) GitHub 不可达时自动降级到 Gitee 镜像并成功提取
    class _FakeResp:
        def __init__(self, status_code: int, text: str, url: str, content_type: str = 'text/plain; charset=utf-8'):
            self.status_code = status_code
            self.text = text
            self.url = url
            self.headers = {'Content-Type': content_type}

    def _fake_get_fallback(self, url: str, **kwargs):
        if 'github' in url:
            raise requests.Timeout('simulated TUN/GFW timeout on GitHub')
        if 'gitee.com' in url and url.endswith('/raw/main/SKILL.md'):
            return _FakeResp(200, md_skill, url)
        return _FakeResp(404, 'Not Found', url)

    monkeypatch.setattr(requests.Session, 'get', _fake_get_fallback)
    res_fallback = import_skill_from_git_url('https://github.com/demo-org/bid-skill-pack')
    assert res_fallback['fetched_from_provider'] == 'gitee'
    assert res_fallback['used_fallback'] is True
    assert res_fallback['name'] == '金融信创高并发投标规范'

    # 5a) 返回 HTML 登录/错误壳页时安全拒收（不误当 Markdown 入库）
    def _fake_get_html_shell(self, url: str, **kwargs):
        return _FakeResp(200, '<!DOCTYPE html><html><body>Login required</body></html>', url, 'text/html')

    monkeypatch.setattr(requests.Session, 'get', _fake_get_html_shell)
    with pytest.raises(SkillImportError) as exc_html:
        import_skill_from_git_url('demo-org/bid-skill-pack')
    assert exc_html.value.code == 'SKILL_FILE_NOT_FOUND'

    # 5b) GitHub 与 Gitee 均断网不可达 -> 明确抛出 EXTERNAL_SKILL_UNREACHABLE
    def _fake_get_all_down(self, url: str, **kwargs):
        raise requests.ConnectionError('offline environment')

    monkeypatch.setattr(requests.Session, 'get', _fake_get_all_down)
    with pytest.raises(SkillImportError) as exc_offline:
        import_skill_from_git_url('demo-org/bid-skill-pack')
    assert exc_offline.value.code == 'EXTERNAL_SKILL_UNREACHABLE'


# ── 2. core/bid_composer：AI 动态规划/撰稿、防幻觉锁、Part4 修复与封面目录排版 ────

class _FakeComposerLLM:
    """模拟大模型：刻意漏掉一个评分主题并塞入虚假合同金额以检验防御守卫。"""

    def __init__(self):
        self.plan_calls = 0
        self.draft_calls = 0

    def chat(self, messages: list, model_tier: str = 'flagship', **kwargs) -> LLMResponse:
        prompt = str((messages or [{}])[-1].get('content') or '')
        if '《技术与商务实施方案》动态章节' in prompt:
            self.plan_calls += 1
            # 刻意仅规划第 1 个主题，故意漏掉后面主题，验证确定性覆盖兜底守卫！
            payload = {
                'chapters': [
                    {
                        'chapter_no': '第一部分·技术篇-1',
                        'title': '核心数据库高可用与信创适配架构',
                        'chapter_kind': 'technical',
                        'focus_topic_ids': ['topic_1'],
                        'writing_brief': '深入阐述数据库双活架构与国产化兼容路径',
                        'recommended_table': '高可用SLA保障表',
                    }
                ]
            }
            return LLMResponse(content=json.dumps(payload, ensure_ascii=False), model='fake-flash')

        if '为以下招标实质条款逐条撰写' in prompt:
            payload = {
                'responses': [
                    {'seq': 1, 'response': '完全响应：承诺 7×24 小时本地化应急技术响应，2 小时到场。'},
                ]
            }
            return LLMResponse(content=json.dumps(payload, ensure_ascii=False), model='fake-flash')

        self.draft_calls += 1
        # 刻意包含虚构大额人民币数字以触发 _sanitize_ai_draft_text 防金钱幻觉锁
        payload = {
            'paragraphs': [
                '我方针对本项目制定了完善的工程实施组织方案，拟安排资深架构师驻场保障，曾中标同类项目合同金额 9980000 元可作参考。',
                '所有核心节点均可按招标进度表保质交付。',
            ],
            'table': {
                'title': '阶段实施保障表',
                'headers': ['阶段', '主要任务', '交付成果'],
                'rows': [['准备期', '环境巡检', '巡检报告'], ['实施期', '集群部署', '上线验收单']],
            },
            'needs_manual': ['请技术总监补充近三年同级别真实合同扫描件（脱敏版）'],
        }
        return LLMResponse(content=json.dumps(payload, ensure_ascii=False), model='fake-flash')


def test_f8_ai_dynamic_composer_coverage_guard_anti_hallucination_and_clean_layout(tmp_path: Path):
    tender_p = _make_docx(
        tmp_path / 'tender_e38.docx',
        paragraphs=[
            '智慧政务云数据库运维保障项目 招标文件',
            '采购项目名称：智慧政务云数据库运维保障项目',
            '项目编号：ZC-2026-E38',
            '采购人名称：某市政务服务和数据管理局',
            '开标时间：2026年12月28日 09时00分',
            '本项目最高限价为 1,800,000.00 元，超过限价无效。',
            '第三章 资格审查要求：投标人须提供营业执照及ISO27001认证证书。',
            '第四章 实质性条款：',
            '★1. 须承诺提供 7×24 小时本地化应急技术响应，2 小时到达现场。',
            '▲2. 核心监控组件须全面适配国产数据库环境。',
            '6、报价过低且不能提供有效成本说明的，评标委员会依据评分细则作扣分或废标处理。',
            '第五章 评分细则',
        ],
        tables=[
            [
                ['评分因素', '评分标准', '分值'],
                ['数据库高可用运维架构', '提出完善的双活集群巡检与故障自愈架构。', '30'],
                ['重大活动现场保障方案', '详述重要保障期间专人驻场值守机制。', '20'],
                ['本地化售后服务承诺', '包含备品备件及培训移交计划。', '15'],
            ]
        ],
    )

    skill_custom = {
        'id': 99,
        'name': '深蓝商务精编测试Skill',
        'writing_instructions': '结构干练、分点清晰，重点回应数据库高可用及现场保障。',
        'layout_spec': {
            'cover_style': 'navy_business',
            'font_zh': '微软雅黑',
            'heading_font_zh': '黑体',
            'font_size_pt': 10.5,
            'line_spacing': 1.25,
            'accent_color_hex': '#1E3A8A',
            'table_header_bg': '#1E3A8A',
            'include_toc': True,
            'append_checklist_at_end': False,
            'target_section_words': 500,
            'tone': 'technical_deep',
        },
    }
    fake_llm = _FakeComposerLLM()
    bp = compose_bid_blueprint(
        tender_p,
        ledger_certs=[{'cert_name': '企业法人营业执照', 'valid_until': None}],
        client=fake_llm,
        skill=skill_custom,
        use_ai=True,
    )

    assert bp.ai_status == 'done'
    assert fake_llm.plan_calls == 1
    assert fake_llm.draft_calls >= 1

    bp_dict = bp.to_dict()
    # 1) Part 4 实质性偏离表不得串入 `6、报价过低...评分细则`（eval_rule 已剥离）
    sec_dev = next(s for s in bp_dict['sections'] if s['section_id'] == 'sec_deviation')
    dev_text = json.dumps(sec_dev, ensure_ascii=False)
    assert '报价过低' not in dev_text
    assert '评分细则' not in dev_text

    # 2) 防覆盖遗漏守卫：虽然 AI 只返回了 1 章，但其余评分主题必须兜底保留在方案章节中
    sec_scoring = next(s for s in bp_dict['sections'] if s['section_id'] == 'sec_scoring')
    scoring_text = json.dumps(sec_scoring, ensure_ascii=False)
    assert '重大活动现场保障方案' in scoring_text
    assert '本地化售后服务承诺' in scoring_text

    # 3) 防金钱幻觉守卫：AI 杜撰的 `9980000 元` 必须被中和拦截，报价栏仍保留 [待人工填报金额...]
    assert '9980000' not in scoring_text
    assert '[待人工核实金额]' in scoring_text
    sec_price = next(s for s in bp_dict['sections'] if s['section_id'] == 'sec_price')
    assert '[待人工填报金额' in json.dumps(sec_price, ensure_ascii=False)

    # 4) 导出 Word：检验独立封面、Heading 1/2 原生大纲、TOC 域，以及 append_checklist_at_end=False 不污染投标正本
    out_clean_body = export_composed_bid_docx(bp, tmp_path / 'out_clean_body.docx')
    doc_clean = Document(str(out_clean_body))
    styles_used = {p.style.name for p in doc_clean.paragraphs}
    assert 'Heading 1' in styles_used
    assert 'Heading 2' in styles_used
    full_clean_txt = _collect_docx_text(out_clean_body)
    assert '投 标 文 件' in full_clean_txt
    assert '智慧政务云数据库运维保障项目' in full_clean_txt
    assert '人工待填报清单总览与企业台账备查附录' not in full_clean_txt
    with zipfile.ZipFile(str(out_clean_body), 'r') as zf:
        doc_xml = zf.read('word/document.xml').decode('utf-8', errors='ignore')
        assert 'TOC \\o' in doc_xml

    # 5) 当 append_checklist_at_end=True 时文末正确附带内部核对附录
    bp_dict['skill_layout'] = {**skill_custom['layout_spec'], 'append_checklist_at_end': True}
    out_with_app = export_composed_bid_docx(bp_dict, tmp_path / 'out_with_app.docx')
    assert '人工待填报清单总览与企业台账备查附录' in _collect_docx_text(out_with_app)


# ── 3. API：/api/composer/skills 全生命周期 + 文件导入 + 异步生成 + 中断取消 ──────

def test_e38_composer_skills_api_and_async_generate_cancel(tmp_path: Path, monkeypatch):
    db_file = tmp_path / 'e38_composer_api.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    tender_p = _make_docx(
        tmp_path / 'api_tender_e38.docx',
        paragraphs=[
            '项目名称：市级政务数据灾备建设采购项目',
            '项目编号：ZC-E38-009',
            '开标时间：2026年12月30日 10时00分',
            '本项目最高限价 900,000.00 元。',
            '★1. 须提供同城双活灾备链路保障方案。',
        ],
    )
    fake_llm = _FakeComposerLLM()
    monkeypatch.setattr(composer_router_mod, 'resolve_effective_llm_client', lambda _s, _uid: (fake_llm, 'active'))

    created_draft_ids: list[int] = []
    try:
        with TestClient(app) as tc:
            admin_h = _login_headers(tc, 'admin')
            u_b = tc.post(
                '/api/auth/users',
                headers=admin_h,
                json={'username': 'e38_t2', 'password': 'Password123!', 'tenant_id': 2, 'role': 'member'},
            )
            assert u_b.status_code == 200
            t2_h = _login_headers(tc, 'e38_t2', 'Password123!')

            # 1) 首次拉取自动初始化内置预设 Skill（含 E38 基础 3 套及后续扩展预设）
            r_list = tc.get('/api/composer/skills', headers=admin_h)
            assert r_list.status_code == 200
            assert r_list.json()['total'] == len(BUILTIN_WRITING_SKILLS)
            default_skill = next(s for s in r_list.json()['items'] if s['is_default'])
            assert default_skill['name'] == '政采严谨全响应版'

            # 2) 新建自定义 Skill + 跨租户隔离
            r_create = tc.post(
                '/api/composer/skills',
                headers=admin_h,
                json={
                    'name': '自研测试规范',
                    'description': 'E38 API 自定义测试',
                    'writing_instructions': '简明条理应答所有技术需求。',
                    'layout_spec': {'cover_style': 'tech_modern', 'font_size_pt': 11, 'tone': 'concise_direct'},
                    'is_default': True,
                },
            )
            assert r_create.status_code == 200
            custom_id = r_create.json()['id']
            assert tc.put(f'/api/composer/skills/{custom_id}', headers=t2_h, json={
                'name': '越权篡改',
                'description': '',
                'writing_instructions': 'bad',
                'layout_spec': {},
                'is_default': False,
            }).status_code == 404
            assert tc.delete(f'/api/composer/skills/{custom_id}', headers=t2_h).status_code == 404

            # 3) 本地上传 .md 导入 Skill + 无网络时 GitHub preview 返回可控 422
            md_bytes = "---\nname: 本地MD导入Skill\ndescription: local file\n---\n全部以事实为准。".encode('utf-8')
            r_imp_file = tc.post(
                '/api/composer/skills/import-file',
                headers=admin_h,
                files={'file': ('custom_skill.md', md_bytes, 'text/markdown')},
            )
            assert r_imp_file.status_code == 200
            assert r_imp_file.json()['name'] == '本地MD导入Skill'

            monkeypatch.setattr(
                composer_router_mod,
                'preview_skill_from_git_url',
                lambda *a, **kw: (_ for _ in ()).throw(
                    SkillImportError('EXTERNAL_SKILL_UNREACHABLE', 'GitHub 与 Gitee 均不可达')
                ),
            ) if hasattr(composer_router_mod, 'preview_skill_from_git_url') else monkeypatch.setattr(
                composer_router_mod,
                'import_skill_from_git_url',
                lambda *a, **kw: (_ for _ in ()).throw(
                    SkillImportError('EXTERNAL_SKILL_UNREACHABLE', 'GitHub 与 Gitee 均不可达')
                ),
            )
            r_prev_err = tc.post(
                '/api/composer/skills/preview-url',
                headers=admin_h,
                json={'url_or_repo': 'demo-org/unavailable-skill', 'allow_gitee_fallback': True},
            )
            assert r_prev_err.status_code == 422
            assert r_prev_err.json()['detail']['code'] == 'EXTERNAL_SKILL_UNREACHABLE'

            # 4) 删除到仅剩最后 1 个 Skill 时触发 409 LAST_SKILL_PROTECTED
            all_ids = [s['id'] for s in tc.get('/api/composer/skills', headers=admin_h).json()['items']]
            for sid in all_ids[:-1]:
                assert tc.delete(f'/api/composer/skills/{sid}', headers=admin_h).status_code == 200
            r_last_del = tc.delete(f'/api/composer/skills/{all_ids[-1]}', headers=admin_h)
            assert r_last_del.status_code == 409
            assert r_last_del.json()['detail']['code'] == 'LAST_SKILL_PROTECTED'

            # 5) 异步后台生成（async_run=true + use_ai=true + skill_id）
            r_async = tc.post(
                '/api/composer/generate',
                headers=admin_h,
                files=[('tender_file', ('api_tender_e38.docx', tender_p.read_bytes(), 'application/octet-stream'))],
                data={
                    'skill_id': str(all_ids[-1]),
                    'use_ai': 'true',
                    'async_run': 'true',
                    'title': '异步AI编排草稿',
                },
            )
            assert r_async.status_code == 200, r_async.text
            did = r_async.json()['id']
            created_draft_ids.append(did)
            assert get_worker().wait_all(timeout=15.0) is True

            r_done = tc.get(f'/api/composer/drafts/{did}', headers=admin_h)
            assert r_done.status_code == 200
            done_body = r_done.json()
            assert done_body['status'] == 'completed'
            assert done_body['progress'] == 100
            assert done_body['ai_status'] == 'done'

            r_dl = tc.get(f'/api/composer/drafts/{did}/download', headers=admin_h)
            assert r_dl.status_code == 200
            assert zipfile.is_zipfile(BytesIO(r_dl.content))

            # 已完成任务再次调 cancel 返回 409 TASK_NOT_RUNNING
            r_c_idle = tc.post(f'/api/composer/drafts/{did}/cancel', headers=admin_h)
            assert r_c_idle.status_code == 409
            assert r_c_idle.json()['detail']['code'] == 'TASK_NOT_RUNNING'

            # 6) 模拟慢速任务实时触发 /cancel 成功中断并置 status='failed'
            entered = threading.Event()
            release = threading.Event()

            def _slow_compose(*args, cancel_check=None, **kwargs):
                entered.set()
                release.wait(timeout=3.0)
                if cancel_check is not None:
                    cancel_check()
                raise RuntimeError('expected cancellation')

            monkeypatch.setattr(composer_router_mod, 'compose_bid_blueprint', _slow_compose)
            r_slow = tc.post(
                '/api/composer/generate',
                headers=admin_h,
                files=[('tender_file', ('slow_t.docx', tender_p.read_bytes(), 'application/octet-stream'))],
                data={'async_run': 'true', 'use_ai': 'true', 'title': '可取消生成测试'},
            )
            assert r_slow.status_code == 200
            slow_id = r_slow.json()['id']
            created_draft_ids.append(slow_id)
            assert entered.wait(timeout=5.0) is True
            r_cancel = tc.post(f'/api/composer/drafts/{slow_id}/cancel', headers=admin_h)
            assert r_cancel.status_code == 200
            release.set()
            assert get_worker().wait_all(timeout=10.0) is True

            r_slow_after = tc.get(f'/api/composer/drafts/{slow_id}', headers=admin_h)
            assert r_slow_after.json()['status'] == 'failed'
            assert r_slow_after.json()['error_msg'] == '用户中断'

            # 清理草稿并确认磁盘上传/导出目录随 DELETE 释放
            for cleanup_id in list(created_draft_ids):
                assert tc.delete(f'/api/composer/drafts/{cleanup_id}', headers=admin_h).status_code == 200
            created_draft_ids.clear()
    finally:
        pass


# ── 4. E36 解除 6 份上限 + AI 语义深层复核 + verify_quote + 异步/取消 + 迁移 ────────

class _FakeSimilarityLLM:
    def __init__(self, verified_quote_a: str, verified_quote_b: str):
        self.verified_quote_a = verified_quote_a
        self.verified_quote_b = verified_quote_b

    def chat(self, messages: list, model_tier: str = 'flagship', **kwargs) -> LLMResponse:
        payload = {
            'overall_summary': (
                '两家单位技术运维方案高度语义同构，建议核实编写链路；'
                '切勿提供规避清标洗稿技巧。'  # 刻意塞入禁用词验证合规拦截重写
            ),
            'semantic_matches': [
                {
                    'a_label': '参选1',
                    'b_label': '参选2',
                    'similarity_type': 'semantic_isomorphism',
                    'reason': '核心微服务故障迁移架构与步骤顺序高度对应',
                    'a_quote': self.verified_quote_a,
                    'b_quote': self.verified_quote_b,
                },
                {
                    'a_label': '参选1',
                    'b_label': '参选2',
                    'similarity_type': 'semantic_isomorphism',
                    'reason': '模型编造的不存在片段',
                    'a_quote': '这是一段大模型幻觉杜撰的假原文12345',
                    'b_quote': '这是另一段根本不在文档里的假引用67890',
                },
            ],
            'false_positive_notes': ['GB/T 22239 国家标准名称属于行业通用表述，可作常规排查。'],
        }
        return LLMResponse(content=json.dumps(payload, ensure_ascii=False), model='fake-flash')


def test_e38_similarity_unlimited_files_ai_verify_quote_and_async_api(tmp_path: Path, monkeypatch):
    quote_a = '我方将部署多活分布式事务网关并在两分钟内完成主备自动切换，同时每日凌晨两点全量冷备归档。'
    quote_b = '本公司采用多活分布式事务网关实现两分钟内主备快速倒换，于每天凌晨两点自动执行全量冷备归档。'
    tender_p = _make_docx(tmp_path / 'sim_tender.docx', ['公共需求：平台需符合等保三级标准与高可用保障。'])

    bid_paths: list[dict] = []
    for idx in range(1, 8):  # 实测 7 份（突破旧 6 份上限！）
        extra = quote_a if idx == 1 else (quote_b if idx == 2 else f'第{idx}家独立撰写的多节点巡检与自动化运维规划。')
        p = _make_docx(
            tmp_path / f'bid_{idx}.docx',
            ['公共需求：平台需符合等保三级标准与高可用保障。', extra],
        )
        bid_paths.append({'label': f'参选{idx}', 'path': p})

    fake_sim_llm = _FakeSimilarityLLM(quote_a, quote_b)
    res_core = analyze_bid_similarity(
        bid_paths,
        tender_path=tender_p,
        enable_ai_review=True,
        client=fake_sim_llm,
    )
    assert len(res_core['bidders']) == 7
    assert len(res_core['pair_matrix']) == 21  # C(7, 2) = 21

    ai_rev = res_core['ai_review']
    assert ai_rev['status'] == 'done'
    # 合规锁：出现 "规避清标" / "洗稿技巧" 时整体综述被替换为安全文本
    assert '规避清标' not in ai_rev['summary']
    assert '洗稿技巧' not in ai_rev['summary']
    # verify_quote 严审：真实双侧连续子串保留，幻觉编造句被剔除！
    assert len(ai_rev['semantic_matches']) == 1
    assert ai_rev['semantic_matches'][0]['quote_verified'] is True
    assert ai_rev['semantic_matches'][0]['a_quote'] == quote_a
    assert ai_rev['semantic_matches'][0]['b_quote'] == quote_b

    sim_findings = [f for f in res_core['findings'] if f['code'] == 'SEMANTIC_ISOMORPHISM']
    assert len(sim_findings) == 1

    rep_docx = export_similarity_report_docx({'title': '7家AI交叉研判', **res_core}, tmp_path / 'sim_7_ai.docx')
    assert '五、AI 语义深层复核与误报排查综述' in _collect_docx_text(rep_docx)

    # API 全链路：7 份同时上传（不再触发 MAX_BID_FILE_COUNT）+ async_run=true + AI 复核 + 导出 + 删除清盘
    db_file = tmp_path / 'e38_sim_api.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)
    monkeypatch.setattr(similarity_router_mod, 'resolve_effective_llm_client', lambda _s, _uid: (fake_sim_llm, 'active'))

    with TestClient(app) as tc:
        admin_h = _login_headers(tc, 'admin')
        files_payload = [('tender_file', ('sim_tender.docx', tender_p.read_bytes(), 'application/octet-stream'))]
        for b in bid_paths:
            files_payload.append(('bid_files', (b['path'].name, b['path'].read_bytes(), 'application/octet-stream')))

        r_post = tc.post(
            '/api/similarity/compare',
            headers=admin_h,
            files=files_payload,
            data={
                'title': '7家标书异步AI比对',
                'labels_json': json.dumps([b['label'] for b in bid_paths], ensure_ascii=False),
                'enable_ai_review': 'true',
                'async_run': 'true',
            },
        )
        assert r_post.status_code == 200, r_post.text
        rid = r_post.json()['id']
        assert get_worker().wait_all(timeout=15.0) is True

        r_get = tc.get(f'/api/similarity/{rid}', headers=admin_h)
        assert r_get.status_code == 200
        body = r_get.json()
        assert body['status'] == 'completed'
        assert body['progress'] == 100
        assert body['bidder_count'] == 7
        assert body['ai_review']['status'] == 'done'

        r_exp = tc.get(f'/api/similarity/{rid}/export', headers=admin_h)
        assert r_exp.status_code == 200
        assert zipfile.is_zipfile(BytesIO(r_exp.content))

        assert tc.post(f'/api/similarity/{rid}/cancel', headers=admin_h).status_code == 409
        assert tc.delete(f'/api/similarity/{rid}', headers=admin_h).status_code == 200


def test_e38_legacy_sqlite_migration_and_stale_task_recovery(tmp_path: Path):
    # 1) 模拟老版本缺列 SQLite 表，验证 ensure_*_columns 自动幂等加列
    legacy_db = tmp_path / 'legacy_e38.db'
    eng = create_engine(f"sqlite:///{legacy_db.as_posix()}")
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE similarity_reports ("
            "id INTEGER PRIMARY KEY, tenant_id INTEGER, user_id INTEGER, title VARCHAR(256))"
        ))
        conn.execute(text(
            "CREATE TABLE bid_compose_drafts ("
            "id INTEGER PRIMARY KEY, tenant_id INTEGER, user_id INTEGER, title VARCHAR(256))"
        ))
    added_sim = ensure_similarity_reports_columns(eng)
    added_comp = ensure_bid_compose_drafts_columns(eng)
    assert 'status' in added_sim and 'ai_review_json' in added_sim
    assert 'status' in added_comp and 'skill_id' in added_comp
    assert ensure_similarity_reports_columns(eng) == []
    assert ensure_bid_compose_drafts_columns(eng) == []
    eng.dispose()

    # 2) 服务重启复位：残留 running / queued 状态全部收口为 failed ('服务重启中断')
    app_db = tmp_path / 'stale_recover.db'
    app_stale = create_app(database_url=f"sqlite:///{app_db.as_posix()}", auto_init_db=True)
    with TestClient(app_stale):
        import backend.database as db_mod
        sess = db_mod.SessionLocal()
        try:
            sess.add(BidComposeDraft(
                tenant_id=1, user_id=1, title='重启残留草稿', tender_filename='t.docx',
                tender_path='x.docx', status='running', progress=45, stage_detail='AI撰稿中',
            ))
            sess.add(SimilarityReport(
                tenant_id=1, user_id=1, title='重启残留比对', bidders_json=[{}, {}, {}],
                status='queued', progress=10, stage_detail='排队中',
            ))
            sess.commit()
        finally:
            sess.close()

        fixed_count = recover_stale_tasks(db_mod.SessionLocal)
        assert fixed_count >= 2

        sess2 = db_mod.SessionLocal()
        try:
            draft_row = sess2.query(BidComposeDraft).filter_by(title='重启残留草稿').first()
            sim_row = sess2.query(SimilarityReport).filter_by(title='重启残留比对').first()
            assert draft_row is not None and draft_row.status == 'failed' and draft_row.error_msg == '服务重启中断'
            assert sim_row is not None and sim_row.status == 'failed' and sim_row.error_msg == '服务重启中断'
        finally:
            sess2.close()
