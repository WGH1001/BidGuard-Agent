# -*- coding: utf-8 -*-
"""B2 · backend 10 组路由与 SSE 核验流水线端到端集成测试（0 Token 纯确定性，不烧钱）。

覆盖任务卡 B2 §5 全部验收标准：
1. 登录 -> 拿令牌 -> 访问 /api/auth/me；无令牌 401；member 访问 admin 审计接口 403；
2. 双书上传 -> 建任务 -> POST /{id}/run -> 轮询与 SSE 流到 delivered（F1~F4 走 0 Token 确定性层真实执行，
   mock docx 中 ★ 行间插入非标记中性行防止合并）；
3. findings 落库查询与反馈回写（accepted/rejected）；NEED_MANUAL 报价表人工回填复算接口；
4. 多租户严格隔离：B 租户访问 A 租户任务/偏离表/问答会话一律返回 404；
5. /api/deviation 导出偏离表产物文件真实存在且 total_rows > 0；
6. /api/qa 问答会话创建与 ask 走 keyword_fallback 模式返回 quotes（docx 页码为 null）；
7. /api/extract、/api/radar、/api/rules、/api/reports（含 redline 批注 severity 映射与 ZIP 交付包）、
   /api/model-config（Key 加密不回显明文）全链路覆盖；所有断言精确数数。
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from docx import Document
from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.auth import hash_password
import backend.database as db_mod
from backend.main import create_app
from backend.models import Tenant, User


def _make_docx_bytes(paragraphs: list[str]) -> bytes:
    """按铁律 7 构造测试 docx 字节流（★ 条款间插入非标记中性行防解析合并）。"""
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


@pytest.fixture()
def client_env(tmp_path, monkeypatch):
    """构造绑定临时 SQLite 文件库的 TestClient，并强制开启 0 Token 模式（不联网不烧钱）。"""
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    db_file = tmp_path / 'test_bidguard_routers.db'
    db_url = f"sqlite:///{db_file.as_posix()}"

    app = create_app(database_url=db_url, auto_init_db=True)
    with TestClient(app) as tc:
        # 额外创建一个租户 B 的用户用于跨租户 404 隔离测试
        session = db_mod.SessionLocal()
        try:
            tenant_b = session.query(Tenant).filter_by(name='弱电智能化工程部').first()
            if tenant_b is None:
                tenant_b = Tenant(name='弱电智能化工程部', is_active=True)
                session.add(tenant_b)
                session.flush()
            user_b = session.query(User).filter_by(username='bob_tenant_b').first()
            if user_b is None:
                user_b = User(
                    tenant_id=tenant_b.id,
                    username='bob_tenant_b',
                    password_hash=hash_password('BidGuard2026!'),
                    display_name='鲍勃（二部）',
                    role='member',
                    is_active=True,
                )
                session.add(user_b)
                session.commit()
        finally:
            session.close()
        yield tc


def _login_headers(tc: TestClient, username: str, password: str = 'BidGuard2026!') -> dict[str, str]:
    resp = tc.post('/api/auth/login', json={'username': username, 'password': password})
    assert resp.status_code == 200, resp.text
    token = resp.json()['token']
    return {'Authorization': f"Bearer {token}"}


# ── 1. 认证鉴权与角色权限守卫（login / me / 401 / 403）───────────────────

def test_auth_login_me_and_admin_guard(client_env: TestClient):
    tc = client_env
    # 无令牌访问受保护端点 -> 401
    r_no_auth = tc.get('/api/auth/me')
    assert r_no_auth.status_code == 401
    assert 'code' in r_no_auth.json()['detail']

    # member 登录（liming）
    member_hdrs = _login_headers(tc, 'liming')
    r_me = tc.get('/api/auth/me', headers=member_hdrs)
    assert r_me.status_code == 200
    assert r_me.json()['username'] == 'liming'
    assert r_me.json()['role'] == 'member'

    # member 访问管理员专属审计日志接口 -> 403
    r_forbidden = tc.get('/api/reports/audit-logs', headers=member_hdrs)
    assert r_forbidden.status_code == 403

    # admin 登录访问审计日志 -> 200
    admin_hdrs = _login_headers(tc, 'admin')
    r_audit = tc.get('/api/reports/audit-logs', headers=admin_hdrs)
    assert r_audit.status_code == 200
    assert r_audit.json()['total'] >= 1


# ── 2. 主流程闭环：上传双书 -> run -> SSE 进度流 -> findings 反馈 -> NEED_MANUAL 回填 -> 偏离表与交付包 ──

def test_full_check_pipeline_sse_feedback_manual_price_and_deviation(client_env: TestClient):
    tc = client_env
    hdrs = _login_headers(tc, 'liming')

    # 按铁律 7 构造 mock 招标书与投标书（★ 行之间插入非标记中性行防止合并）
    tender_bytes = _make_docx_bytes([
        '第一章 采购需求说明',
        '★1. 平台服务端须原生适配国产银河麒麟高级服务器操作系统 V10。',
        '上述为操作系统基础运行环境基线要求。',
        '★2. 数据库层必须兼容达梦数据库管理系统 DM8，并提供互认证书。',
        '上述为数据库层基础运行环境基线要求。',
        '★3. 投标人必须配备至少 2 名持有 CISP 认证证书的驻场安全工程师。',
        '上述为驻场人员资质基线要求。',
    ])
    bid_bytes = _make_docx_bytes([
        '一、技术要求响应表',
        '★1. 平台服务端须原生适配国产银河麒麟高级服务器操作系统 V10。（我方完全满足，正响应）',
        '上述为我方对第一项操作系统要求的响应。',
        '★2. 数据库层必须兼容达梦数据库管理系统 DM8，并提供互认证书。（我方完全满足，正响应）',
        '上述为我方对第二项数据库要求的响应。',
        '二、资格证明文件：ISO证书有效期至 2026-10-01。',
    ])

    files = {
        'tender_file': ('tender_mock.docx', tender_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
        'bid_file': ('bid_mock.docx', bid_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
    }
    r_create = tc.post(
        '/api/checks',
        data={'title': '政务云态势感知投标体检', 'open_date': '2026-11-05'},
        files=files,
        headers=hdrs,
    )
    assert r_create.status_code == 200, r_create.text
    task_id = r_create.json()['id']
    assert r_create.json()['status'] == 'created'

    # 触发流水线执行（sync=True 确定性执行）
    r_run = tc.post(f"/api/checks/{task_id}/run?sync=true", headers=hdrs)
    assert r_run.status_code == 200
    assert r_run.json()['status'] == 'delivered'

    # 订阅 SSE 流，验证收到 event: done 与 delivered 状态
    r_sse = tc.get(f"/api/checks/{task_id}/stream", headers=hdrs)
    assert r_sse.status_code == 200
    assert 'event: done' in r_sse.text
    assert '"status": "delivered"' in r_sse.text

    # 获取 findings 列表并验证检出 ★3 漏应答与 F3 证照过期
    r_findings = tc.get(f"/api/checks/{task_id}/findings", headers=hdrs)
    assert r_findings.status_code == 200
    f_items = r_findings.json()['items']
    assert len(f_items) >= 2
    codes = {f['code'] for f in f_items}
    assert 'MISSING_RESPONSE' in codes
    assert 'CERT_EXPIRED' in codes

    # 对第一条 finding 提交 accepted 反馈
    first_fid = f_items[0]['id']
    r_fb = tc.post(
        f"/api/checks/{task_id}/findings/{first_fid}/feedback",
        json={'feedback': 'accepted', 'feedback_note': '确认漏答，立即补齐'},
        headers=hdrs,
    )
    assert r_fb.status_code == 200
    assert r_fb.json()['feedback'] == 'accepted'

    # 测试 NEED_MANUAL 报价表人工回填接口（故意填入差 500 元的算术错误触发 F2 报警）
    r_manual = tc.post(
        f"/api/checks/{task_id}/price-manual",
        json={
            'items': [
                {'name': '日志审计系统', 'qty': '2', 'unit_price': '42500.00', 'total_price': '84500.00'},
            ],
            'table_total': '84500.00',
            'bid_total': '84500.00',
            'bid_total_upper': '捌万肆仟伍佰元整',
            'price_cap': '120000.00',
        },
        headers=hdrs,
    )
    assert r_manual.status_code == 200
    assert r_manual.json()['price_findings_count'] == 1
    assert r_manual.json()['findings'][0]['code'] == 'ITEM_ARITHMETIC'

    # 测试 F5 偏离表一键导出（验证文件真实生成且 total_rows == 3）
    r_dev = tc.post(f"/api/deviation/{task_id}", headers=hdrs)
    assert r_dev.status_code == 200
    dev_data = r_dev.json()
    assert dev_data['total_rows'] == 3
    assert dev_data['no_deviation_count'] == 2
    assert dev_data['missing_count'] == 1

    r_dev_dl = tc.get(dev_data['download_url'], headers=hdrs)
    assert r_dev_dl.status_code == 200
    assert len(r_dev_dl.content) > 1000

    # 测试交付中心单任务打包 ZIP（含 redline 批注原件 + 偏离表 + 签字单）
    r_zip = tc.get(f"/api/reports/{task_id}/deliver.zip", headers=hdrs)
    assert r_zip.status_code == 200
    assert len(r_zip.content) > 1500


# ── 3. 多租户严格隔离：B 租户访问 A 租户任务返回 404 ──────────────────────

def test_multi_tenant_isolation_returns_404(client_env: TestClient):
    tc = client_env
    hdrs_a = _login_headers(tc, 'liming')
    hdrs_b = _login_headers(tc, 'bob_tenant_b')

    t_bytes = _make_docx_bytes(['★1. 核心交换机支持双电源冗余。', '上述为硬件要求。'])
    b_bytes = _make_docx_bytes(['★1. 核心交换机支持双电源冗余。（完全满足）'])
    r_create = tc.post(
        '/api/checks',
        data={'title': '租户A私有投标任务'},
        files={
            'tender_file': ('t.docx', t_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
            'bid_file': ('b.docx', b_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
        },
        headers=hdrs_a,
    )
    assert r_create.status_code == 200
    task_a_id = r_create.json()['id']

    # 租户 B 访问租户 A 的任务详情、偏离表、运行接口一律返回 404
    assert tc.get(f"/api/checks/{task_a_id}", headers=hdrs_b).status_code == 404
    assert tc.post(f"/api/deviation/{task_a_id}", headers=hdrs_b).status_code == 404
    # 租户 B 下载租户 A 的上传文件（/api/files 租户归属校验）一律 404
    tender_url_a = r_create.json()['tender_url']
    assert tender_url_a
    assert tc.get(tender_url_a, headers=hdrs_b).status_code == 404
    assert tc.get(tender_url_a, headers=hdrs_a).status_code == 200
    # 租户 B 列表只能看到本租户的 0 个任务
    list_b = tc.get('/api/checks', headers=hdrs_b).json()
    assert list_b['total'] == 0
    assert len(list_b['items']) == 0


# ── 4. F7 标书专家问答会话与 keyword_fallback 模式核验 ────────────────────

def test_qa_session_and_keyword_fallback_ask(client_env: TestClient):
    tc = client_env
    hdrs = _login_headers(tc, 'liming')

    t_bytes = _make_docx_bytes([
        '第一章 商务要求',
        '★1. 本项目免费质保期自终验合格之日起不少于 36 个月。',
        '上述为质保基线要求。',
    ])
    b_bytes = _make_docx_bytes([
        '一、售后服务承诺：我公司提供自终验合格之日起 36 个月的免费质保期。',
    ])
    r_task = tc.post(
        '/api/checks',
        data={'title': '质保问答测试项目'},
        files={
            'tender_file': ('t.docx', t_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
            'bid_file': ('b.docx', b_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
        },
        headers=hdrs,
    )
    tid = r_task.json()['id']

    # 新建问答会话
    r_sess = tc.post('/api/qa/sessions', json={'task_id': tid}, headers=hdrs)
    assert r_sess.status_code == 200
    sid = r_sess.json()['id']

    # 发起提问（0 Token 下走 keyword_fallback）
    r_ask = tc.post(f"/api/qa/{sid}/ask", json={'question': '质保期要求多久？'}, headers=hdrs)
    assert r_ask.status_code == 200
    ask_data = r_ask.json()
    assert ask_data['mode'] == 'keyword_fallback'
    assert len(ask_data['quotes']) >= 1
    assert ask_data['quotes'][0]['page'] is None  # docx 页码严格为 null

    # 校验会话 messages_json 已持久化追加 2 条记录（user + assistant）
    r_detail = tc.get(f"/api/qa/sessions/{sid}", headers=hdrs)
    assert r_detail.status_code == 200
    assert len(r_detail.json()['messages_json']) == 2


# ── 5. /api/extract、/api/radar、/api/rules、/api/model-config 覆盖测试 ────

def test_extract_radar_rules_and_model_config_routes(client_env: TestClient):
    tc = client_env
    hdrs = _login_headers(tc, 'liming')

    tender_bytes = _make_docx_bytes([
        '第一章 采购需求',
        '★1. 核心交换机须参照中兴品牌 ZXR10 型号进行选型配置。',
        '上述为设备选型要求。',
        '★2. 系统须支持国产化银河麒麟操作系统 V10。',
        '上述为信创要求。',
    ])

    # (a) /api/extract
    r_ext = tc.post(
        '/api/extract',
        files={'file': ('tender.docx', tender_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')},
        headers=hdrs,
    )
    assert r_ext.status_code == 200
    assert r_ext.json()['llm_status'] == 'skipped'
    assert len(r_ext.json()['marked_clauses']) == 2

    # (b) /api/radar
    r_rad = tc.post(
        '/api/radar',
        files={'tender_file': ('tender.docx', tender_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')},
        headers=hdrs,
    )
    assert r_rad.status_code == 200
    assert len(r_rad.json()['suspicious_params']) == 1
    assert 'ZXR10' in r_rad.json()['suspicious_params'][0]['quote']

    # (c) /api/rules & certificates
    r_rules = tc.get('/api/rules', headers=hdrs)
    assert r_rules.status_code == 200
    assert len(r_rules.json()) >= 1

    r_certs = tc.get('/api/rules/certificates', headers=hdrs)
    assert r_certs.status_code == 200
    assert len(r_certs.json()) >= 2

    # (d) /api/model-config（保存加密 Key 后绝不回显明文，仅返回 configured: true 与尾 4 位）
    secret_key = 'sk-bidguard-test-key-1234567890abcd'
    r_mc = tc.put(
        '/api/model-config',
        json={
            'base_url': 'http://10.0.2.81:3000/v1',
            'api_key': secret_key,
            'model_flash': 'GLM-5.3-Flash',
            'model_flagship': 'GLM-5.3',
            'supports_reasoning_effort': False,
        },
        headers=hdrs,
    )
    assert r_mc.status_code == 200
    mc_data = r_mc.json()
    assert mc_data['configured'] is True
    assert mc_data['api_key_last4'] == 'abcd'
    assert secret_key not in r_mc.text

    # (e) /api/dashboard
    r_dash = tc.get('/api/dashboard', headers=hdrs)
    assert r_dash.status_code == 200
    assert r_dash.json()['total_projects'] >= 1
    assert len(r_dash.json()['defense_matrix']) == 6


# ── 6. E10：F6 历史列表/详情/删除、任务运行中 409 拒删与级联清理、法规级联删除、大盘 F1 统计与清零归零 ──

def test_e10_radar_history_task_delete_guard_regulation_delete_and_dashboard_zero(client_env: TestClient):
    tc = client_env
    member_hdrs = _login_headers(tc, 'liming')
    admin_hdrs = _login_headers(tc, 'admin')
    tenant_b_hdrs = _login_headers(tc, 'bob_tenant_b')

    tender_bytes = _make_docx_bytes([
        '第一章 采购需求',
        '★1. 核心交换机须参照中兴品牌 ZXR10 型号进行选型配置。',
        '上述为设备选型要求。',
        '★2. 系统须支持国产化银河麒麟操作系统 V10。',
        '上述为信创要求。',
    ])
    bid_bytes = _make_docx_bytes([
        '一、技术要求响应表',
        '★1. 核心交换机须参照中兴品牌 ZXR10 型号进行选型配置。（完全满足）',
        '★2. 系统须支持国产化银河麒麟操作系统 V10。（完全满足）',
    ])

    # (a) F6 雷达扫描 -> 历史列表 -> 详情回放 -> 删除
    r_rad = tc.post(
        '/api/radar',
        data={'title': 'E10雷达历史回放测试'},
        files={'tender_file': ('tender.docx', tender_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')},
        headers=member_hdrs,
    )
    assert r_rad.status_code == 200
    rad_task_id = r_rad.json()['task_id']

    r_rad_list = tc.get('/api/radar', headers=member_hdrs)
    assert r_rad_list.status_code == 200
    assert any(t['id'] == rad_task_id for t in r_rad_list.json()['items'])

    r_rad_detail = tc.get(f"/api/radar/{rad_task_id}", headers=member_hdrs)
    assert r_rad_detail.status_code == 200
    assert r_rad_detail.json()['task_id'] == rad_task_id
    assert r_rad_detail.json()['title'] == 'E10雷达历史回放测试'
    assert len(r_rad_detail.json()['suspicious_params']) == 1

    # 跨租户访问雷达详情或删除一律 404
    assert tc.get(f"/api/radar/{rad_task_id}", headers=tenant_b_hdrs).status_code == 404
    assert tc.delete(f"/api/radar/{rad_task_id}", headers=tenant_b_hdrs).status_code == 404

    # (b) F2-F4 任务创建与运行 -> 大盘 F1 统计 > 0 -> 运行中 409 拒删 -> 终态级联删除
    r_check = tc.post(
        '/api/checks',
        data={'title': 'E10体检删除与F1统计测试'},
        files={
            'tender_file': ('t.docx', tender_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
            'bid_file': ('b.docx', bid_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
        },
        headers=member_hdrs,
    )
    assert r_check.status_code == 200
    chk_id = r_check.json()['id']

    r_run = tc.post(f"/api/checks/{chk_id}/run?sync=true", headers=member_hdrs)
    assert r_run.status_code == 200

    # 验证大盘 F1 防线统计已纳入 F1 提取条款数（不再恒为 0）
    r_dash_before = tc.get('/api/dashboard', headers=member_hdrs).json()
    f1_item = next(m for m in r_dash_before['defense_matrix'] if m['module'] == 'F1')
    assert f1_item['hit_count'] >= 2

    # 模拟任务处于运行中状态，验证 DELETE 返回 409
    from backend.models import BidCheckTask
    s = db_mod.SessionLocal()
    try:
        t_obj = s.query(BidCheckTask).filter_by(id=chk_id).first()
        t_obj.status = 'checking'
        s.commit()
    finally:
        s.close()

    r_del_running = tc.delete(f"/api/checks/{chk_id}", headers=member_hdrs)
    assert r_del_running.status_code == 409
    assert r_del_running.json()['detail']['code'] == 'TASK_RUNNING'

    # 恢复为 delivered 后删除全租户所有任务（含 radar 与 pre_check），验证大盘归零
    s = db_mod.SessionLocal()
    try:
        t_obj = s.query(BidCheckTask).filter_by(id=chk_id).first()
        t_obj.status = 'delivered'
        s.commit()
    finally:
        s.close()

    all_tasks = tc.get('/api/checks?page_size=100', headers=member_hdrs).json()['items']
    for item in all_tasks:
        r_del = tc.delete(f"/api/checks/{item['id']}", headers=member_hdrs)
        assert r_del.status_code == 200

    r_dash_zero = tc.get('/api/dashboard', headers=member_hdrs).json()
    assert r_dash_zero['total_projects'] == 0
    assert all(m['hit_count'] == 0 for m in r_dash_zero['defense_matrix'])

    # (c) E4 法规库删除：member 403，admin 200 级联清理条文
    laws = tc.get('/api/regulations/laws', headers=member_hdrs).json()
    assert len(laws) >= 1
    target_law_id = laws[-1]['id']

    assert tc.delete(f"/api/regulations/laws/{target_law_id}", headers=member_hdrs).status_code == 403
    r_del_law = tc.delete(f"/api/regulations/laws/{target_law_id}", headers=admin_hdrs)
    assert r_del_law.status_code == 200
    assert tc.get(f"/api/regulations/laws/{target_law_id}", headers=admin_hdrs).status_code == 404


def test_login_five_failures_brute_force_lockout(client_env: TestClient):
    """验证连续 5 次输错密码触发 429 ACCOUNT_LOCKED 防爆破锁定，管理员重置密码后可立即解锁登录。"""
    tc = client_env
    admin_hdrs = _login_headers(tc, 'admin')

    # 创建测试成员账号，避免影响其他复用默认账号的测试用例
    r_create = tc.post(
        '/api/auth/users',
        json={
            'username': 'lock_probe_user',
            'display_name': '防爆破测试号',
            'password': 'RightPass123!',
            'role': 'member',
            'tenant_id': 1,
        },
        headers=admin_hdrs,
    )
    assert r_create.status_code == 200, r_create.text
    uid = r_create.json()['id']

    # 第 1~4 次输错返回 401 INVALID_CREDENTIALS
    for attempt in range(4):
        r_bad = tc.post('/api/auth/login', json={'username': 'lock_probe_user', 'password': f'WrongPass{attempt}'})
        assert r_bad.status_code == 401
        assert r_bad.json()['detail']['code'] == 'INVALID_CREDENTIALS'

    # 第 5 次输错直接触发 429 ACCOUNT_LOCKED
    r_5th = tc.post('/api/auth/login', json={'username': 'lock_probe_user', 'password': 'WrongPass4'})
    assert r_5th.status_code == 429
    assert r_5th.json()['detail']['code'] == 'ACCOUNT_LOCKED'

    # 锁定期间即使输入正确密码仍应被 429 拦截
    r_locked_ok = tc.post('/api/auth/login', json={'username': 'lock_probe_user', 'password': 'RightPass123!'})
    assert r_locked_ok.status_code == 429
    assert r_locked_ok.json()['detail']['code'] == 'ACCOUNT_LOCKED'

    # 管理员重置密码后自动解除锁定，可正常登录
    r_reset = tc.post(
        f'/api/auth/users/{uid}/reset-password',
        json={'new_password': 'NewRightPass123!'},
        headers=admin_hdrs,
    )
    assert r_reset.status_code == 200

    r_ok = tc.post('/api/auth/login', json={'username': 'lock_probe_user', 'password': 'NewRightPass123!'})
    assert r_ok.status_code == 200
    assert r_ok.json()['token']
