# -*- coding: utf-8 -*-
"""E24 · F7 标书专家多文件与图片附件问答卡专项单元测试（0 Token 离线可跑，断言严格数数）。

覆盖任务卡 §3 全部 7 组验收标准：
1. OCR 低门槛与 VLM 内存缩图：
   - 少于 100 字的 PNG/JPG 走 parse_qa_attachment 不抛异常并生成 .ocr.txt，原 parse_document 100 字限制零回归；
   - 超 2048px 大图走 encode_image_for_vlm 后等比缩放为 JPEG 且磁盘原图字节零改动；
2. 上传校验、配额与级联清理：
   - 非法后缀 / 空文件 / 单文件 >20MB / 单会话 >10 个 / 单轮 >4 张图均返回对应 422 业务错误码；
   - 删除单附件与删除会话均彻底清理磁盘原文件及 .ocr.txt 伴生缓存；
3. 老库平滑加列与模型配置：
   - 构造缺 qa_sessions.attachments_json 与 user_model_configs.supports_vision 的旧 SQLite 库验证自动补列成功；
   - /api/model-config 与 worker.resolve_effective_llm_client 正确透传 supports_vision；
4. 多模态网关与拒图熔断重试：
   - supports_vision=True 时发送 image_url，history 不重发历史图片；
   - 模拟上游拒图 400 验证剥图重试与 _mark_vision_unsupported() 单实例熔断，且不误触发 _is_thinking_reject、不吞 401/429/500；
5. ReAct 第 7 工具 search_attachment 与 0-Token 降级：
   - 0-Token 模式与在线 ReAct 模式均可检索补充附件并输出准确 offset、attachment_id 与 attachment_name；
6. 反幻觉专项拒收：
   - /ask 传入伪造或跨会话 attachment_id 返回 HTTP 422 INVALID_ATTACHMENT_ID；
   - LLM 编造不存在的 attachment_id 或虚假引文被 _verify_and_locate_quotes 拦截标 quote_verified=False；
   - 纯图视觉结论保留正文且显式标 quote_verified=False；
7. ReAct JSON 容错、禁吐工具 JSON 与 after_offset 续检：
   - 含未转义真实换行的 JSON 字符串被 _parse_llm_json(strict=False) 成功解析；
   - 无法解析的残缺工具 JSON 绝不吐进最终 answer；
   - after_offset > 0 能越过前 5 条命中继续检索后段高频条款。
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

from docx import Document
from fastapi.testclient import TestClient
import pymupdf
import pytest
from sqlalchemy import create_engine, inspect

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.auth import hash_password
import backend.database as db_mod
from backend.database import PROJECT_ROOT, get_engine, get_session_factory
from backend.main import create_app
from backend.models import (
    QaSession,
    Tenant,
    User,
    UserModelConfig,
    ensure_qa_sessions_columns,
    ensure_user_model_configs_columns,
)
from backend.worker import resolve_effective_llm_client
from core.bid_react_agent import (
    _parse_llm_json,
    ask_bid_expert,
    locate_chapter,
    search_attachment,
    search_tender,
)
from core.llm_gateway import (
    LLMClient,
    _estimate_messages_prompt_chars,
    _is_thinking_reject,
    _is_vision_reject,
    build_client_from_user_config,
    encode_image_for_vlm,
)
from core.parser import parse_document, parse_qa_attachment


def _make_docx_bytes(lines: list[str]) -> bytes:
    doc = Document()
    for ln in lines:
        doc.add_paragraph(ln)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _make_png_bytes(width: int = 320, height: int = 120, text: str = '') -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=width, height=height)
    if text:
        page.insert_text((16, 60), text, fontsize=18)
    pix = page.get_pixmap(alpha=False)
    png_bytes = pix.tobytes(output='png')
    doc.close()
    return png_bytes


# ── 1. OCR 低门槛与 VLM 内存缩图（原图零改动 & parse_document 100 字限制零回归）──

def test_e24_low_threshold_ocr_and_vlm_in_memory_resize(tmp_path):
    # (a) 空白或短字数 PNG：parse_document 仍抛 <100 字 ValueError，而 parse_qa_attachment 正常返回且写 .ocr.txt
    short_png = tmp_path / 'seal_short.png'
    short_png.write_bytes(_make_png_bytes(240, 100, text='OK'))

    with pytest.raises(ValueError, match='字'):
        parse_document(str(short_png))

    att_res = parse_qa_attachment(short_png)
    assert att_res['is_image'] is True
    assert att_res['image_width'] == 240 and att_res['image_height'] == 100
    assert Path(str(short_png) + '.ocr.txt').is_file()

    # (b) 超 2048px 大图：encode_image_for_vlm 在内存中缩图并重编码 JPEG，磁盘原图字节数保持一字不动
    huge_png = tmp_path / 'huge_cert.png'
    orig_bytes = _make_png_bytes(2600, 1800, text='CERT-2026')
    huge_png.write_bytes(orig_bytes)
    disk_size_before = huge_png.stat().st_size

    vlm_info = encode_image_for_vlm(huge_png, max_edge=2048)
    assert vlm_info['resized'] is True
    assert max(vlm_info['width'], vlm_info['height']) <= 2048
    assert vlm_info['data_url'].startswith('data:image/jpeg;base64,')
    assert huge_png.stat().st_size == disk_size_before


# ── 2 & 6a. 附件上传校验、配额管控、伪造 ID 拒收与级联清理 ──────────────────

def test_e24_attachment_quotas_validation_and_cascade_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'test_e24_api.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    t_bytes = _make_docx_bytes(['第一章 招标要求', '★1. 质保期不少于36个月。'])
    b_bytes = _make_docx_bytes(['第一章 投标响应', '★1. 我方承诺质保期36个月。'])

    with TestClient(app) as tc:
        r_login = tc.post('/api/auth/login', json={'username': 'admin', 'password': 'BidGuard2026!'})
        hdrs = {'Authorization': f"Bearer {r_login.json()['token']}"}

        r_task = tc.post(
            '/api/checks',
            headers=hdrs,
            files={
                'tender_file': ('t.docx', t_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                'bid_file': ('b.docx', b_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
            },
            data={'title': 'E24附件配额测试', 'auto_run': 'false'},
        )
        tid = r_task.json()['id']
        r_sess = tc.post('/api/qa/sessions', headers=hdrs, json={'task_id': tid})
        sid = r_sess.json()['id']

        # ① 非法后缀 -> 422 UNSUPPORTED_FILE_TYPE
        r_bad_ext = tc.post(
            f"/api/qa/sessions/{sid}/attachments",
            headers=hdrs,
            files={'file': ('evil.exe', b'MZ123', 'application/octet-stream')},
        )
        assert r_bad_ext.status_code == 422
        assert r_bad_ext.json()['detail']['code'] == 'UNSUPPORTED_FILE_TYPE'

        # ② 空文件 -> 422 EMPTY_FILE
        r_empty = tc.post(
            f"/api/qa/sessions/{sid}/attachments",
            headers=hdrs,
            files={'file': ('empty.txt', b'', 'text/plain')},
        )
        assert r_empty.status_code == 422
        assert r_empty.json()['detail']['code'] == 'EMPTY_FILE'

        # ③ 超过单文件上限 -> 422 FILE_TOO_LARGE
        import backend.routers.qa as qa_mod
        monkeypatch.setattr(qa_mod, '_QA_MAX_SINGLE_FILE_BYTES', 1024)
        r_large = tc.post(
            f"/api/qa/sessions/{sid}/attachments",
            headers=hdrs,
            files={'file': ('big.txt', b'a' * 2048, 'text/plain')},
        )
        assert r_large.status_code == 422
        assert r_large.json()['detail']['code'] == 'FILE_TOO_LARGE'
        monkeypatch.setattr(qa_mod, '_QA_MAX_SINGLE_FILE_BYTES', 20 * 1024 * 1024)

        # ④ 正常上传 5 张图片 + 1 份补遗 md，验证单轮 >4 图拦截、伪造 ID 拦截、流式端点校验、配额上限及物理清理
        img_bytes = _make_png_bytes(200, 80)
        img_ids: list[str] = []
        for i in range(5):
            r_up = tc.post(
                f"/api/qa/sessions/{sid}/attachments",
                headers=hdrs,
                files={'file': (f"img_{i}.png", img_bytes, 'image/png')},
            )
            assert r_up.status_code == 200
            img_ids.append(r_up.json()['attachment_id'])

        r_md = tc.post(
            f"/api/qa/sessions/{sid}/attachments",
            headers=hdrs,
            files={'file': ('补遗说明.md', '# 补充通知\n交货期调整为45天。'.encode('utf-8'), 'text/markdown')},
        )
        assert r_md.status_code == 200
        md_id = r_md.json()['attachment_id']

        # 单轮传 5 张图 -> 422 TOO_MANY_IMAGES_PER_TURN
        r_too_many_imgs = tc.post(
            f"/api/qa/sessions/{sid}/ask",
            headers=hdrs,
            json={'question': '请看这5张图', 'attachment_ids': img_ids},
        )
        assert r_too_many_imgs.status_code == 422
        assert r_too_many_imgs.json()['detail']['code'] == 'TOO_MANY_IMAGES_PER_TURN'

        # 传入伪造 attachment_id（同步与流式接口均应提前拦截）-> 422 INVALID_ATTACHMENT_ID
        r_fake_id = tc.post(
            f"/api/qa/sessions/{sid}/ask",
            headers=hdrs,
            json={'question': '测试伪造ID', 'attachment_ids': ['att_fake_999999']},
        )
        assert r_fake_id.status_code == 422
        assert r_fake_id.json()['detail']['code'] == 'INVALID_ATTACHMENT_ID'

        r_fake_stream = tc.post(
            f"/api/qa/sessions/{sid}/ask/stream",
            headers=hdrs,
            json={'question': '流式伪造ID', 'attachment_ids': ['att_fake_999999']},
        )
        assert r_fake_stream.status_code == 422
        assert r_fake_stream.json()['detail']['code'] == 'INVALID_ATTACHMENT_ID'

        # 合法附件走 SSE /ask/stream 正常返回并持久化附件摘要
        with tc.stream(
            'POST',
            f"/api/qa/sessions/{sid}/ask/stream",
            headers=hdrs,
            json={'question': '补遗说明里交货期多少天？', 'attachment_ids': [md_id]},
        ) as r_sse:
            assert r_sse.status_code == 200
            sse_body = ''.join(r_sse.iter_text())
        assert 'event: done' in sse_body
        assert '45天' in sse_body

        # ④b 跨租户上传/删除同一会话 -> 404 QA_SESSION_NOT_FOUND
        with db_mod.SessionLocal() as db_s:
            t2 = Tenant(name='tenant_other_e24')
            db_s.add(t2)
            db_s.flush()
            u2 = User(
                tenant_id=t2.id,
                username='other_admin',
                password_hash=hash_password('OtherPass2026!'),
                display_name='他租户管理员',
                role='admin',
            )
            db_s.add(u2)
            db_s.commit()
        r_login2 = tc.post('/api/auth/login', json={'username': 'other_admin', 'password': 'OtherPass2026!'})
        hdrs2 = {'Authorization': f"Bearer {r_login2.json()['token']}"}
        assert tc.post(
            f"/api/qa/sessions/{sid}/attachments",
            headers=hdrs2,
            files={'file': ('x.txt', b'hello world', 'text/plain')},
        ).status_code == 404
        assert tc.delete(
            f"/api/qa/sessions/{sid}/attachments/{img_ids[0]}",
            headers=hdrs2,
        ).status_code == 404

        # ④c 单会话累计体积 >100MB（monkeypatch 上限模拟）-> 422 ATTACHMENT_LIMIT_EXCEEDED
        monkeypatch.setattr(qa_mod, '_QA_MAX_SESSION_TOTAL_BYTES', 5000)
        r_total_cap = tc.post(
            f"/api/qa/sessions/{sid}/attachments",
            headers=hdrs,
            files={'file': ('over_total.txt', b'x' * 5001, 'text/plain')},
        )
        assert r_total_cap.status_code == 422
        assert r_total_cap.json()['detail']['code'] == 'ATTACHMENT_LIMIT_EXCEEDED'
        monkeypatch.setattr(qa_mod, '_QA_MAX_SESSION_TOTAL_BYTES', 100 * 1024 * 1024)

        # ④d 单会话附件个数 >10 个 -> 422 ATTACHMENT_LIMIT_EXCEEDED
        # 当前已有 5 张图 + 1 个 md = 6 个，再传 4 个达上限 10，第 11 个拒绝
        for j in range(4):
            r_fill = tc.post(
                f"/api/qa/sessions/{sid}/attachments",
                headers=hdrs,
                files={'file': (f"extra_{j}.txt", f"补充文本{j}".encode('utf-8'), 'text/plain')},
            )
            assert r_fill.status_code == 200
        r_over_cnt = tc.post(
            f"/api/qa/sessions/{sid}/attachments",
            headers=hdrs,
            files={'file': ('extra_11th.txt', b'11th file', 'text/plain')},
        )
        assert r_over_cnt.status_code == 422
        assert r_over_cnt.json()['detail']['code'] == 'ATTACHMENT_LIMIT_EXCEEDED'

        # ⑤ 删除单附件与删除会话：断言磁盘物理文件与 .ocr.txt 伴生缓存彻底清理
        sess_dir = (PROJECT_ROOT / 'data' / 'uploads' / 'qa' / str(r_login.json()['user']['tenant_id']) / str(sid)).resolve()
        del_img_path = sess_dir / f"{img_ids[0]}.png"
        del_img_ocr = Path(str(del_img_path) + '.ocr.txt')
        assert del_img_path.is_file()
        assert del_img_ocr.is_file()

        r_del_one = tc.delete(f"/api/qa/sessions/{sid}/attachments/{img_ids[0]}", headers=hdrs)
        assert r_del_one.status_code == 200
        assert not del_img_path.exists()
        assert not del_img_ocr.exists()

        r_del_sess = tc.delete(f"/api/qa/sessions/{sid}", headers=hdrs)
        assert r_del_sess.status_code == 200
        assert not sess_dir.exists()


# ── 3. 老库平滑加列与 supports_vision 透传验证 ─────────────────────────────

def test_e24_legacy_db_auto_migration_and_supports_vision_passthrough(tmp_path, monkeypatch):
    monkeypatch.delenv('BIDGUARD_FORCE_ZERO_TOKEN', raising=False)
    legacy_db = tmp_path / 'legacy_e24.db'
    conn = sqlite3.connect(str(legacy_db))
    conn.execute(
        """
        CREATE TABLE qa_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL,
            user_id INTEGER,
            task_id INTEGER,
            title VARCHAR(256) NOT NULL DEFAULT '新对话',
            tender_path VARCHAR(512),
            bid_path VARCHAR(512),
            messages_json JSON NOT NULL DEFAULT '[]',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE user_model_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            base_url VARCHAR(256) NOT NULL,
            api_key_encrypted TEXT NOT NULL,
            model_flash VARCHAR(128) NOT NULL,
            model_flagship VARCHAR(128),
            supports_reasoning_effort BOOLEAN NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        INSERT INTO qa_sessions (tenant_id, title, messages_json)
        VALUES (1, '旧版老库会话', '[]')
        """
    )
    conn.commit()
    conn.close()

    # 先用裸 create_engine 验证 ensure_* 函数本身探测并平滑加列（不依赖 connect PRAGMA 旁路）
    raw_eng = create_engine(f"sqlite:///{legacy_db.as_posix()}")
    try:
        added_qa = ensure_qa_sessions_columns(raw_eng)
        added_mc = ensure_user_model_configs_columns(raw_eng)
        assert 'attachments_json' in added_qa
        assert 'supports_vision' in added_mc
        assert ensure_qa_sessions_columns(raw_eng) == []
        assert ensure_user_model_configs_columns(raw_eng) == []
    finally:
        raw_eng.dispose()

    # 再用项目 get_engine 读取老库旧行，确认 ORM 字段可正常读写且无缺列报错
    eng = get_engine(f"sqlite:///{legacy_db.as_posix()}")
    try:
        insp = inspect(eng)
        assert 'attachments_json' in {c['name'] for c in insp.get_columns('qa_sessions')}
        assert 'supports_vision' in {c['name'] for c in insp.get_columns('user_model_configs')}
        with get_session_factory(eng)() as s:
            old_sess = s.query(QaSession).filter_by(id=1).first()
            assert old_sess is not None and old_sess.attachments_json == []
    finally:
        eng.dispose()

    # 通过完整 FastAPI 路由 /api/model-config 存取 + worker.resolve_effective_llm_client 验证透传
    api_db = tmp_path / 'model_cfg_vision.db'
    app = create_app(database_url=f"sqlite:///{api_db.as_posix()}", auto_init_db=True)
    with TestClient(app) as tc:
        r_login = tc.post('/api/auth/login', json={'username': 'admin', 'password': 'BidGuard2026!'})
        hdrs = {'Authorization': f"Bearer {r_login.json()['token']}"}
        uid = int(r_login.json()['user']['id'])

        r_put = tc.put(
            '/api/model-config',
            headers=hdrs,
            json={
                'base_url': 'http://10.0.2.81:3000/v1',
                'api_key': 'sk-vision-secret-1234',
                'model_flash': 'glm-4v-flash',
                'model_flagship': 'glm-4v-plus',
                'supports_reasoning_effort': False,
                'supports_vision': True,
            },
        )
        assert r_put.status_code == 200
        assert r_put.json()['supports_vision'] is True

        r_get = tc.get('/api/model-config', headers=hdrs)
        assert r_get.status_code == 200
        assert r_get.json()['supports_vision'] is True

        with db_mod.SessionLocal() as db_s:
            eff_client, status = resolve_effective_llm_client(db_s, user_id=uid)
            assert status == 'done'
            assert eff_client is not None
            assert eff_client.supports_vision is True


# ── 4. 多模态网关拒图剥离重试与单实例熔断（不误触发思考熔断、不吞 401/500）──

def test_e24_gateway_vision_reject_circuit_breaker_and_token_estimation(tmp_path, monkeypatch):
    monkeypatch.setattr('time.sleep', lambda _s: None)
    assert _is_vision_reject(RuntimeError("400 Bad Request: image_url is not supported by this model")) is True
    assert _is_vision_reject(RuntimeError("401 Unauthorized: invalid vision token")) is False
    assert _is_vision_reject(RuntimeError("429 Too Many Requests: vision quota exceeded")) is False
    assert _is_vision_reject(RuntimeError("500 Internal Server Error")) is False
    assert _is_thinking_reject(RuntimeError("400 Bad Request: image_url is not supported")) is False

    # HTTP 401/500 不应被吞，也不得错误触发 supports_vision 熔断
    err_client = build_client_from_user_config(
        base_url='http://10.0.2.81:3000/v1',
        api_key='sk-test',
        model_flash='glm-4v-flash',
        supports_vision=True,
    )

    class _Http401Completions:
        def create(self, **kwargs):
            exc = RuntimeError("401 Unauthorized: invalid api_key for vision")
            exc.status_code = 401
            raise exc

    err_client.client = SimpleNamespace(chat=SimpleNamespace(completions=_Http401Completions()))
    err_client.use_openai = True
    with pytest.raises(RuntimeError, match='401'):
        err_client.chat([{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAA'}}]}], model_tier='flash')
    assert err_client.supports_vision is True

    client = build_client_from_user_config(
        base_url='http://10.0.2.81:3000/v1',
        api_key='sk-test',
        model_flash='glm-4v-flash',
        supports_reasoning_effort=True,
        supports_vision=True,
    )
    sent_messages_log: list[list[dict]] = []

    class _RejectVisionCompletions:
        def create(self, **kwargs):
            msgs = kwargs.get('messages') or []
            sent_messages_log.append(msgs)
            for m in msgs:
                if isinstance(m.get('content'), list):
                    raise RuntimeError("400 Bad Request: image_url is not supported by model")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='纯文本降级成功'))],
                usage=SimpleNamespace(prompt_tokens=12, completion_tokens=4),
            )

    client.client = SimpleNamespace(chat=SimpleNamespace(completions=_RejectVisionCompletions()))
    client.use_openai = True

    huge_b64_url = 'data:image/png;base64,' + ('A' * 20000)
    multimodal_msgs = [
        {
            'role': 'user',
            'content': [
                {'type': 'text', 'text': '请看这张营业执照'},
                {'type': 'image_url', 'image_url': {'url': huge_b64_url}},
            ],
        }
    ]
    # 安全字符估算应远低于 len(base64)
    assert _estimate_messages_prompt_chars(multimodal_msgs) < 1000

    # 第 1 次调用：遇 400 拒图自动剥离 image_url 重试成功，且熔断 supports_vision=False，但不影响 supports_reasoning_effort
    r1 = client.chat(multimodal_msgs, model_tier='flash')
    assert r1.content == '纯文本降级成功'
    assert client.supports_vision is False
    assert client.supports_reasoning_effort is True
    assert len(sent_messages_log) == 2
    assert isinstance(sent_messages_log[1][0]['content'], str)

    # 同实例第 2 次调用：直接剥离 image_url 发送，不再撞墙
    r2 = client.chat(multimodal_msgs, model_tier='flash')
    assert r2.content == '纯文本降级成功'
    assert len(sent_messages_log) == 3
    assert isinstance(sent_messages_log[2][0]['content'], str)

    # chat_stream 无 usage 时估算 prompt_tokens 应使用 _estimate_messages_prompt_chars（绝非 len(base64)//2）
    stream_client = build_client_from_user_config(
        base_url='http://10.0.2.81:3000/v1',
        api_key='sk-test',
        model_flash='glm-4v-flash',
        supports_vision=True,
    )

    class _VisionStreamCompletions:
        def create(self, **kwargs):
            return iter([
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='看图'))], usage=None),
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='完成'))], usage=None),
            ])

    stream_client.client = SimpleNamespace(chat=SimpleNamespace(completions=_VisionStreamCompletions()))
    stream_client.use_openai = True
    events = list(stream_client.chat_stream(multimodal_msgs, model_tier='flash'))
    final_ev = [e for e in events if e['type'] == 'final'][0]
    assert final_ev['response'].prompt_tokens < 1000

    # 历史多轮传参测试：supports_vision=True 时，仅当轮末尾携带 image_url，history 不重发历史图片
    t_p = tmp_path / 't_vlm.docx'
    b_p = tmp_path / 'b_vlm.docx'
    img_p = tmp_path / 'seal_vlm.png'
    t_p.write_bytes(_make_docx_bytes(['第一章 招标公告：质保36个月。']))
    b_p.write_bytes(_make_docx_bytes(['第一章 投标文件：质保36个月。']))
    img_p.write_bytes(_make_png_bytes(200, 80, text='SEAL'))
    captured_msgs: list[list[dict]] = []

    class _InspectMessagesClient:
        supports_vision = True
        supports_reasoning_effort = False

        def chat(self, messages, model_tier='flash', reasoning_effort=None):
            captured_msgs.append(messages)
            return SimpleNamespace(content=json.dumps({
                'thought': '已看图',
                'action': 'final',
                'action_input': {'answer': '当前轮图片已查看。', 'quotes': []},
            }))

    ask_bid_expert(
        t_p,
        b_p,
        '当前图片有公章吗？',
        client=_InspectMessagesClient(),
        history=[
            {
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': '上一轮请看图1'},
                    {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,OLDIMG'}},
                ],
                'attachments': [{'attachment_id': 'old_img', 'filename': 'old.png', 'is_image': True}],
            },
            {'role': 'assistant', 'content': '上一轮已查看图1。'},
        ],
        attachments=[{
            'attachment_id': 'cur_img',
            'filename': 'seal_vlm.png',
            'file_path': str(img_p),
            'text': '',
            'is_image': True,
            'ocr_chars': 0,
            'has_text': False,
        }],
    )
    sent = captured_msgs[0]
    # system + 2 条 history(纯文本 str) + 1 条当前 user(含 image_url list)
    assert isinstance(sent[1]['content'], str) and '上一轮请看图1' in sent[1]['content']
    assert isinstance(sent[-1]['content'], list)
    assert any(b.get('type') == 'image_url' for b in sent[-1]['content'])


# ── 5 & 6b. 第 7 工具 search_attachment、0-Token 降级与反幻觉溯源校验 ──────

def test_e24_search_attachment_and_anti_hallucination_quote_verification(tmp_path):
    t_path = tmp_path / 't.docx'
    b_path = tmp_path / 'b.docx'
    att_doc_path = tmp_path / '补遗通知01号.txt'
    att_img_path = tmp_path / '公章扫描件.png'

    t_path.write_bytes(_make_docx_bytes(['第一章 招标公告：原定交货期30天。']))
    b_path.write_bytes(_make_docx_bytes(['第一章 投标响应：我方响应交货期20天。']))
    att_doc_path.write_text('重要更正：本项目交货期由30天变更为45个日历天，特此补遗。', encoding='utf-8')
    att_img_path.write_bytes(_make_png_bytes(200, 80))

    runtime_atts = [
        {
            'attachment_id': 'att_doc_1',
            'filename': '补遗通知01号.txt',
            'file_path': str(att_doc_path),
            'text': att_doc_path.read_text(encoding='utf-8'),
            'is_image': False,
            'ocr_chars': 0,
            'has_text': True,
        },
        {
            'attachment_id': 'att_img_1',
            'filename': '公章扫描件.png',
            'file_path': str(att_img_path),
            'text': '',
            'is_image': True,
            'ocr_chars': 0,
            'has_text': False,
        },
    ]

    # (a) 0-Token 离线降级检索补充文档附件
    res_offline = ask_bid_expert(
        t_path, b_path, '补遗通知里交货期变更为多少天？', client=None, attachments=runtime_atts
    )
    assert '45个日历天' in res_offline['answer']
    att_quotes = [q for q in res_offline['quotes'] if q['source'] == 'attachment']
    assert len(att_quotes) >= 1
    assert att_quotes[0]['quote_verified'] is True
    assert att_quotes[0]['attachment_name'] == '补遗通知01号.txt'

    # (b) 在线 ReAct：同时验证真实附件引文通过、伪造 attachment_id 被清空、纯图视觉结论标 quote_verified=False
    class _AttachmentReActMockClient:
        def __init__(self):
            self.turn = 0

        def chat(self, messages, model_tier='flash', reasoning_effort=None):
            self.turn += 1
            if self.turn == 1:
                return SimpleNamespace(
                    content=json.dumps({
                        'thought': '先检索补充附件中的交货期变更说明',
                        'action': 'search_attachment',
                        'action_input': {'keyword': '交货期', 'attachment_id': 'att_doc_1'},
                    })
                )
            return SimpleNamespace(
                content=json.dumps({
                    'thought': '汇总附件原文与印章图片观察结果',
                    'action': 'final',
                    'action_input': {
                        'answer': '根据补遗通知交货期变更为45个日历天，且公章扫描件印章清晰。',
                        'quotes': [
                            {
                                'source': 'attachment',
                                'attachment_id': 'att_doc_1',
                                'quote': '重要更正：本项目交货期由30天变更为45个日历天，特此补遗。',
                            },
                            {
                                'source': 'attachment',
                                'attachment_id': 'att_hallucinated_999',
                                'quote': '这条引用来自不存在的伪造附件ID',
                            },
                            {
                                'source': 'attachment',
                                'attachment_id': 'att_doc_1',
                                'quote': '这条虚假引文在补遗通知原文中完全不存在',
                            },
                            {
                                'source': 'attachment',
                                'attachment_id': 'att_img_1',
                                'quote': '视觉观察：红色圆形公章加盖完整',
                            },
                        ],
                    },
                })
            )

    res_online = ask_bid_expert(
        t_path, b_path, '补遗通知交货期和公章情况如何？', client=_AttachmentReActMockClient(), attachments=runtime_atts
    )
    qs = res_online['quotes']
    assert len(qs) == 4
    # 第 1 条：真实文档附件子串 -> quote_verified=True
    assert qs[0]['quote_verified'] is True and qs[0]['attachment_name'] == '补遗通知01号.txt'
    # 第 2 条：伪造 attachment_id -> quote_verified=False 且 quote 被清空
    assert qs[1]['quote_verified'] is False and qs[1]['quote'] == ''
    # 第 3 条：真实附件 ID 但虚假引文 -> quote_verified=False 且 quote 被清空
    assert qs[2]['quote_verified'] is False and qs[2]['quote'] == ''
    # 第 4 条：纯图片视觉结论 -> quote_verified=False 且 offset=None
    assert qs[3]['quote_verified'] is False and qs[3]['offset'] is None
    assert '视觉识别结论' in qs[3]['note']


# ── 7. ReAct JSON 换行容错、禁吐工具 JSON 与 after_offset 续检验证 ──────────

def test_e24_react_json_unescaped_newline_tolerance_no_raw_json_leak_and_after_offset(tmp_path):
    # (a) JSON 字符串字面量内含未转义真实换行 "\n" -> _parse_llm_json(strict=False) 成功解析
    raw_with_newline = '{"thought": "检索星号条款",\n"action": "search_tender",\n"action_input": "*\n*"}'
    parsed = _parse_llm_json(raw_with_newline)
    assert parsed is not None
    assert parsed['action'] == 'search_tender'

    # (b) after_offset > 0 越过前 5 处总则命中，继续检索后文第 6 处真正条款
    long_tender_text = '\n'.join(
        [f"总则说明第{i}处：标★项为实质性要求（间隔填充文本{'甲乙丙丁' * 60}）。" for i in range(1, 6)]
        + ["第四章 技术需求：★6. 核心数据库须支持分布式事务强一致性。"]
    )
    first_batch = search_tender('★', tender_text=long_tender_text, after_offset=0)
    assert len(first_batch) == 5
    assert all('★6.' not in h['quote'] for h in first_batch)

    next_cursor = first_batch[-1]['next_after_offset']
    second_batch = search_tender('★', tender_text=long_tender_text, after_offset=next_cursor)
    assert len(second_batch) == 1
    assert '★6. 核心数据库须支持分布式事务强一致性' in second_batch[0]['quote']

    # locate_chapter(..., after_offset=...) 跳过前章命中后文对应章节
    chap_doc = "第一章 技术总则\n说明\n" + ('填充' * 50) + "\n第四章 技术需求正文\n★具体要求"
    ch_all = locate_chapter(chap_doc, '技术', after_offset=0)
    assert len(ch_all) >= 2
    ch_later = locate_chapter(chap_doc, '技术', after_offset=ch_all[0]['start'] + 5)
    assert len(ch_later) == 1 and ch_later[0]['index'] == '第四章' and '技术需求正文' in ch_later[0]['title']

    # (c) 残缺工具 JSON 绝不原样吐进最终 answer（第 1 轮吐出残缺工具 JSON 触发自动纠偏，第 2 轮正常收口）
    t_path = tmp_path / 't_json.docx'
    b_path = tmp_path / 'b_json.docx'
    t_path.write_bytes(_make_docx_bytes(['★1. 质保期36个月。']))
    b_path.write_bytes(_make_docx_bytes(['★1. 质保期36个月。']))

    class _BrokenJsonThenRecoverClient:
        def __init__(self):
            self.turn = 0

        def chat(self, messages, model_tier='flash', reasoning_effort=None):
            self.turn += 1
            if self.turn == 1:
                # 故意返回少右花括号的残缺工具 JSON
                return SimpleNamespace(
                    content='{"thought": "准备检索", "action": "search_tender", "action_input": "质保期"'
                )
            return SimpleNamespace(
                content=json.dumps({
                    'thought': '已修正JSON格式并给出结论',
                    'action': 'final',
                    'action_input': {
                        'answer': '免费质保期为36个月。',
                        'quotes': ['★1. 质保期36个月。'],
                    },
                })
            )

    res = ask_bid_expert(t_path, b_path, '质保期多久？', client=_BrokenJsonThenRecoverClient())
    assert '免费质保期为36个月' in res['answer']
    assert '"action_input"' not in res['answer']

    # 全轮皆吐残缺工具 JSON 时严禁裸露原始 JSON
    class _BrokenJsonAlwaysClient:
        def chat(self, messages, model_tier='flash', reasoning_effort=None):
            return SimpleNamespace(
                content='{"thought": "卡住了", "action": "search_tender", "action_input": "*\n'
            )

    res_exhausted = ask_bid_expert(t_path, b_path, '星号要求？', client=_BrokenJsonAlwaysClient(), max_rounds=2)
    assert '"action"' not in res_exhausted['answer']
    assert '"action_input"' not in res_exhausted['answer']
