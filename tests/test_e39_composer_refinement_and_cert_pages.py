# -*- coding: utf-8 -*-
"""E39 · F8 初稿质量精修与资格证明逐项附页增强全链路测试。

覆盖 E39 全部修复与增强点：
1. AI 单章撰稿并发抖动自动重试（最多重试 2 次 / 共 3 次尝试）+ `PipelineCancelled` 穿透不拦截；
2. AI 正文冗余首行/首段重复章标题自动剥离 + 章节明细表行上限放宽（>12 行不截断）；
3. 偏离表 `_draft_deviation_with_llm` 解除 `[:12]` 截断：分批（每批 12 条）全量覆盖 16+ 条实质条款；
4. `_collect_scoring_topics` 过滤目录省略号、投标人须知/澄清/修改/计分方法等程序条款，准确提取 PDF 换行评分细则；
5. 第二部分资格证照同名带角色合并去重（如 `必交资格 / 加分参考`）+ 「2.0 总览索引表 + 2.1~2.N 逐项独立附页」
   （台账存在有效 PNG/JPG 时真实嵌入 `.docx` 图片 `word/media/*`，PDF 示装订框，缺省示 A4 扫描件粘贴框）；
6. `/api/composer/generate` 实图嵌入后清洗剥离服务器 `attachment_path`，绝不泄露宿主机路径给前端/DB。
"""
from datetime import date
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sys
import zipfile

from docx import Document
from fastapi.testclient import TestClient
import pymupdf
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.main import create_app
import backend.routers.composer as composer_router_mod
from backend.worker import PipelineCancelled
from core.bid_composer import (
    _collect_scoring_topics,
    compose_bid_blueprint,
    export_composed_bid_docx,
)
from core.llm_gateway import LLMResponse


def _make_png(path: Path, width: int = 120, height: int = 60) -> Path:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height), False)
    pix.clear_with(210)
    pix.save(str(path))
    return path


def _make_docx(path: Path, paragraphs: list[str], tables: list[list[list[str]]] | None = None) -> Path:
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    for rows in (tables or []):
        tbl = doc.add_table(rows=len(rows), cols=len(rows[0]))
        tbl.style = 'Table Grid'
        for ri, row in enumerate(rows):
            for ci, val in enumerate(row):
                tbl.rows[ri].cells[ci].text = val
    doc.save(str(path))
    return path


def _collect_docx_text(docx_path: Path) -> str:
    doc = Document(str(docx_path))
    chunks = [p.text for p in doc.paragraphs]
    for t in doc.tables:
        for r in t.rows:
            for c in r.cells:
                chunks.append(c.text)
    return '\n'.join(chunks)


def _login_headers(client: TestClient, username: str, password: str = 'BidGuard2026!') -> dict[str, str]:
    resp = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert resp.status_code == 200, resp.text
    return {'Authorization': f"Bearer {resp.json()['token']}"}


# ── 1. 单章瞬时失败自动重试 + 标题去重 + 表格放宽 (>12 行) + 取消穿透 ──────────

def test_e39_single_chapter_retry_duplicate_title_strip_and_wide_table(tmp_path: Path):
    tender_p = _make_docx(
        tmp_path / 't_retry.docx',
        paragraphs=[
            '项目名称：某市高频数据湖改造项目',
            '项目编号：ZC-E39-001',
            '开标时间：2026年12月20日 09时30分',
            '第五章 评分细则',
        ],
        tables=[
            [
                ['评分因素', '评分标准', '分值'],
                ['总体技术架构方案', '架构清晰先进', '30'],
                ['系统嵌入与扩展能力', '支持平滑嵌入与横向扩展', '30'],
            ]
        ],
    )

    class _FlakyRetryLLM:
        def __init__(self):
            self.ch2_attempts = 0

        def chat(self, messages: list, model_tier: str = 'flagship', **kwargs) -> LLMResponse:
            prompt = str((messages or [{}])[-1].get('content') or '')
            if '《技术与商务实施方案》动态章节' in prompt:
                return LLMResponse(content=json.dumps({
                    'chapters': [
                        {
                            'chapter_no': '第一章',
                            'title': '总体技术架构方案',
                            'chapter_kind': 'technical',
                            'focus_topic_ids': ['topic_1'],
                            'writing_brief': '整体架构',
                            'recommended_table': '指标映射表',
                        },
                        {
                            'chapter_no': '第二章',
                            'title': '系统嵌入与扩展能力',
                            'chapter_kind': 'technical',
                            'focus_topic_ids': ['topic_2'],
                            'writing_brief': '嵌入扩展',
                            'recommended_table': '扩展参数表',
                        },
                    ]
                }, ensure_ascii=False), model='fake-flash')

            if '系统嵌入与扩展能力' in prompt:
                self.ch2_attempts += 1
                if self.ch2_attempts == 1:
                    raise TimeoutError('simulated transient timeout')
                if self.ch2_attempts == 2:
                    return LLMResponse(content='NOT A JSON PAYLOAD', model='fake-flash')
                # 第 3 次成功且刻意重复章标题，并附带 15 行（>12）表格
                return LLMResponse(content=json.dumps({
                    'paragraphs': [
                        '第二章 系统嵌入与扩展能力',
                        '系统提供零代码嵌入网关与微服务横向扩展组件。',
                    ],
                    'table': {
                        'title': '15项参数响应明细表',
                        'headers': ['序号', '指标', '响应值'],
                        'rows': [[str(i), f'扩展指标{i}', '完全满足'] for i in range(1, 16)],
                    },
                    'needs_manual': [],
                }, ensure_ascii=False), model='fake-flash')

            # 第一章刻意在同一首段用 `\n` 携带重复标题
            return LLMResponse(content=json.dumps({
                'paragraphs': [
                    '第一章 总体技术架构方案\n采用高可用微服务与存储分离架构保障稳定运行。',
                ],
                'table': None,
                'needs_manual': [],
            }, ensure_ascii=False), model='fake-flash')

    llm = _FlakyRetryLLM()
    bp = compose_bid_blueprint(tender_p, client=llm, use_ai=True)
    assert bp.ai_status == 'done'
    assert llm.ch2_attempts == 3

    sec_s = next(s for s in bp.sections if s.section_id == 'sec_scoring')
    ai_chs = [b for b in sec_s.content_blocks if b.get('kind') == 'ai_chapter']
    assert len(ai_chs) == 2

    # 第一章与第二章均已剥离重复标题首行/首段
    ch1, ch2 = ai_chs[0], ai_chs[1]
    assert ch1['paragraphs'][0].startswith('采用高可用微服务')
    assert all('第一章 总体技术架构方案' not in p for p in ch1['paragraphs'])
    assert ch2['paragraphs'][0] == '系统提供零代码嵌入网关与微服务横向扩展组件。'
    assert all(p.strip() != '第二章 系统嵌入与扩展能力' for p in ch2['paragraphs'])
    # 15 行表格全部保留，不再被 `[:12]` 截断
    assert ch2['table'] is not None and len(ch2['table']['rows']) == 15

    # 验证 PipelineCancelled(BaseException) 绝不被重试逻辑吞掉
    cancel_llm = _FlakyRetryLLM()
    with pytest.raises(PipelineCancelled):
        compose_bid_blueprint(
            tender_p,
            client=cancel_llm,
            use_ai=True,
            cancel_check=lambda: (_ for _ in ()).throw(PipelineCancelled('cancelled!')),
        )


# ── 2. 偏离表解除 `[:12]` 硬截断：分批全量覆盖 16 条 ★/▲ ───────────────────────

def test_e39_deviation_batched_for_all_16_mandatory_clauses(tmp_path: Path):
    para_lines = ['项目名称：十六项实质条款全量拟稿测试', '第四章 实质性要求']
    for i in range(1, 17):
        para_lines.append(f"（{i}）第{i}项配置要求：")
        para_lines.append(f"★{i}. 须满足核心安全指标与运行保障第 {i} 项规范要求。")
        para_lines.append('这是常规格式说明文本，用于分段核验。')
    tender_p = _make_docx(tmp_path / 't_16_dev.docx', paragraphs=para_lines)

    class _BatchedDevLLM:
        def __init__(self):
            self.dev_batch_sizes: list[int] = []

        def chat(self, messages: list, model_tier: str = 'flagship', **kwargs) -> LLMResponse:
            prompt = str((messages or [{}])[-1].get('content') or '')
            if '为以下招标实质条款逐条撰写' in prompt:
                seqs = [int(m) for m in re.findall(r'seq=(\d+)｜', prompt)]
                self.dev_batch_sizes.append(len(seqs))
                return LLMResponse(content=json.dumps({
                    'responses': [
                        {'seq': s, 'response': f'完全响应：第{s}项实质指标我方完全满足。'}
                        for s in seqs
                    ]
                }, ensure_ascii=False), model='fake-flash')
            if '《技术与商务实施方案》动态章节' in prompt:
                return LLMResponse(content=json.dumps({
                    'chapters': [
                        {'chapter_no': '一', 'title': '技术响应总方案', 'focus_topic_ids': ['topic_1']}
                    ]
                }, ensure_ascii=False), model='fake-flash')
            return LLMResponse(content=json.dumps({
                'paragraphs': ['完整方案内容。'],
                'table': None,
                'needs_manual': [],
            }, ensure_ascii=False), model='fake-flash')

    llm = _BatchedDevLLM()
    bp = compose_bid_blueprint(tender_p, client=llm, use_ai=True)
    sec_dev = next(s for s in bp.sections if s.section_id == 'sec_deviation')
    dev_rows = [b for b in sec_dev.content_blocks if b.get('kind') == 'deviation_row']
    assert len(dev_rows) == 16
    assert llm.dev_batch_sizes == [12, 4]
    # 确认第 13~16 条全部拥有 AI 草稿，不再遗留 [待人工核对]
    for r in dev_rows:
        assert '[AI草稿·待确认]' in r['bid_response'], r


# ── 3. 过滤须知/目录/计分规则噪音 + PDF 评分细则提取 ────────────────────────────

def test_e39_collect_topics_filters_procedural_noise_and_reads_pdf_scores():
    fake_checklist = {
        'scoring_tables': [],
        'scoring_hits': [
            {
                'excerpt': (
                    '评分办法及评分标准............................................... 22\n'
                    '4.2 投标人应认真阅读招标文件所有的事项、格式、条款和技术规范等。\n'
                    '5.1 任何要求对招标文件进行澄清的投标人，均应以书面形式通知采购代理机构。\n'
                    '6.1 在投标截止期十五日前，采购人可以对已发出的招标文件进行澄清修改。\n'
                    '2、计分方法：将各评标委员对同一投标单位的得分合计后取平均值。\n'
                )
            },
            {
                'excerpt': (
                    '评分细则：\n'
                    '序号\n评分因素\n评价指标和分值\n'
                    '1\n价格\n（30 分）\n评标基准价...\n'
                    '2\n技术\n（55 分）\n'
                    '实施方案\n0-9 分\n实施方案内容完整详细，可行性高得9分。\n'
                    '售后服务\n方案\n0-12 分\n提供7x24小时售后响应与应急预案。\n'
                    '项目进度\n计划\n0-11 分\n里程碑节点清晰科学得11分。\n'
                )
            },
        ],
    }
    topics = _collect_scoring_topics(fake_checklist, '')
    titles_and_quotes = ' '.join(f"{t['title']} {t['tender_quote']}" for t in topics)
    assert '认真阅读' not in titles_and_quotes
    assert '书面形式通知采购代理机构' not in titles_and_quotes
    assert '计分方法' not in titles_and_quotes
    assert '实施方案' in titles_and_quotes
    assert '售后服务方案' in titles_and_quotes
    assert '项目进度计划' in titles_and_quotes


# ── 4. 证照同名带角色合并去重 + 总览索引与独立附页真实嵌图 ────────────────────

def test_e39_cert_role_merge_and_subpage_image_embedding(tmp_path: Path):
    tender_p = _make_docx(
        tmp_path / 't_certs.docx',
        paragraphs=[
            '项目名称：证照合并与实图嵌入验收项目',
            '第三章 资格要求：投标人须提供营业执照。',
        ],
    )
    biz_png = _make_png(tmp_path / 'biz_license.png')
    checklist = {
        'chapters': [],
        'marked_clauses': [],
        'invalid_bid_clauses': [],
        'scoring_tables': [],
        'scoring_hits': [],
        'required_certificates': [
            {'cert_name': '营业执照', 'requirement_level': 'mandatory', 'quote': '提供营业执照'},
            {'cert_name': '环境标志产品认证证书', 'requirement_level': 'mandatory', 'quote': '必交环境标志证书'},
            {'cert_name': '财务审计报告', 'requirement_level': 'mandatory', 'quote': '财务审计报告'},
            {'cert_name': 'ISO27001认证证书', 'requirement_level': 'bonus', 'quote': '加分项ISO27001'},
            {'cert_name': '环境标志产品认证证书', 'requirement_level': 'bonus', 'quote': '评分加分环境标志证书'},
        ],
    }
    ledger_certs = [
        {
            'id': 11,
            'cert_name': '企业法人营业执照',
            'cert_no': '91110000MA012345',
            'is_long_term': True,
            'valid_until': date(9999, 12, 31),
            'has_attachment': True,
            'attachment_name': '营业执照扫描件.png',
            'attachment_path': str(biz_png),
        },
        {
            'id': 12,
            'cert_name': '财务审计报告',
            'cert_no': 'AUDIT-2025',
            'is_long_term': False,
            'valid_until': date(2028, 12, 31),
            'has_attachment': True,
            'attachment_name': '2025审计报告.pdf',
            'attachment_path': str(tmp_path / 'audit.pdf'),
        },
    ]

    bp = compose_bid_blueprint(tender_p, ledger_certs=ledger_certs, checklist=checklist, use_ai=False)
    sec_q = next(s for s in bp.sections if s.section_id == 'sec_qual')
    cert_rows = [b for b in sec_q.content_blocks if b.get('kind') == 'cert_row']

    # `环境标志产品认证证书` 两条去重合并为 1 条并保留双重角色标签
    env_rows = [r for r in cert_rows if '环境标志' in r['cert_name']]
    assert len(env_rows) == 1
    assert env_rows[0]['requirement_level'] == 'mandatory'
    assert env_rows[0]['role_label'] == '必交资格 / 加分参考'
    assert len(cert_rows) == 4

    out_docx = export_composed_bid_docx(bp, tmp_path / 'qual_subpages.docx')
    doc = Document(str(out_docx))
    h2_titles = [p.text for p in doc.paragraphs if p.style.name == 'Heading 2']
    assert '2.0 资格与资质证明文件总览索引表' in h2_titles
    assert any(t.startswith('2.1 ') for t in h2_titles)
    assert any('环境标志产品认证证书' in t for t in h2_titles)

    full_txt = _collect_docx_text(out_docx)
    assert '必交资格 / 加分参考' in full_txt
    assert '2025审计报告.pdf' in full_txt
    assert '原件扫描件 / 复印件粘贴区' in full_txt

    # 确认 PNG 扫描件已真实嵌入 .docx 的 word/media/ 目录
    with zipfile.ZipFile(str(out_docx), 'r') as zf:
        media_files = [n for n in zf.namelist() if n.startswith('word/media/')]
        assert len(media_files) >= 1


# ── 5. API 端到端：台账真图片上传 + 生成嵌图 .docx + JSON 不泄露宿主机路径 ────

def test_e39_api_real_cert_image_embed_and_sanitize_host_path(tmp_path: Path):
    db_file = tmp_path / 'e39_api.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    tender_p = _make_docx(
        tmp_path / 'e39_api_tender.docx',
        paragraphs=[
            '项目名称：API实图嵌入安全性检查项目',
            '第三章 资格要求：投标人须提供有效企业法人营业执照与ISO27001认证证书。',
        ],
    )
    png_bytes = _make_png(tmp_path / 'api_biz.png').read_bytes()

    with TestClient(app) as tc:
        admin_h = _login_headers(tc, 'admin')
        # 1) 创建长期有效营业执照并上传 PNG 原件
        r_cert = tc.post(
            '/api/rules/certificates',
            headers=admin_h,
            json={'cert_name': '企业法人营业执照', 'cert_no': 'LIC-E39', 'is_long_term': True},
        )
        assert r_cert.status_code == 200, r_cert.text
        cid = r_cert.json()['id']

        r_att = tc.post(
            f'/api/rules/certificates/{cid}/attachment',
            headers=admin_h,
            files={'file': ('api_biz.png', png_bytes, 'image/png')},
        )
        assert r_att.status_code == 200, r_att.text

        # 2) 生成标书初稿（0-LLM）
        r_gen = tc.post(
            '/api/composer/generate',
            headers=admin_h,
            files=[('tender_file', ('e39_api_tender.docx', tender_p.read_bytes(), 'application/octet-stream'))],
            data={'use_ai': 'false', 'title': 'E39实图嵌入测试稿'},
        )
        assert r_gen.status_code == 200, r_gen.text
        body = r_gen.json()
        did = body['id']

        # 断言接口 JSON 中标记 has_embedded_image=True，但绝不包含服务器物理 attachment_path 路径
        serialized = json.dumps(body, ensure_ascii=False)
        assert 'attachment_path' not in serialized
        assert 'data/uploads/certificates' not in serialized
        sec_q = next(s for s in body['blueprint']['sections'] if s['section_id'] == 'sec_qual')
        biz_blk = next(b for b in sec_q['content_blocks'] if '营业执照' in b['cert_name'])
        assert biz_blk['has_embedded_image'] is True

        # 3) 下载导出 .docx 确实内置了该 PNG 图片
        r_dl = tc.get(f'/api/composer/drafts/{did}/download', headers=admin_h)
        assert r_dl.status_code == 200
        with zipfile.ZipFile(BytesIO(r_dl.content), 'r') as zf:
            assert any(n.startswith('word/media/') for n in zf.namelist())

        # 4) 清理测试数据
        assert tc.delete(f'/api/composer/drafts/{did}', headers=admin_h).status_code == 200
        assert tc.delete(f'/api/rules/certificates/{cid}', headers=admin_h).status_code == 200
