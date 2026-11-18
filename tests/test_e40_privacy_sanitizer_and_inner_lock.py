# -*- coding: utf-8 -*-
"""E40 · 合规底座双锁卡（LLM 三出口统一脱敏 + 内网模型防外泄锁定 + 上传零 OCR 统计）全链路验收测试。"""
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from docx import Document
from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.main import create_app
from backend.worker import resolve_effective_llm_client
from core.bid_react_agent import ask_bid_expert
from core.llm_gateway import (
    LLMClient,
    build_client_from_user_config,
    get_llm_client,
    is_inner_model_url_allowed,
)
from core.privacy_sanitizer import (
    is_valid_cn_id_card,
    sanitize_chat_messages,
    sanitize_text,
    scan_document_file,
    scan_document_text,
)
from core.redline import inject_word_comments
from core.response_checker import run_response_check


def _make_docx(path: Path, paragraphs: list[str]) -> Path:
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    doc.save(str(path))
    return path


def _login_headers(client: TestClient, username: str, password: str = 'BidGuard2026!') -> dict[str, str]:
    resp = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert resp.status_code == 200, resp.text
    return {'Authorization': f"Bearer {resp.json()['token']}"}


# ── 1. core/privacy_sanitizer：GB 11643 校验、银行语境约束、等长掩码与幂等 ───────

def test_e40_id_card_gb11643_and_bank_context_equal_len_idempotent():
    # 合法 18 位身份证号（GB 11643 校验位正确）
    valid_id_x = '11010519491231002X'
    valid_id_6 = '310104199005121237'
    assert is_valid_cn_id_card(valid_id_x) is True
    assert is_valid_cn_id_card(valid_id_6) is True

    # 错误校验位 & 非法日期 02 月 30 日 绝不误判
    assert is_valid_cn_id_card('310104199005121234') is False
    assert is_valid_cn_id_card('31010419900230123X') is False

    sample = (
        f"法定代表人身份证号：{valid_id_x}，授权代表身份证：{valid_id_6}。\n"
        "公司开户银行：工商银行海淀支行，对公银行账号：6222 0210 0111 2223 334。\n"
        "备用收款账户：1100223344556677。\n"
        "干扰项测试：项目编号：202601010000123456（银行项目），标准号 GB/T 22239-2019，"
        "银行保函金额：123456789012.00元，联系电话：13812345678，校验错号：310104199005121234。"
    )
    masked, stats = sanitize_text(sample)
    assert stats == {'id_masked': 2, 'bank_masked': 2}
    # 严格等长掩码：原串与脱敏串字符长度 100% 相等
    assert len(masked) == len(sample)
    assert valid_id_x not in masked and valid_id_6 not in masked
    assert '110***********002X' in masked
    assert '310***********1237' in masked
    assert '6222 0210 0111 2223 334' not in masked
    assert '6222 **** **** ***3 334' in masked
    assert '1100********6677' in masked

    # 干扰项完全无损保留
    assert '202601010000123456' in masked
    assert 'GB/T 22239-2019' in masked
    assert '123456789012.00元' in masked
    assert '13812345678' in masked
    assert '310104199005121234' in masked

    # 严格幂等：二次脱敏计数归零且文本不变
    masked2, stats2 = sanitize_text(masked)
    assert masked2 == masked
    assert stats2 == {'id_masked': 0, 'bank_masked': 0}

    # 只读扫描计数与脱敏一致且不修改输入
    assert scan_document_text(sample) == {'id_masked': 2, 'bank_masked': 2}


# ── 2. core/llm_gateway：chat / chat_stream / embed 三出口统一脱敏与图文保真 ─────

def test_e40_llm_gateway_three_exits_and_multimodal_preservation(monkeypatch):
    monkeypatch.setenv('PRIVACY_SANITIZE_ENABLED', '1')
    captured_chat_reqs: list[list] = []
    captured_embed_reqs: list[list[str]] = []

    class _FakeCompletions:
        def create(self, **kwargs):
            captured_chat_reqs.append(kwargs['messages'])
            if kwargs.get('stream'):
                chunk = SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content='流式响应OK'))],
                    usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
                )
                return iter([chunk])
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='同步响应OK'))],
                usage=SimpleNamespace(prompt_tokens=12, completion_tokens=6),
            )

    class _FakeEmbeddings:
        def create(self, **kwargs):
            captured_embed_reqs.append(list(kwargs['input']))
            return SimpleNamespace(
                data=[SimpleNamespace(index=i, embedding=[0.1, 0.2]) for i in range(len(kwargs['input']))],
                usage=SimpleNamespace(prompt_tokens=8),
            )

    client = LLMClient(
        {
            'base_url': 'http://10.0.2.81:3000/v1',
            'models': {'flash': 'GLM-5.3-Flash', 'flagship': 'GLM-5.3-Flash', 'embedding': 'emb-v1'},
            'supports_vision': True,
            'pricing': {},
        },
        'test-key',
    )
    client.use_openai = True
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions()), embeddings=_FakeEmbeddings())

    raw_id = '11010519491231002X'
    raw_bank = '6222021001112223334'
    orig_msgs = [
        {'role': 'system', 'content': f'核查主体身份证 {raw_id}'},
        {
            'role': 'user',
            'content': [
                {'type': 'text', 'text': f'请核验银行账号：{raw_bank}'},
                {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,iVBORw0KGgo='}},
            ],
        },
    ]

    # 1) chat() 出口
    resp = client.chat(orig_msgs, model_tier='flash')
    assert resp.content == '同步响应OK'
    sent_msgs = captured_chat_reqs[0]
    assert raw_id not in json.dumps(sent_msgs, ensure_ascii=False)
    assert raw_bank not in json.dumps(sent_msgs, ensure_ascii=False)
    assert sent_msgs[1]['content'][1] == {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,iVBORw0KGgo='}}
    # 调用方原始 messages 对象绝不被就地篡改
    assert raw_id in orig_msgs[0]['content']

    # 2) chat_stream() 出口
    events = list(client.chat_stream(orig_msgs, model_tier='flash'))
    assert events[-1]['type'] == 'final'
    assert raw_id not in json.dumps(captured_chat_reqs[1], ensure_ascii=False)
    assert raw_bank not in json.dumps(captured_chat_reqs[1], ensure_ascii=False)

    # 3) embed() 出口
    vecs = client.embed([f'银行卡号：{raw_bank}，证件号：{raw_id}'])
    assert len(vecs) == 1
    assert raw_id not in captured_embed_reqs[0][0]
    assert raw_bank not in captured_embed_reqs[0][0]

    usage = client.total_usage()
    assert usage['privacy_id_masked'] == 3
    assert usage['privacy_bank_masked'] == 3

    # 4) PRIVACY_SANITIZE_ENABLED=0 对照验证
    monkeypatch.setenv('PRIVACY_SANITIZE_ENABLED', '0')
    client.chat([{'role': 'user', 'content': f'银行卡号：{raw_bank}'}], model_tier='flash')
    assert raw_bank in json.dumps(captured_chat_reqs[2], ensure_ascii=False)


# ── 3. STRICT_INNER_MODEL_ONLY 内网防外泄硬锁 + 存量公网忽略告警 ─────────────────

def test_e40_strict_inner_model_lock_and_stale_user_config_ignore(tmp_path: Path, monkeypatch):
    db_file = tmp_path / 'e40_strict.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    assert is_inner_model_url_allowed('http://10.0.2.81:3000/v1') is True
    assert is_inner_model_url_allowed('http://192.168.10.22:8000/v1') is True
    assert is_inner_model_url_allowed('https://open.bigmodel.cn/api/paas/v4') is False
    assert is_inner_model_url_allowed('http://127.0.0.1:8000/v1') is False

    with TestClient(app) as tc:
        admin_h = _login_headers(tc, 'admin')

        # 1) strict=0 时允许预先保存一条公网自定义配置（模拟存量历史数据）
        monkeypatch.setenv('STRICT_INNER_MODEL_ONLY', '0')
        r_save_pub = tc.put(
            '/api/model-config',
            headers=admin_h,
            json={
                'base_url': 'https://open.bigmodel.cn/api/paas/v4',
                'api_key': 'pub-key-123456',
                'model_flash': 'glm-5.3-flash',
                'model_flagship': 'glm-5.3-flash',
            },
        )
        assert r_save_pub.status_code == 200

        # 2) 开启 strict=1 后：公网地址保存与测试全部返回 422 MODEL_HOST_FORBIDDEN
        monkeypatch.setenv('STRICT_INNER_MODEL_ONLY', '1')
        monkeypatch.setenv('INNER_MODEL_EXTRA_HOSTS', 'maas-inner.corp.local')

        for endpoint in ('/api/model-config', '/api/model-config/test'):
            r_blk = tc.request(
                'PUT' if endpoint.endswith('model-config') else 'POST',
                endpoint,
                headers=admin_h,
                json={
                    'base_url': 'https://api.openai.com/v1',
                    'api_key': 'sk-public',
                    'model_flash': 'gpt-4o-mini',
                },
            )
            assert r_blk.status_code == 422
            assert r_blk.json()['detail']['code'] == 'MODEL_HOST_FORBIDDEN'

        # 2b) 禁用 demo 云端环境回退
        with pytest.raises(RuntimeError):
            get_llm_client('demo')
        monkeypatch.setenv('PRIVATE_API_KEY', 'inner-secret-key')
        prod_client = get_llm_client()
        assert prod_client.base_url == 'http://10.0.2.81:3000/v1'

        # 2c) 存量公网用户配置在 resolve_effective_llm_client 时被忽略并安全回退 prod 内网网关
        import backend.database as db_mod
        sess = db_mod.SessionLocal()
        try:
            eff_client, status = resolve_effective_llm_client(sess, 1)
            assert status == 'done'
            assert eff_client.base_url == 'http://10.0.2.81:3000/v1'
        finally:
            sess.close()

        # 3) strict=1 下在原自定义配置界面依然可正常保存内网 IP 与白名单内网域！
        r_inner_ip = tc.put(
            '/api/model-config',
            headers=admin_h,
            json={
                'base_url': 'http://192.168.10.22:8000/v1',
                'api_key': 'inner-user-key-9999',
                'model_flash': 'DeepSeek-V3',
                'model_flagship': 'Qwen2.5-72B',
            },
        )
        assert r_inner_ip.status_code == 200
        assert r_inner_ip.json()['base_url'] == 'http://192.168.10.22:8000/v1'

        r_inner_dom = tc.put(
            '/api/model-config',
            headers=admin_h,
            json={
                'base_url': 'https://maas-inner.corp.local/v1',
                'api_key': 'inner-dom-key-8888',
                'model_flash': 'GLM-5.3-Flash',
            },
        )
        assert r_inner_dom.status_code == 200
        with pytest.raises(ValueError):
            build_client_from_user_config('https://api.openai.com/v1', 'k', 'm')


# ── 4. 上传零 OCR 统计 + 实弹探针（原文件 SHA256 不变 + E5 不漂移 + 送 LLM 已掩码）─

def test_e40_upload_zero_ocr_stats_and_f4_f7_redline_probe(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    monkeypatch.setenv('PRIVACY_SANITIZE_ENABLED', '1')

    raw_id = '11010519491231002X'
    raw_bank = '6222 0210 0111 2223 334'

    tender_p = _make_docx(
        tmp_path / 'probe_tender.docx',
        [
            '项目名称：内网脱敏实弹验证项目',
            '★1. 须承诺提供 7×24 小时本地化安全应急响应。',
            f'保证金收款银行账号：{raw_bank}，联系人身份证号：{raw_id}。',
        ],
    )
    bid_p = _make_docx(
        tmp_path / 'probe_bid.docx',
        [
            '项目名称：内网脱敏实弹验证项目 投标文件',
            '我方完全响应★1：提供 7×24 小时本地化安全应急响应。',
            f'我方授权代表身份证：{raw_id}，公司基本户银行账号：{raw_bank}。',
        ],
    )
    orig_tender_sha = hashlib.sha256(tender_p.read_bytes()).hexdigest()
    orig_bid_sha = hashlib.sha256(bid_p.read_bytes()).hexdigest()

    # 断言 scan_document_file 强制传 ocr_images=False
    seen_ocr_flags: list[bool] = []
    import core.privacy_sanitizer as ps_mod
    real_parse = ps_mod.parse_document

    def _spy_parse(fp, *args, **kwargs):
        seen_ocr_flags.append(bool(kwargs.get('ocr_images', True)))
        return real_parse(fp, *args, **kwargs)

    monkeypatch.setattr(ps_mod, 'parse_document', _spy_parse)
    st_check = scan_document_file(tender_p)
    assert st_check == {'id_masked': 1, 'bank_masked': 1}
    assert seen_ocr_flags == [False]

    db_file = tmp_path / 'e40_upload.db'
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)
    created_task_id: int | None = None
    with TestClient(app) as tc:
        admin_h = _login_headers(tc, 'admin')

        # 1) /api/extract 返回 privacy_stats
        r_ext = tc.post(
            '/api/extract',
            headers=admin_h,
            files={'file': ('probe_tender.docx', tender_p.read_bytes(), 'application/octet-stream')},
        )
        assert r_ext.status_code == 200, r_ext.text
        assert r_ext.json()['privacy_stats'] == {'id_masked': 1, 'bank_masked': 1}

        # 2) /api/checks 创建返回 privacy_stats，且磁盘落盘文件 SHA256 与源文件逐字节一致
        r_chk = tc.post(
            '/api/checks',
            headers=admin_h,
            files=[
                ('tender_file', ('probe_tender.docx', tender_p.read_bytes(), 'application/octet-stream')),
                ('bid_file', ('probe_bid.docx', bid_p.read_bytes(), 'application/octet-stream')),
            ],
            data={'title': 'E40脱敏上传探针', 'auto_run': 'false'},
        )
        assert r_chk.status_code == 200, r_chk.text
        chk_body = r_chk.json()
        created_task_id = int(chk_body['id'])
        assert chk_body['privacy_stats'] == {'id_masked': 2, 'bank_masked': 2}

        import backend.database as db_mod
        from backend.models import BidCheckTask
        sess = db_mod.SessionLocal()
        try:
            row = sess.query(BidCheckTask).filter_by(id=created_task_id).first()
            assert row is not None
            proj_root = Path(__file__).resolve().parents[1]
            disk_tender_sha = hashlib.sha256((proj_root / row.tender_path).read_bytes()).hexdigest()
            disk_bid_sha = hashlib.sha256((proj_root / row.bid_path).read_bytes()).hexdigest()
            assert disk_tender_sha == orig_tender_sha
            assert disk_bid_sha == orig_bid_sha
        finally:
            sess.close()

        # 3) /api/qa/sessions/{id}/attachments 返回 privacy_stats
        r_qs = tc.post('/api/qa/sessions', headers=admin_h, json={})
        assert r_qs.status_code == 200
        qsid = r_qs.json()['id']
        r_qatt = tc.post(
            f'/api/qa/sessions/{qsid}/attachments',
            headers=admin_h,
            files={'file': ('probe_bid.docx', bid_p.read_bytes(), 'application/octet-stream')},
        )
        assert r_qatt.status_code == 200, r_qatt.text
        assert r_qatt.json()['privacy_stats'] == {'id_masked': 1, 'bank_masked': 1}
        assert tc.delete(f'/api/qa/sessions/{qsid}', headers=admin_h).status_code == 200
        if created_task_id is not None:
            assert tc.delete(f'/api/checks/{created_task_id}', headers=admin_h).status_code == 200

    # 4) 实弹：含身份证/银行卡语料走 F4 + F7（经 LLMClient）与 E5 批注锚点验证
    sent_payloads: list[str] = []

    class _SpyCompletions:
        def create(self, **kwargs):
            sent_payloads.append(json.dumps(kwargs['messages'], ensure_ascii=False))
            # 模拟 F4 或 F7 收口回复
            prompt_str = str(kwargs['messages'][-1].get('content') or '')
            if 'action' in prompt_str.lower() or 'final' in prompt_str.lower():
                ans = json.dumps({
                    'thought': '已直接回答',
                    'action': 'final',
                    'action_input': {
                        'answer': '我方完全响应★1：提供 7×24 小时本地化安全应急响应。',
                        'quotes': [{'source': 'bid', 'quote': '我方完全响应★1：提供 7×24 小时本地化安全应急响应。'}],
                    },
                }, ensure_ascii=False)
            else:
                ans = json.dumps({
                    'status': 'pass',
                    'message': '实质响应一致',
                    'quote': '我方完全响应★1：提供 7×24 小时本地化安全应急响应。',
                }, ensure_ascii=False)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=ans))],
                usage=SimpleNamespace(prompt_tokens=20, completion_tokens=15),
            )

    spy_client = LLMClient(
        {'base_url': 'http://10.0.2.81:3000/v1', 'models': {'flash': 'GLM-5.3-Flash', 'flagship': 'GLM-5.3-Flash'}},
        'k',
    )
    spy_client.use_openai = True
    spy_client.client = SimpleNamespace(chat=SimpleNamespace(completions=_SpyCompletions()))

    # 4a) F4 调用
    run_response_check(str(tender_p), str(bid_p), client=spy_client)
    # 4b) F7 调用
    ask_bid_expert(tender_p, bid_p, '请确认★1条款我方如何响应？', client=spy_client, max_rounds=2)

    all_sent = '\n'.join(sent_payloads)
    assert raw_id not in all_sent
    assert raw_bank not in all_sent

    # 4c) E5 原位 Word 批注精确命中未篡改原文
    class _FakeFinding:
        severity = 'warning'
        rule_name = 'DEMO_CHECK'
        message = '核验银行账户条款'
        suggestion = '核对账号'
        legal_basis = ''
        clause_no = '★1'
        quote = '我方完全响应★1：提供 7×24 小时本地化安全应急响应。'

    redline_out = tmp_path / 'probe_redline.docx'
    inject_word_comments(str(bid_p), [_FakeFinding()], out_docx_path=str(redline_out))
    assert redline_out.is_file()
